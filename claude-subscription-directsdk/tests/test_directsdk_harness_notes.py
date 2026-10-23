"""Claude-only harness notes at the end of agent steps' system text."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading

import pytest

from conftest import PLUGIN_NAME
import directsdk

# Records the system file and session id it was started with, then answers one query from the fake upstream.
NATIVE = r'''
import json, os, pathlib, sys, urllib.request, urllib.error
system = pathlib.Path(sys.argv[sys.argv.index('--system-prompt-file')+1]).read_text()
pathlib.Path(os.environ['RECORD']).write_text(json.dumps({'system': system, 'session_id': sys.argv[sys.argv.index('--session-id')+1]}))
for line in sys.stdin:
    if json.loads(line).get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}),flush=True)
        continue
    break
body = json.dumps({'messages': []}).encode()
raw = urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL']+'/v1/messages', data=body), timeout=5).read().decode()
for frame in raw.split('\n\n'):
    event = json.loads(frame[6:]) if frame.startswith('data: ') else {}
    if event.get('type') == 'content_block_delta' and event['delta']['type'] == 'text_delta':
        print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':event['delta']}}),flush=True)
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}),flush=True)
tools = 'tool_use' in raw
print(json.dumps({'type':'result','subtype':'error_max_turns' if tools else 'success','usage':{'input_tokens':0,'output_tokens':0}}),flush=True)
'''
USAGE = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
TOOLS = [{'type': 'function', 'function': {'name': 'probe', 'parameters': {'type': 'object'}}}]
CALL = {'type': 'tool_use', 'id': 'toolu_1', 'name': 'mcp__hermes__probe', 'input': {}}


def sse(blocks, stop):
    events = [{'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant', 'model': 'opus', 'content': [], 'usage': USAGE}}]
    for i, block in enumerate(blocks):
        start = dict(block, **({'input': {}} if block['type'] == 'tool_use' else {'text': ''}))
        delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])} if block['type'] == 'tool_use' else
                 {'type': 'text_delta', 'text': block['text']})
        events += [{'type': 'content_block_start', 'index': i, 'content_block': start}, {'type': 'content_block_delta', 'index': i, 'delta': delta},
                   {'type': 'content_block_stop', 'index': i}]
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop}, 'usage': USAGE}, {'type': 'message_stop'}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(directsdk, 'claude_guidance_enabled', lambda: True)
    monkeypatch.setattr(directsdk, 'settle_answers_enabled', lambda: False)
    reply = []
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(reply[0])
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    (tmp_path / 'native.py').write_text(NATIVE)
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}', 'RECORD': str(tmp_path / 'record.json')}
    client = directsdk.Client(command=[sys.executable, str(tmp_path / 'native.py')], env=env)
    def call(blocks, stop='tool_use', messages=None, tools=TOOLS):
        reply[:] = [sse(blocks, stop)]
        return client.create(model='opus', messages=messages or [{'role': 'user', 'content': 'go'}], tools=tools,
                             extra_body={'reasoning': {'enabled': True, 'effort': 'high'}}).choices[0]
    call.record = lambda: json.loads((tmp_path / 'record.json').read_text())
    yield call
    client.close(); peer.shutdown(); peer.server_close()


def test_the_two_settings_default_and_are_declared(profile):
    import directsdk_setup
    from hermes_cli.plugins_settings import plugin_settings_fields
    assert (directsdk_setup.claude_guidance_enabled(), directsdk_setup.settle_answers_enabled()) == (True, False)
    fields = {f['key']: f for f in plugin_settings_fields(PLUGIN_NAME, Path(directsdk_setup.__file__).resolve().parent)}
    assert [(fields[k]['type'], fields[k]['default']) for k in ('claude_guidance', 'settle_answers')] == [('boolean', True), ('boolean', False)]
    assert 'progress_updates' not in fields


@pytest.mark.parametrize('key, value, read', [('claude_guidance', False, 'claude_guidance_enabled'),
                                              ('settle_answers', True, 'settle_answers_enabled')])
def test_each_setting_is_honoured(profile, key, value, read):
    import directsdk_setup
    from hermes_cli.config import get_config_path
    get_config_path().write_text('plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        %s: %s\n'
                                 % (PLUGIN_NAME, key, json.dumps(value)), encoding='utf-8')
    assert getattr(directsdk_setup, read)() is value


def test_the_harness_notes_close_the_system_text_of_agent_steps(run, monkeypatch):
    messages = [{'role': 'system', 'content': 'You are eva.'}, {'role': 'user', 'content': 'go'}]
    run([CALL], messages=messages)
    first = run.record()
    assert first['system'] == 'You are eva.\n\n' + directsdk.GUIDANCE and first['system'].endswith('in your final message.')
    frames = directsdk.prepare_history(messages, {'probe': 'probe'})[1]
    assert first['session_id'] == directsdk.conversation_session_id(first['system'], frames)
    run([CALL], messages=messages + [{'role': 'assistant', 'content': 'hi'}, {'role': 'user', 'content': 'again'}])
    assert run.record() == first  # stable across the calls of one conversation
    monkeypatch.setattr(directsdk, 'settle_answers_enabled', lambda: True)
    run([CALL], messages=messages)
    settled = run.record()
    assert settled['system'] == first['system'] + directsdk.SETTLE and settled['session_id'] != first['session_id']
    monkeypatch.setattr(directsdk, 'claude_guidance_enabled', lambda: False)
    run([CALL], messages=messages)
    assert run.record()['system'] == 'You are eva.'


def test_requests_without_tools_get_no_harness_notes(run):
    run([{'type': 'text', 'text': 'A title'}], stop='end_turn', tools=None,
        messages=[{'role': 'system', 'content': 'Title this.'}, {'role': 'user', 'content': 'go'}])
    assert run.record()['system'] == 'Title this.'
