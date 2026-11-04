"""The opening request must cache a prefix that the second request replays (#77).

In subscription mode native prepends its per-request context block (the account-email
reminder) to the conversation's first user turn, but replays that turn without it on every
later request. Unless the relay sends Hermes' own frame alone, call #2 re-writes the whole
first turn, and Claude reads native's context as part of the user's message (#195). These
fixtures use native's real 2.1.280 message layout.
"""
import copy
import json

import pytest

from admission import _frame_first, pin_message_breakpoint

MARKER = {'type': 'ephemeral', 'ttl': '1h'}
REMINDER = {'type': 'text', 'text': "<system-reminder>\nAs you answer the user's questions, you can use the "
                                    "following context:\n# userEmail\nThe user's email address is a@example.invalid.\n"
                                    '</system-reminder>\n'}
FRAME = {'type': 'text', 'text': 'Use the terminal tool to run `echo alpha`.' + ' source pack' * 200}
ENV = 'Primary working directory: /tmp/claude-directsdk-cwd-501\nPlatform: darwin'


def call1():
    """Native's opening request: reminder ahead of Hermes' frame, marker on the env/date message."""
    return [
        {'role': 'user', 'content': [REMINDER, FRAME]},
        {'role': 'system', 'content': [{'type': 'text', 'text': ENV + "\n\nToday's date is 2026-09-30.",
                                        'cache_control': MARKER}]},
    ]


def call2():
    """Native's second request: the same first turn replayed bare, one tool round, the date after it."""
    return [
        {'role': 'user', 'content': [FRAME]},
        {'role': 'system', 'content': ENV},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'mcp__hermes__terminal',
                                           'input': {'command': 'echo alpha'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1',
                                      'content': 'alpha\n\n' + REMINDER['text']}]},
        {'role': 'system', 'content': [{'type': 'text', 'text': "Today's date is 2026-09-30.",
                                        'cache_control': MARKER}]},
    ]


def wire(messages):
    return json.dumps({'model': 'm', 'messages': messages}).encode()


def cached_prefix(payload):
    """What the API caches for this request: every message block up to and including the marker."""
    blocks = []
    for message in json.loads(payload)['messages']:
        content = message['content']
        for block in ([{'type': 'text', 'text': content}] if isinstance(content, str) else content):
            plain = {k: v for k, v in block.items() if k != 'cache_control'}
            blocks.append((message['role'], json.dumps(plain, sort_keys=True)))
            if 'cache_control' in block:
                return blocks
    return None


def replayed(payload):
    body = json.loads(payload)
    out = []
    for message in body['messages']:
        content = message['content']
        for block in ([{'type': 'text', 'text': content}] if isinstance(content, str) else content):
            plain = {k: v for k, v in block.items() if k != 'cache_control'}
            out.append((message['role'], json.dumps(plain, sort_keys=True)))
    return out


def test_first_request_caches_a_prefix_the_second_request_replays():
    first = pin_message_breakpoint(wire(call1()), [FRAME])
    second = pin_message_breakpoint(wire(call2()), [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'alpha'}])

    prefix = cached_prefix(first)
    assert prefix is not None
    # the whole cached span of call #1 recurs, block for block, at the head of call #2
    assert replayed(second)[:len(prefix)] == prefix
    # and it covers Hermes' first turn, the part that was being re-written
    assert prefix[-1] == ('user', json.dumps(FRAME, sort_keys=True))


def test_first_turn_is_sent_as_later_requests_replay_it_with_one_marker():
    out = json.loads(pin_message_breakpoint(wire(call1()), [FRAME]))
    first_turn = out['messages'][0]['content']
    # native's context is dropped from the user's turn, as every later request replays it
    assert first_turn == [{**FRAME, 'cache_control': MARKER}]
    assert sum('cache_control' in b for m in out['messages'] if isinstance(m['content'], list)
               for b in m['content']) == 1
    assert out['messages'][1]['content'][0]['text'].startswith(ENV)


@pytest.mark.parametrize('messages,queried', [
    # a later turn: there is history, the first turn is left exactly as native sent it
    (call2()[:3] + [{'role': 'user', 'content': [REMINDER, FRAME]}], [FRAME]),
    # the frame occurs twice: ambiguous, forward as is
    ([{'role': 'user', 'content': [REMINDER, FRAME, FRAME]}], [FRAME]),
    # something other than text ahead of the frame is not native's context block
    ([{'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='}}, FRAME]}], [FRAME]),
    # the frame is not in the turn at all (native rewrote it)
    ([{'role': 'user', 'content': [REMINDER, {'type': 'text', 'text': 'rewritten'}]}], [FRAME]),
])
def test_first_turn_is_only_changed_when_the_frame_is_unambiguous(messages, queried):
    messages = copy.deepcopy(messages)
    messages[-1]['content'][-1] = {**messages[-1]['content'][-1], 'cache_control': MARKER}
    raw = wire(messages)
    out = json.loads(pin_message_breakpoint(raw, queried))
    for sent, got in zip(json.loads(raw)['messages'], out['messages']):
        strip = lambda c: [{k: v for k, v in b.items() if k != 'cache_control'} for b in c] if isinstance(c, list) else c
        assert strip(got['content']) == strip(sent['content'])


def test_later_request_without_memory_keeps_reminder_first():
    messages = call1()[:1] + call2()[2:]
    payload = wire(messages)
    out = json.loads(pin_message_breakpoint(payload, [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'alpha'}]))
    assert out['messages'][0]['content'] == [REMINDER, FRAME]


@pytest.mark.parametrize('changed', [False, True])
def test_first_turn_memory_requires_identical_plain_content(changed):
    memory = {}
    pin_message_breakpoint(wire(call1()), [FRAME], memory)
    assert set(memory) == {'permutation', 'sha256'}
    assert memory['permutation'] == (1, 1) and len(memory['sha256']) == 64
    messages = copy.deepcopy(call1()[:1] + call2()[2:])
    # Markers and JSON key order do not change the original plain content's identity.
    messages[0]['content'][0] = dict(reversed(list(messages[0]['content'][0].items())), cache_control=MARKER)
    if changed:
        messages[0]['content'][0]['text'] += ' changed'
    before = copy.deepcopy(messages)
    assert _frame_first(messages, [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'alpha'}], memory) is (not changed)
    expected = before[0]['content'] if changed else [{**before[0]['content'][1], 'cache_control': MARKER}]
    assert messages[0]['content'] == expected
    assert messages[1:] == before[1:]


@pytest.mark.parametrize('marked', [False, True])
def test_drop_multiple_plain_prefixes_preserves_frame_marker_and_skills(marked):
    from admission import _breakpoints
    from directsdk import SKILLS_REMINDER
    frame = [{**FRAME, 'cache_control': MARKER}, {'type': 'text', 'text': SKILLS_REMINDER}]
    leading = [dict(REMINDER), {'type': 'text', 'text': 'native memory'}]
    if marked:
        leading[0]['cache_control'] = {'type': 'ephemeral'}
    body = {'messages': [{'role': 'user', 'content': leading + frame}],
            'system': [{'type': 'text', 'text': 'system', 'cache_control': MARKER}],
            'tools': [{'name': 'probe', 'cache_control': MARKER}]}
    out = json.loads(pin_message_breakpoint(json.dumps(body).encode(), frame))
    assert out['messages'][0]['content'] == frame
    assert _breakpoints(out) == 3


@pytest.mark.parametrize('prefix', [REMINDER, {**REMINDER, 'cache_control': MARKER}])
def test_drop_unmarked_opening_or_transfer_dropped_marker(prefix):
    out = json.loads(pin_message_breakpoint(wire([{'role': 'user', 'content': [prefix, FRAME]}]), [FRAME]))
    expected = [{**FRAME, **({'cache_control': MARKER} if 'cache_control' in prefix else {})}]
    assert out['messages'][0]['content'] == expected


@pytest.mark.parametrize('prefix', [
    {**REMINDER, 'citations': []}, {'type': 'image', 'source': {}}, None,
])
def test_non_plain_opening_prefix_forwards_unchanged(prefix):
    raw = wire([{'role': 'user', 'content': [prefix, FRAME]}])
    assert pin_message_breakpoint(raw, [FRAME]) == raw
    # The plain counterpart must be dropped; rejecting every prefix is not a fix.
    plain = json.loads(pin_message_breakpoint(wire([{'role': 'user', 'content': [REMINDER, FRAME]}]), [FRAME]))
    assert plain['messages'][0]['content'] == [FRAME]
