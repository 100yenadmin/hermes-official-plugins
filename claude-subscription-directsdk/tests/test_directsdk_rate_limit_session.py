"""Secret-free rate limit metadata in each per-call/session step, including streaming."""
import json
import logging

import pytest

import directsdk
import test_directsdk_session as T
from test_directsdk_session import rig  # noqa: F401

BASE = {'window': 'five_hour', 'overage_status': 'allowed', 'using_overage': False}
FULL = {**BASE, 'status': 'allowed_warning', 'utilization': 0.875,
        'surpassed_threshold': 0.8, 'resets_at': 1800000000}


def test_summary_optional_fields_present_and_only_allowlisted_fields():
    summary = directsdk._rate_limit_summary({'type': 'rate_limit_event', 'ignored': 'PRIVATE_SENTINEL',
        'rate_limit_info': {'rateLimitType': 'five_hour', 'overageStatus': 'allowed',
                            'status': 'allowed_warning', 'utilization': 0.875,
                            'surpassedThreshold': 0.8, 'resetsAt': 1800000000.9,
                            'ignored': 'PRIVATE_SENTINEL'}})
    assert summary == FULL


def test_summary_optional_fields_absent():
    assert directsdk._rate_limit_summary({'rate_limit_info': {
        'rateLimitType': 'five_hour', 'overageStatus': 'allowed'}}) == BASE


@pytest.mark.parametrize('value', [None, '0.9', True, [], {}, float('nan'), float('inf'), float('-inf')])
def test_summary_ignores_non_numeric_optional_fields(value):
    assert directsdk._rate_limit_summary({'rate_limit_info': {
        'rateLimitType': 'five_hour', 'overageStatus': 'allowed',
        'status': 42, 'utilization': value, 'surpassedThreshold': value, 'resetsAt': value}}) == BASE


@pytest.mark.parametrize('mode', ['0', '1'], ids=['per-call', 'session'])
@pytest.mark.parametrize('stream', [False, True], ids=['response', 'stream'])
@pytest.mark.parametrize('last_full', [False, True], ids=['last-minimal', 'last-full'])
def test_in_stream_events_record_last_summary_log_once_and_do_not_leak_between_steps(rig, tmp_path, caplog, mode, stream, last_full):
    first_event = {'type': 'rate_limit_event', 'ignored': 'PRIVATE_SENTINEL', 'rate_limit_info': {
        'rateLimitType': 'five_hour', 'overageStatus': 'allowed', 'status': 'allowed_warning',
        'utilization': 0.875, 'surpassedThreshold': 0.8, 'resetsAt': 1800000000.9,
        'ignored': 'PRIVATE_SENTINEL'}}
    second_event = {'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'overage'}}
    events = [second_event, first_event] if last_full else [first_event, second_event]
    anchor = "    out({'type':'stream_event','event':{'type':'message_stop'}})"
    inject = ('    if len(history) == 1:\n'
              '        out(' + repr(events[0]) + ')\n'
              '        out(' + repr(events[1]) + ')\n')
    assert anchor in T.NATIVE
    (tmp_path / 'native.py').write_text(T.NATIVE.replace(anchor, inject + anchor))
    client = rig.client()
    client.env['CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE'] = mode
    caplog.set_level(logging.INFO, logger=directsdk._session_log.name)
    history = [{'role': 'user', 'content': 'hi'}]
    response = T.step(client, history, stream=stream)
    admission = response.usage.model_dump()['native_admission']
    assert admission['rate_limit'] == (FULL if last_full else {'window': 'overage', 'overage_status': '', 'using_overage': True})
    assert response.choices[0].message.content == 'echo hi'
    logs = [r.getMessage() for r in caplog.records if r.getMessage().startswith('rate limit:')]
    expected_logs = [
        'rate limit: window=five_hour status=allowed_warning utilization=0.875 surpassed=0.80 resets_at=1800000000 overage=allowed using_overage=0',
        'rate limit: window=overage status=- utilization=- surpassed=- resets_at=- overage=- using_overage=1']
    assert logs == (expected_logs[::-1] if last_full else expected_logs)
    assert 'PRIVATE_SENTINEL' not in caplog.text + json.dumps(admission)
    history += [T.as_history(response), {'role': 'user', 'content': 'again'}]
    next_response = T.step(client, history, stream=stream)
    assert 'rate_limit' not in next_response.usage.model_dump()['native_admission']
    assert len([r for r in caplog.records if r.getMessage().startswith('rate limit:')]) == 2
