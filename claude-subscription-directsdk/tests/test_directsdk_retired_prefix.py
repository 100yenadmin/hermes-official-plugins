"""A rebuilt native's first request marks where the retired native's last request ended, so the API's ~20-block
lookback finds that request's cache entry instead of rewriting the whole conversation."""
import json
from types import SimpleNamespace

import admission

MARK = {'type': 'ephemeral', 'ttl': '1h'}


def convo(n, tail=0, marked_end=True):
    rows = []
    for i in range(n):
        rows.append({'role': 'user' if i % 2 == 0 else 'assistant', 'content': [{'type': 'text', 'text': f'row {i} ' * 5}]})
    for i in range(tail):
        rows.append({'role': 'user' if (n + i) % 2 == 0 else 'assistant', 'content': [{'type': 'text', 'text': f'tail {i}'}]})
    if marked_end:
        rows[-1]['content'][-1]['cache_control'] = dict(MARK)
    return rows


def gate_for(old_messages):
    return SimpleNamespace(first_turn={}, parity_prev=(123, admission.row_profile(old_messages)))


def body(messages, system_marked=True):
    b = {'model': 'm', 'messages': messages}
    if system_marked:
        b['system'] = [{'type': 'text', 'text': 'sys', 'cache_control': dict(MARK)}]
    return json.dumps(b).encode()


def test_a_rebuild_marks_the_end_of_the_retired_natives_last_request():
    old = convo(9)
    gate = gate_for(old)
    out = json.loads(admission.mark_retired_prefix(gate, body(convo(9, tail=31))))
    assert out['messages'][8]['content'][-1]['cache_control'] == MARK  # the retired request's last row, same TTL
    assert out['messages'][-1]['content'][-1]['cache_control'] == MARK  # native's own mark stays
    assert gate.parity_prev is not None  # observe_parity still gets the hand-over for its log line


def test_no_mark_when_rows_changed_or_nothing_follows_or_marks_are_full_or_no_handover():
    old = convo(9)
    changed = convo(9, tail=31)
    changed[4]['content'][0]['text'] += ' edited'
    for gate, payload in (
            (gate_for(old), body(changed)),                       # a row of the retired request changed
            (gate_for(old), body(convo(9))),                      # nothing after it: native's own mark is already there
            (SimpleNamespace(first_turn={}, parity_prev=None), body(convo(9, tail=31))),  # not a rebuild
            (SimpleNamespace(first_turn=None, parity_prev=(1, admission.row_profile(old))), body(convo(9, tail=31))),  # per call
    ):
        assert admission.mark_retired_prefix(gate, payload) is payload
    full = convo(9, tail=31)
    for i in (20, 25):
        full[i]['content'][-1]['cache_control'] = dict(MARK)
    assert admission.mark_retired_prefix(gate_for(old), body(full)) == body(full)  # 4 marks already: unchanged
    assert admission.mark_retired_prefix(gate_for(old), b'not json') == b'not json'


def test_the_relay_runs_the_mark_before_observing_parity():
    import inspect
    source = inspect.getsource(admission.Handler.do_POST)
    assert source.index('mark_retired_prefix(gate, payload)') < source.index('observe_parity(gate, payload)')


def test_the_copied_mark_keeps_ttls_non_increasing_and_ignores_schema_fields():
    old = convo(9)
    msgs = convo(9, tail=31, marked_end=False)
    msgs[-1]['content'][-1]['cache_control'] = {'type': 'ephemeral', 'ttl': '5m'}  # native's end mark, 5m
    b = {'model': 'm', 'messages': msgs, 'system': [{'type': 'text', 'text': 's', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
         'tools': [{'name': 't', 'input_schema': {'type': 'object', 'properties': {'cache_control': {'type': 'string'}}}}]}
    out = json.loads(admission.mark_retired_prefix(gate_for(old), json.dumps(b).encode()))
    # Copies the next genuine mark (5m), never the earlier 1h system mark and never the schema property.
    assert out['messages'][8]['content'][-1]['cache_control'] == {'type': 'ephemeral', 'ttl': '5m'}
    # Same JSON key order reversed: the result does not depend on serialization order.
    rev = json.dumps({k: b[k] for k in ('tools', 'system', 'messages', 'model')}).encode()
    assert json.loads(admission.mark_retired_prefix(gate_for(old), rev))['messages'][8]['content'][-1]['cache_control'] == {'type': 'ephemeral', 'ttl': '5m'}


def test_no_genuine_mark_anywhere_means_no_new_mark():
    old = convo(9)
    payload = json.dumps({'model': 'm', 'messages': convo(9, tail=31, marked_end=False)}).encode()
    assert admission.mark_retired_prefix(gate_for(old), payload) is payload
