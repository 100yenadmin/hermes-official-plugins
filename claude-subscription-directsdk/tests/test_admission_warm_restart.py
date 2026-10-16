"""Warm native replacements preserve the completed tool-loop prefix (#84)."""
import copy
import json

import pytest

from admission import _breakpoints, _frame_first, _plain, pin_message_breakpoint

MARKER = {'type': 'ephemeral', 'ttl': '1h'}
CONTEXT = [{'type': 'text', 'text': '<system-reminder>synthetic context</system-reminder>'},
           {'type': 'text', 'text': 'Synthetic account context.'}]


def turn(n, rounds):
    frame = [{'type': 'text', 'text': f'Turn {n}: run the steps.'},
             {'type': 'text', 'text': 'source pack ' * 200 if n == 1 else f'input {n}'}]
    messages = [{'role': 'user', 'content': frame}]
    for k in range(rounds):
        tool_id = f't{n}.{k}'
        messages += [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': f'plan {tool_id}', 'signature': f'sig{tool_id}'},
                {'type': 'tool_use', 'id': tool_id, 'name': 'mcp__hermes__terminal',
                 'input': {'command': f'echo {tool_id}'}}]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': tool_id, 'content': f'result {tool_id}'}]},
        ]
    return messages


def native(messages, launch, dual=False):
    """The process keeps context on its launch frame; replay into a replacement omits it.

    Like the existing turn-start fixtures, native inserts a fixed env message and marks
    its trailing date; newer native also marks the last assistant block.
    """
    sent = copy.deepcopy(messages)
    sent[launch]['content'] = copy.deepcopy(CONTEXT) + sent[launch]['content']
    sent.insert(1, {'role': 'system', 'content': 'Primary working directory: /tmp/synthetic-warm84'})
    if dual and len(sent) > 3:
        sent[-2]['content'][-1]['cache_control'] = copy.deepcopy(MARKER)
    sent.append({'role': 'system', 'content': [
        {'type': 'text', 'text': "Today's date is 2026-10-15.", 'cache_control': copy.deepcopy(MARKER)}]})
    return {'model': 'synthetic', 'messages': sent}


def wire(body):
    return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()


def pinned(body, queried, memory):
    return json.loads(pin_message_breakpoint(wire(body), queried, memory))


def plain_prefix(messages):
    return wire([{'role': m['role'], 'content': [_plain(b) for b in m['content']]
                 if isinstance(m['content'], list) else m['content']} for m in messages])


@pytest.mark.parametrize('rounds', [1, 12], ids=['short-loop', 'long-loop'])
@pytest.mark.parametrize('dual', [False, True], ids=['one-native-mark', 'two-native-marks'])
def test_restart_boundaries_replay_completed_turn_prefix(rounds, dual):
    first, second, third = [turn(n, rounds) for n in (1, 2, 3)]
    p1 = {}
    opening = pinned(native(first[:1], 0, dual), first[0]['content'], p1)
    assert [_plain(b) for b in opening['messages'][0]['content']] == first[0]['content'] + CONTEXT
    p1_last = pinned(native(first + second, 0, dual), second[-1]['content'], p1)

    p2 = {}
    start3 = len(first + second) + 1  # native's env message precedes later turns
    p2_first = pinned(native(first + second + third[:1], len(first + second), dual), third[0]['content'], p2)
    # Every message of P1's last tool-step request, excluding its ephemeral date, recurs.
    assert plain_prefix(p2_first['messages'][:start3]) == plain_prefix(p1_last['messages'][:-1])
    assert p2 == {'context': {'index': start3, 'blocks': CONTEXT}}
    assert [_plain(b) for b in p2_first['messages'][start3]['content']] == third[0]['content']

    p2_last = pinned(native(first + second + third, len(first + second), dual), third[-1]['content'], p2)
    assert plain_prefix(p2_last['messages'][:start3]) == plain_prefix(p1_last['messages'][:-1])
    assert [_plain(b) for b in p2_last['messages'][start3]['content']] == third[0]['content']
    fourth = turn(4, rounds)
    p3 = {}
    start4 = len(first + second + third) + 1
    p3_first = pinned(native(first + second + third + fourth[:1], len(first + second + third), dual),
                      fourth[0]['content'], p3)
    assert plain_prefix(p3_first['messages'][:start4]) == plain_prefix(p2_last['messages'][:-1])
    for body in (p2_first, p2_last, p3_first):
        assert [_plain(b) for b in body['messages'][0]['content']] == first[0]['content'] + CONTEXT
        assert _breakpoints(body) <= 4


def legacy_expected(body, queried, permutation=False):
    """Expected #77 layout and marker folding for these short-loop fixtures."""
    expected = copy.deepcopy(body)
    messages = expected['messages']
    if permutation:
        content = messages[0]['content']
        messages[0]['content'] = content[len(CONTEXT):] + content[:len(CONTEXT)]
    last = max((i for i, m in enumerate(messages) if m['role'] == 'assistant'), default=-1)
    newest = messages[last + 1]
    matches = 0
    for sent, host in zip(newest['content'], queried):
        if _plain(sent) != _plain(host):
            break
        matches += 1
    target = newest['content'][matches - 1] if matches else messages[last]['content'][-1]
    target.setdefault('cache_control', messages[-1]['content'][0].pop('cache_control'))
    return wire(expected)


def test_kept_alive_and_per_call_wire_output_remains_byte_identical():
    first, second, third = [turn(n, 1) for n in (1, 2, 3)]
    memory = {}
    body = native(first[:1], 0)
    assert pin_message_breakpoint(wire(body), first[0]['content'], memory) == legacy_expected(
        body, first[0]['content'], permutation=True)
    for messages in (first, first + second, first + second + third):
        body, queried = native(messages, 0), messages[-1]['content']
        assert pin_message_breakpoint(wire(body), queried, memory) == legacy_expected(body, queried, permutation=True)
    # Per-call mode still reorders only the opening turn, regardless of launch position.
    for messages, launch, queried in ((first[:1], 0, first[0]['content']),
                                     (first + second + third[:1], len(first + second), third[0]['content']),
                                     (first + second + third, len(first + second), third[-1]['content'])):
        body = native(messages, launch)
        assert pin_message_breakpoint(wire(body), queried, None) == legacy_expected(
            body, queried, permutation=len(messages) == 1)


@pytest.mark.parametrize('case', ['missing-frame', 'duplicate-frame', 'frame-first', 'cache-control',
                                 'metadata', 'non-text', 'string-newest', 'string-opening'])
def test_restart_discovery_fails_open(case):
    first, second = turn(1, 1), turn(2, 1)
    body = native(first + second[:1], len(first))
    newest = body['messages'][-2]['content']
    queried = second[0]['content']
    if case == 'missing-frame':
        queried = [{'type': 'text', 'text': 'absent'}]
    elif case == 'duplicate-frame':
        newest += copy.deepcopy(queried)
    elif case == 'frame-first':
        del newest[:len(CONTEXT)]
    elif case == 'cache-control':
        newest[0]['cache_control'] = MARKER
    elif case == 'metadata':
        newest[0]['citations'] = []
    elif case == 'non-text':
        newest[0] = {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.invalid/image'}}
    elif case == 'string-newest':
        body['messages'][-2]['content'] = 'scalar'
    elif case == 'string-opening':
        body['messages'][0]['content'] = 'scalar'
    before = copy.deepcopy(body['messages'])
    memory = {}
    out = pinned(body, queried, memory)
    assert plain_prefix(out['messages']) == plain_prefix(before)
    assert memory['context'] is None if case != 'string-opening' else isinstance(memory['context'], dict)
    # Discovery never retries, even when a later tool frame could otherwise match.
    valid = native(first + second[:1], len(first))['messages']
    if memory['context'] is None:
        assert not _frame_first(valid, second[0]['content'], memory)
        assert memory == {'context': None}


@pytest.mark.parametrize('case', ['short-history', 'wrong-role', 'changed-context', 'already-appended', 'string-content'])
def test_recorded_context_mismatches_change_nothing(case):
    first, second = turn(1, 1), turn(2, 1)
    memory = {}
    pinned(native(first + second[:1], len(first)), second[0]['content'], memory)
    messages = native(first + second, len(first))['messages']
    index = memory['context']['index']
    if case == 'short-history':
        messages = messages[:index]
    elif case == 'wrong-role':
        messages[index]['role'] = 'assistant'
    elif case == 'changed-context':
        messages[index]['content'][0]['text'] += ' changed'
    elif case == 'already-appended':
        messages[0]['content'] += copy.deepcopy(CONTEXT)
    elif case == 'string-content':
        messages[index]['content'] = 'scalar'
    before = copy.deepcopy(messages)
    assert not _frame_first(messages, second[-1]['content'], memory)
    assert messages == before
