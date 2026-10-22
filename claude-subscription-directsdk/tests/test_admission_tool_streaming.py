"""Tool arguments stream as the model writes them, so a long tool call is not mistaken for a stall.

Native sets ``eager_input_streaming`` only when it talks to a first-party base URL; behind the relay it
leaves the flag off and the API buffers a large argument to the end (measured live: one 89.8 s gap
while Opus wrote a 2,000-word ``write_file`` argument).
"""
import json

from admission import stream_tool_input

TOOLS = [{'name': 'mcp__hermes__write_file', 'input_schema': {'type': 'object'}},
         {'name': 'mcp__hermes__read_file', 'input_schema': {'type': 'object'}, 'eager_input_streaming': False}]


def test_every_tool_asks_for_streamed_arguments_and_native_values_win():
    out = json.loads(stream_tool_input(json.dumps({'model': 'm', 'tools': TOOLS, 'messages': []}).encode()))
    assert [t.get('eager_input_streaming') for t in out['tools']] == [True, False]
    assert [{k: v for k, v in t.items() if k != 'eager_input_streaming'} for t in out['tools']] == \
        [{k: v for k, v in t.items() if k != 'eager_input_streaming'} for t in TOOLS]


def test_requests_without_tools_or_unparseable_forward_byte_identical():
    for raw in (json.dumps({'model': 'm', 'messages': []}).encode(), b'not json',
                json.dumps({'model': 'm', 'tools': [dict(TOOLS[1])], 'messages': []}).encode()):
        assert stream_tool_input(raw) is raw
