"""Claude must be reminded to consult Hermes skills on each new user turn, without touching the cache.

Deep into a session, or after compaction, Claude stops consulting the skills index that sits at the
top of Hermes' system prompt. The provider restates the rule beside the queried user turn. It is
per-request context: Hermes history never contains it, and the cache breakpoint stays on host content.
"""
import json
import os
import sys

import directsdk
from admission import pin_message_breakpoint

NATIVE = r"""
import json, os, sys
if '--version' in sys.argv:
    print('2.1.263 (Claude Code)'); sys.exit()
rows = []
for line in sys.stdin:
    row = json.loads(line); rows.append(row)
    if row.get('shouldQuery') is False:
        print(json.dumps({'type': 'result', 'num_turns': 0, 'is_error': False}), flush=True)
open(os.environ['ROWS_CAPTURE'], 'w').write(json.dumps(rows))
print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}], 'id': 'msg', 'model': 'sonnet', 'stop_reason': 'end_turn'}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'num_turns': 1, 'subtype': 'success', 'is_error': False, 'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)
"""


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'description': name, 'parameters': {'type': 'object', 'properties': {}}}}


def sent(tmp_path, messages, tools):
    script, capture = tmp_path / 'native.py', tmp_path / 'rows.json'
    script.write_text(NATIVE)
    client = directsdk.Client(command=[sys.executable, str(script)], env={'PATH': os.defpath, 'HOME': str(tmp_path), 'ROWS_CAPTURE': str(capture)})
    original = json.loads(json.dumps(messages))
    try:
        client.create(model='sonnet', messages=messages, tools=tools)
    finally:
        client.close()
    assert messages == original, 'caller history mutated'
    return json.loads(capture.read_text())[-1]['message']['content']


def test_fresh_user_turn_carries_the_reminder_after_host_content(tmp_path):
    question = [{'role': 'user', 'content': 'earlier'}, {'role': 'assistant', 'content': 'reply'},
                {'role': 'user', 'content': 'open a PR for my branch'}]
    last = sent(tmp_path, question, [tool('terminal'), tool('skill_view')])
    assert last[0] == {'type': 'text', 'text': 'open a PR for my branch'}
    assert last[1:] == [{'type': 'text', 'text': directsdk.SKILLS_REMINDER}]

    # A tool round inside the turn, or an inventory without skills, is sent exactly as Hermes built it.
    round_ = question + [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': 't1', 'content': 'out'}]
    assert [b['type'] for b in sent(tmp_path, round_, [tool('terminal'), tool('skill_view')])] == ['tool_result']
    assert sent(tmp_path, question, [tool('terminal')]) == [{'type': 'text', 'text': 'open a PR for my branch'}]


def test_cache_breakpoint_stays_on_host_content_not_the_reminder():
    host = {'type': 'text', 'text': 'open a PR for my branch'}
    reminder = {'type': 'text', 'text': directsdk.SKILLS_REMINDER, 'cache_control': {'type': 'ephemeral'}}
    wire = {'model': 'm', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'earlier'}]},
                                       {'role': 'assistant', 'content': [{'type': 'text', 'text': 'reply'}]},
                                       {'role': 'user', 'content': [host, reminder]}]}
    pinned = json.loads(pin_message_breakpoint(json.dumps(wire).encode(), [host]))
    newest = pinned['messages'][-1]['content']
    assert 'cache_control' in newest[0] and 'cache_control' not in newest[1]
