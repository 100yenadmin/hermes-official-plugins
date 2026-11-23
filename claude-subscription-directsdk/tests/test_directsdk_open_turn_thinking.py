"""A cold session spawn whose history ends inside an open turn can replay that turn without its thinking blocks
(setting ``drop_open_turn_thinking``, default off), and the relay queues `session cold:` notes for a native's first
requests and for the first later request that opens a user turn. Probable cause the drop answers: reproduced once at
~349k, 5 of 6 in eva's logs, API mechanism unproven."""
import json
import re
from types import SimpleNamespace

import admission
import directsdk
import directsdk_setup


def user(text):
    return {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}}


def results(*ids):
    return {'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': i, 'content': [{'type': 'text', 'text': 'r'}]} for i in ids]}}


def assistant(*blocks, mid='m'):
    return {'type': 'assistant', 'message': {'id': mid, 'role': 'assistant', 'content': list(blocks)}}


THINK = {'type': 'thinking', 'thinking': 'secret plan', 'signature': 'sig-a'}
REDACTED = {'type': 'redacted_thinking', 'data': 'opaque'}


def call(i):
    return {'type': 'tool_use', 'id': i, 'name': 'mcp__hermes__x', 'input': {}}


def text(t):
    return {'type': 'text', 'text': t}


def test_an_open_turn_loses_its_thinking_and_closed_turns_keep_theirs():
    closed = assistant(THINK, text('earlier answer'), mid='c')
    frames = [user('q1'), closed, user('q2'),
              assistant(THINK, text('looking'), call('t1'), mid='a1'), results('t1'),
              assistant(REDACTED, call('t2'), mid='a2'), results('t2')]
    before = json.dumps(frames)
    out, dropped = directsdk.drop_open_turn_thinking(frames)
    assert dropped == 2
    assert json.dumps(frames) == before  # the caller's frames are not mutated
    assert out[1] == closed  # a closed turn is untouched, signature and all
    assert [b['type'] for b in out[3]['message']['content']] == ['text', 'tool_use']
    assert [b['type'] for b in out[5]['message']['content']] == ['tool_use']
    assert out[-1] == frames[-1] and len(out) == len(frames)


def test_interleaved_thinking_only_frames_are_dropped_whole():
    # stream-json carries one block per assistant message: a thinking-only frame between tool rounds goes away
    frames = [user('q'), assistant(THINK, mid='a'), assistant(call('t1'), mid='a'), results('t1'),
              assistant({**THINK, 'signature': 'sig-b'}, mid='b'), assistant(call('t2'), mid='b'), results('t2')]
    out, dropped = directsdk.drop_open_turn_thinking(frames)
    assert dropped == 2
    assert [f['type'] for f in out] == ['user', 'assistant', 'user', 'assistant', 'user']
    assert all(b['type'] != 'thinking' for f in out for b in f['message']['content'])


def test_a_history_ending_at_a_user_turn_or_without_thinking_is_returned_as_given():
    at_user = [user('q1'), assistant(THINK, text('a')), user('q2')]
    assert directsdk.drop_open_turn_thinking(at_user) == (at_user, 0)
    plain = [user('q'), assistant(text('x'), call('t1')), results('t1')]
    out, dropped = directsdk.drop_open_turn_thinking(plain)
    assert dropped == 0 and out is plain
    only_results = [results('t0')]
    assert directsdk.drop_open_turn_thinking(only_results) == (only_results, 0)


def test_a_user_row_with_text_beside_tool_results_opens_the_turn():
    mixed = {'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't0', 'content': 'r'}, {'type': 'text', 'text': 'and also'}]}}
    frames = [user('q'), assistant(THINK, call('t0')), mixed, assistant(THINK, call('t1')), results('t1')]
    out, dropped = directsdk.drop_open_turn_thinking(frames)
    assert dropped == 1
    assert out[1]['message']['content'][0] == THINK  # before the mixed row: a closed turn


def test_the_setting_defaults_off(monkeypatch):
    monkeypatch.setattr(directsdk_setup, '_setting', lambda key, default=None: default)
    assert directsdk_setup.drop_open_turn_thinking_enabled() is False


class _Stop(Exception):
    pass


def _spawn(monkeypatch, *, cold, enabled):
    seen = {}
    monkeypatch.setattr(directsdk, 'drop_open_turn_thinking_enabled', lambda: enabled)
    monkeypatch.setattr(directsdk, 'relay', lambda *a, **k: SimpleNamespace(url='http://127.0.0.1:9', parity_prev=None))

    def fake_command(*a, **k):
        raise _Stop

    monkeypatch.setattr(directsdk, 'native_command', fake_command)
    real = directsdk.drop_open_turn_thinking
    monkeypatch.setattr(directsdk, 'drop_open_turn_thinking', lambda frames: seen.setdefault('out', real(frames)))
    frames = [user('q'), assistant(THINK, call('t1')), results('t1')]
    session = SimpleNamespace()
    client = directsdk.Client(env={})
    try:
        client._session_spawn(session, None, {'model': 'm'}, '{}', None, {}, [], frames, 1, {}, ['claude'], None, cold=cold)
    except _Stop:
        pass
    return seen


def test_only_a_cold_spawn_with_the_setting_on_drops(monkeypatch):
    assert _spawn(monkeypatch, cold=True, enabled=True)['out'][1] == 1
    assert _spawn(monkeypatch, cold=False, enabled=True) == {}  # a rebuild keeps the rows its retired native cached
    assert _spawn(monkeypatch, cold=True, enabled=False) == {}


SAFE = re.compile(r'^[a-z_]+=[a-z0-9_\-/:.,]+$')


def gate():
    return SimpleNamespace(first_turn={}, cold_seen=0, cold_notes=[], cold_turn_logged=False, cold_system=None)


def payload(messages, **extra):
    return json.dumps({'system': [{'type': 'text', 'text': 'sys text', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
                       'thinking': {'type': 'adaptive', 'display': 'summarized'}, 'messages': messages, **extra}).encode()


def msg(role, *blocks):
    return {'role': role, 'content': list(blocks)}


MARK = {'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}


def test_cold_notes_cover_the_first_two_requests_and_the_next_user_turn():
    g = gate()
    open_turn = [msg('user', text('q1')), msg('assistant', THINK, text('a')), msg('user', text('q2')),
                 msg('assistant', THINK, call('t1')), msg('user', {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'r', **MARK})]
    admission.observe_cold(g, payload(open_turn))
    more = open_turn + [msg('assistant', call('t2')), msg('user', {'type': 'tool_result', 'tool_use_id': 't2', 'content': 'r'})]
    admission.observe_cold(g, payload(more))
    admission.observe_cold(g, payload(more + [msg('assistant', call('t3')), msg('user', {'type': 'tool_result', 'tool_use_id': 't3', 'content': 'r'})]))
    closed = more + [msg('assistant', text('done')), msg('user', {**text('q3'), **MARK})]
    admission.observe_cold(g, payload(closed))
    admission.observe_cold(g, payload(closed + [msg('assistant', text('x')), msg('user', text('q4'))]))
    assert [n.split()[0] for n in g.cold_notes] == ['n=1', 'n=2', 'n=4']
    first, _, turn = g.cold_notes
    assert 'opens=tool' in first and 'open_think=1' in first and 'hist_think=2' in first and 'marks=s0:1h,m4.0:1h' in first
    assert 'think=adaptive:summarized' in first and re.search(r'sys=\d+ ', first) and 'sys_changed=-' in first
    assert 'opens=user' in turn and 'open_think=0' in turn and 'closed_think=1' in turn and 'marks=s0:1h,m4.0:1h,m8.0:1h' in turn and 'rows=9' in turn and 'sys_changed=none' in turn
    for note in g.cold_notes:
        assert 'secret plan' not in note and 'sys text' not in note and 'sig-a' not in note
        assert all(field.startswith('n=') or SAFE.match(field) for field in note.split()), note


def test_a_second_request_that_opens_a_user_turn_ends_the_notes():
    g = gate()
    base = [msg('user', text('q1'))]
    admission.observe_cold(g, payload(base))
    admission.observe_cold(g, payload(base + [msg('assistant', text('a')), msg('user', text('q2'))]))
    admission.observe_cold(g, payload(base + [msg('assistant', text('a')), msg('user', text('q2')), msg('assistant', text('b')), msg('user', text('q3'))]))
    assert [n.split()[0] for n in g.cold_notes] == ['n=1', 'n=2']


def test_a_per_call_gate_or_a_bad_payload_logs_nothing():
    g = SimpleNamespace(first_turn=None, cold_notes=[])
    admission.observe_cold(g, payload([msg('user', text('q'))]))
    assert g.cold_notes == []
    g = gate()
    admission.observe_cold(g, b'not json')
    assert g.cold_notes == []


def test_a_changed_system_block_is_named_by_index_without_its_digest():
    g = gate()
    first = json.loads(payload([msg('user', text('q1'))]))
    admission.observe_cold(g, json.dumps(first).encode())
    first['system'][0]['text'] = 'sys tex2'  # same length, other bytes
    first['messages'] = first['messages'] + [msg('assistant', text('a')), msg('user', text('q2'))]
    admission.observe_cold(g, json.dumps(first).encode())
    assert g.cold_notes[0].endswith('sys_changed=-') and g.cold_notes[1].endswith('sys_changed=0')
    assert not re.search(r'[0-9a-f]{8}', ' '.join(g.cold_notes))
