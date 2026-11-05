"""contrib/claude_allowance_watch.py: the no_agent cron script that tells the owner when the Claude weekly
allowance runs low. The reader is faked (a stub plugin where the import path matters); nothing is sent."""
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'contrib' / 'claude_allowance_watch.py'
_spec = importlib.util.spec_from_file_location('claude_allowance_watch', SCRIPT)
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)

T0 = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
RESET = '2026-11-10T16:00:00.365322+00:00'
NEXT_RESET = '2026-11-17T16:00:00+00:00'
STUB_PLUGIN = '''
def fetch_usage(timeout=8):
    return {'limits': [{'kind': 'weekly_all', 'group': 'weekly', 'percent': 76,
                        'resets_at': '2026-11-10T16:00:00+00:00', 'scope': None}]}, None
'''


def reply(percent, resets=RESET, kind='weekly_all', group='weekly', scoped=None, extra=()):
    entry = {'kind': kind, 'group': group, 'percent': percent, 'resets_at': resets, 'scope': None}
    if scoped:
        entry['scope'] = {'model': {'display_name': scoped}, 'surface': None}
    return {'limits': [entry, *extra]}, None


class Tick:
    """One cron tick per call against a temp profile home, with a scripted reader; returns what it printed."""

    def __init__(self, home, capsys):
        self.home, self.capsys, self.now, self.reads = home, capsys, T0, 0

    def __call__(self, answer, minutes=0):
        self.now += timedelta(minutes=minutes)

        def fetch():
            self.reads += 1
            if isinstance(answer, Exception):
                raise answer
            return answer

        assert watch.main(home=self.home, now=self.now, fetch=fetch) == 0
        return self.capsys.readouterr().out.strip()

    @property
    def state(self):
        return json.loads((self.home / 'cache' / 'claude_allowance_watch' / 'state.json').read_text())


@pytest.fixture
def tick(tmp_path, capsys):
    return Tick(tmp_path, capsys)


def test_a_quiet_week_prints_nothing(tick):
    assert tick(reply(40)) == ''
    assert tick(reply(74), minutes=60) == ''


def test_75_percent_is_told_once(tick):
    out = tick(reply(76))
    assert out.startswith('Heads-up: eva has used 76% of her Claude weekly limit (resets ')
    assert out.count('\n') == 0
    assert tick(reply(77), minutes=10) == ''
    assert tick(reply(89), minutes=10) == ''


def test_90_then_used_up_then_silence(tick):
    assert '76%' in tick(reply(76))
    assert '91% of her Claude weekly limit' in tick(reply(91), minutes=10)
    used_up = tick(reply(100), minutes=10)
    assert used_up.startswith('eva has used up her Claude weekly limit (resets ')
    assert used_up.endswith("She can't answer on Claude until it resets.")
    assert tick(reply(100), minutes=10) == ''


def test_a_jump_past_two_thresholds_sends_one_line_and_no_late_75(tick):
    assert tick(reply(60)) == ''
    out = tick(reply(92), minutes=20)
    assert out.count('\n') == 0 and '92%' in out
    assert tick(reply(93), minutes=10) == ''


def test_a_new_reset_period_rearms_and_old_periods_are_forgotten(tick):
    assert '76%' in tick(reply(76))
    # Past the old reset: the new week's figure is low, then crosses 75% again.
    assert tick(reply(5, resets=NEXT_RESET), minutes=6 * 24 * 60) == ''
    assert all(NEXT_RESET[:10] not in key for key in tick.state['notified'])
    assert tick.state['notified'] == {}
    assert '80%' in tick(reply(80, resets=NEXT_RESET), minutes=60)


def test_reset_time_drift_does_not_rearm(tick):
    assert '76%' in tick(reply(76, resets='2026-11-10T16:00:00.365322+00:00'))
    assert tick(reply(77, resets='2026-11-10T15:59:59.9+00:00'), minutes=10) == ''
    assert tick(reply(78, resets='2026-11-10T16:00:01Z'), minutes=10) == ''


def test_the_session_limit_is_left_to_the_chat(tick):
    session = {'kind': 'session', 'group': 'session', 'percent': 100, 'resets_at': RESET}
    assert tick(reply(10, extra=[session])) == ''


def test_a_model_scoped_limit_keeps_its_name(tick):
    out = tick(reply(80, kind='weekly_scoped', scoped='Fable', extra=[
        {'kind': 'weekly_all', 'group': 'weekly', 'percent': 30, 'resets_at': RESET}]))
    assert 'used 80% of her Claude Fable weekly limit' in out
    assert out.count('\n') == 0


@pytest.mark.parametrize('percent', [None, float('nan'), True, '95', {'value': 95}])
def test_an_unreadable_percent_is_never_read_as_zero(tick, percent):
    assert tick(reply(percent)) == ''
    assert tick.state['last_error'] == 'the reply had no weekly limits'
    assert tick.state['failing_since'] is not None
    assert 'notified' not in tick.state


@pytest.mark.parametrize('answer', [(None, 'Claude Code did not answer in time'), ({'limits': 'odd'}, None),
                                    ({}, None), ('odd', None)])
def test_failed_readings_tell_once_after_two_hours_then_daily(tick, answer):
    assert tick(answer) == ''
    assert tick(answer, minutes=60) == ''
    told = tick(answer, minutes=60)
    assert told.startswith("eva's Claude usage check has not worked since ")
    assert told.endswith('so there will be no early warning before her weekly limit runs out.')
    assert tick(answer, minutes=60) == ''
    assert tick(answer, minutes=20 * 60) == ''
    assert tick(answer, minutes=4 * 60).startswith("eva's Claude usage check has not worked since ")


def test_a_good_reading_clears_a_failure(tick):
    failure = (None, 'Claude Code did not answer in time')
    assert tick(failure) == '' and tick(failure, minutes=60) == ''
    assert tick(reply(40), minutes=60) == ''
    assert tick.state['failing_since'] is None and tick.state['last_error'] is None
    assert tick(failure, minutes=60) == ''
    assert tick(failure, minutes=60) == ''  # the grace starts again from the new failure


def test_a_crashing_reader_is_reported_by_type_only(tick):
    assert tick(RuntimeError('token sk-ant-secret in a message')) == ''
    assert tick.state['last_error'] == 'RuntimeError while reading'
    assert 'secret' not in json.dumps(tick.state)


@pytest.mark.parametrize('worst, wait, reads', [(40, 30, 1), (40, 60, 2), (55, 10, 1), (55, 20, 2), (72, 10, 2)])
def test_readings_follow_the_worst_limit(tick, worst, wait, reads):
    tick(reply(worst))
    tick(reply(worst), minutes=wait)
    assert tick.reads == reads


def test_an_unwritable_state_is_silent_and_still_exits_0(tmp_path, capsys):
    (tmp_path / 'cache').write_text('not a directory')
    assert watch.main(home=tmp_path, now=T0, fetch=lambda: reply(95)) == 0
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'claude_allowance_watch:' in captured.err


def test_a_corrupt_state_starts_over(tick):
    path = tick.home / 'cache' / 'claude_allowance_watch' / 'state.json'
    path.parent.mkdir(parents=True)
    path.write_text('{not json')
    assert '76%' in tick(reply(76))


def _stub_plugin(home):
    plugin = home / 'plugins' / 'claude-subscription-directsdk-experimental'
    plugin.mkdir(parents=True)
    (plugin / 'directsdk_setup.py').write_text(STUB_PLUGIN)
    return plugin


@pytest.fixture
def fresh_import(monkeypatch):
    """Import the stub as ``directsdk_setup`` and put back whatever the rest of the suite had."""
    monkeypatch.setattr(sys, 'path', list(sys.path))
    prior = sys.modules.pop('directsdk_setup', None)
    yield
    sys.modules.pop('directsdk_setup', None)
    if prior is not None:
        sys.modules['directsdk_setup'] = prior


def test_the_profile_plugin_is_the_reader(tmp_path, capsys, fresh_import):
    _stub_plugin(tmp_path)
    assert watch.main(home=tmp_path, now=T0) == 0
    assert '76% of her Claude weekly limit' in capsys.readouterr().out


def test_a_profile_without_the_plugin_says_so(tmp_path):
    assert watch.plugin_fetch(tmp_path)() == (None, 'the Claude plugin is not installed in this profile')


def test_it_runs_as_cron_runs_it(tmp_path):
    """Cron runs ``python $HERMES_HOME/scripts/<script>``; the profile home comes from the script's place."""
    _stub_plugin(tmp_path)
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    run = subprocess.run([sys.executable, str(scripts / SCRIPT.name)], cwd=scripts, capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith('Heads-up: eva has used 76% of her Claude weekly limit')
    second = subprocess.run([sys.executable, str(scripts / SCRIPT.name)], cwd=scripts, capture_output=True,
                            text=True, timeout=60)
    assert second.returncode == 0 and second.stdout == ''
