"""Opus 5.5 / Fable 5.1 progress updates reach Hermes as interim content; Claude-only harness notes for agent steps."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import urllib.request

import pytest

from conftest import PLUGIN_NAME
import admission
import directsdk

# Claude Code's own gate: no `thinking` in the extra body -> it asks for display "updates" itself.
NATIVE = r'''
import json, os, pathlib, sys, urllib.request, urllib.error
extra = json.loads(json.loads(pathlib.Path(sys.argv[sys.argv.index('--settings')+1]).read_text())['env']['CLAUDE_CODE_EXTRA_BODY'])
system = pathlib.Path(sys.argv[sys.argv.index('--system-prompt-file')+1]).read_text()
pathlib.Path(os.environ['RECORD']).write_text(json.dumps({'system': system, 'session_id': sys.argv[sys.argv.index('--session-id')+1]}))
for line in sys.stdin:
    if json.loads(line).get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}),flush=True)
        continue
    break
body = json.dumps({'thinking': extra.get('thinking', {'type':'adaptive','display':'updates'}), 'messages': []}).encode()
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
UPDATE = {'type': 'thinking', 'thinking': 'Checking the logs first.', 'signature': 'sig-update'}
REASONING = {'type': 'thinking', 'thinking': '', 'signature': 'sig-reasoning'}
CALL = {'type': 'tool_use', 'id': 'toolu_1', 'name': 'mcp__hermes__probe', 'input': {}}


def sse(blocks, stop, details=None):
    events = [{'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant', 'model': 'opus', 'content': [], 'usage': USAGE}}]
    for i, block in enumerate(blocks):
        start = dict(block, **{'tool_use': {'input': {}}, 'thinking': {'thinking': '', 'signature': ''}, 'text': {'text': ''}}[block['type']])
        delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])} if block['type'] == 'tool_use' else
                 {'type': 'text_delta', 'text': block['text']} if block['type'] == 'text' else {'type': 'thinking_delta', 'thinking': block['thinking']})
        events += [{'type': 'content_block_start', 'index': i, 'content_block': start}, {'type': 'content_block_delta', 'index': i, 'delta': delta}]
        if block['type'] == 'thinking':
            events.append({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'signature_delta', 'signature': block['signature']}})
        events.append({'type': 'content_block_stop', 'index': i})
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop, **({'stop_details': details} if details else {})}, 'usage': USAGE}, {'type': 'message_stop'}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(directsdk, 'progress_updates_enabled', lambda: True)
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
    def call(blocks, stop='tool_use', model='opus', reasoning=True, details=None, stream=False, messages=None, tools=TOOLS):
        reply[:] = [sse(blocks, stop, details)]
        extra = {'reasoning': {'enabled': True, 'effort': 'high'} if reasoning else {'enabled': False}}
        result = client.create(model=model, messages=messages or [{'role': 'user', 'content': 'go'}], tools=tools, extra_body=extra, stream=stream)
        if stream:
            chunks = list(result)
            return chunks[-1]._response.choices[0], ''.join(c.choices[0].delta.content or '' for c in chunks)
        return result.choices[0]
    call.record = lambda: json.loads((tmp_path / 'record.json').read_text())
    yield call
    client.close(); peer.shutdown(); peer.server_close()


def body(model, reasoning):
    return json.loads(directsdk.request_body({'model': model, 'messages': [], 'extra_body': {'reasoning': reasoning}})[0])


def test_update_routes_leave_thinking_to_claude_code_and_others_are_unchanged(monkeypatch):
    on, summarized = {'enabled': True, 'effort': 'high'}, {'type': 'adaptive', 'display': 'summarized'}
    def sent(model, reasoning, setting):
        monkeypatch.setattr(directsdk, 'progress_updates_enabled', lambda: setting)
        return body(model, reasoning)
    for route in ('opus', 'claude-opus-5-5', 'claude-opus-5-5[1m]', 'opus-5.5', 'fable', 'claude-fable-5-1[1m]'):
        assert sent(route, on, True) == {'output_config': {'effort': 'high'}, 'tools': []}, route
        assert sent(route, on, False) == {'thinking': summarized, 'output_config': {'effort': 'high'}, 'tools': []}, route
    for model in ('opus', 'fable', 'sonnet', 'claude-sonnet-5-5', 'haiku', 'claude-opus-5', 'claude-haiku-4-5'):
        for reasoning in (on, {'enabled': False}, {'effort': 'none'}):
            if model not in ('opus', 'fable') or reasoning is not on:
                assert sent(model, reasoning, True) == sent(model, reasoning, False), (model, reasoning)


def test_the_three_settings_default_and_are_declared(profile):
    import directsdk_setup
    from hermes_cli.plugins_settings import plugin_settings_fields
    assert (directsdk_setup.progress_updates_enabled(), directsdk_setup.claude_guidance_enabled(), directsdk_setup.settle_answers_enabled()) == (True, True, False)
    fields = {f['key']: f for f in plugin_settings_fields(PLUGIN_NAME, Path(directsdk_setup.__file__).resolve().parent)}
    assert [(fields[k]['type'], fields[k]['default']) for k in ('progress_updates', 'claude_guidance', 'settle_answers')] == [('boolean', True), ('boolean', True), ('boolean', False)]


@pytest.mark.parametrize('key, value, read', [('progress_updates', False, 'progress_updates_enabled'),
                                              ('claude_guidance', False, 'claude_guidance_enabled'),
                                              ('settle_answers', True, 'settle_answers_enabled')])
def test_each_setting_is_honoured(profile, key, value, read):
    import directsdk_setup
    from hermes_cli.config import get_config_path
    get_config_path().write_text('plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        %s: %s\n'
                                 % (PLUGIN_NAME, key, json.dumps(value)), encoding='utf-8')
    assert getattr(directsdk_setup, read)() is value
    if key == 'progress_updates':
        assert body('opus', {'enabled': True})['thinking'] == {'type': 'adaptive', 'display': 'summarized'}


def test_admission_records_the_display_sent_upstream_without_changing_bytes():
    received = []
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    gate = admission.Admission(f'http://127.0.0.1:{peer.server_port}', 5)
    try:
        for payload, display in ((b'{"thinking": {"type": "adaptive", "display": "updates"}, "messages": []}', 'updates'),
                                 (b'{"thinking": {"type": "adaptive"}, "messages": []}', None), (b'not json', None)):
            gate.rearm(5, None)
            urllib.request.urlopen(urllib.request.Request(gate.url + '/v1/messages', data=payload), timeout=5).read()
            assert received[-1] == payload and gate.display == display
        gate.rearm(5, None)
        assert gate.display is None
    finally:
        gate.close(); peer.shutdown(); peer.server_close()


def test_an_update_before_a_call_becomes_interim_content_and_replays_signed(run):
    choice = run([REASONING, UPDATE, {'type': 'text', 'text': ' Then the config. '}, CALL])
    message = choice.message
    assert choice.finish_reason == 'tool_calls' and message.tool_calls[0].id == 'toolu_1'
    assert message.content == 'Checking the logs first.\n\nThen the config.' and message.reasoning_content is None
    assert message.reasoning_details[0]['projection'] == directsdk.projection(message.model_dump())
    stored = dict(message.model_dump(), content=message.content.strip())
    stored.pop('reasoning_content')
    history = [{'role': 'user', 'content': 'go'}, stored, {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'ok'}]
    replayed = directsdk.prepare_history(history, {'probe': 'probe'})[1][1]['message']['content']
    assert [b.get('signature') for b in replayed if b['type'] == 'thinking'] == ['sig-reasoning', 'sig-update']
    # The session key of the message Hermes sends back is the key of the message returned.
    assert directsdk._message_key(stored) == directsdk._message_key(message.model_dump())


def test_a_final_answer_is_never_prefixed_with_an_update(run):
    message = run([UPDATE, {'type': 'text', 'text': 'All done.'}], stop='end_turn').message
    assert message.content == 'All done.' and message.reasoning_content == 'Checking the logs first.'


def test_a_refusal_keeps_update_text_out_of_content(run):
    choice = run([UPDATE, CALL], stop='refusal', details={'category': 'cyber', 'explanation': 'Declined.'})
    assert choice.finish_reason == 'content_filter' and choice.message.content is None and choice.message.tool_calls is None
    assert choice.message.refusal == 'Declined.'


def test_an_interrupted_update_follows_its_calls(run):
    cut = dict(UPDATE, thinking='Reading the file. This part of the response was interrupted before it finished.')
    with_call = run([cut, CALL], stop='max_tokens')
    assert with_call.finish_reason == 'tool_calls' and with_call.message.content == cut['thinking']
    without = run([cut, {'type': 'text', 'text': 'Partial'}], stop='max_tokens')
    assert without.finish_reason == 'length' and without.message.content == 'Partial' and without.message.reasoning_content == cut['thinking']


def test_streamed_text_is_checked_against_text_blocks_only(run):
    choice, streamed = run([UPDATE, {'type': 'text', 'text': 'Then the config.'}, CALL], stream=True)
    assert streamed == 'Then the config.' and choice.message.content == 'Checking the logs first.\n\nThen the config.'


@pytest.mark.parametrize('model, reasoning', [('sonnet', True), ('opus', False)])
def test_non_update_requests_keep_thinking_in_reasoning(run, model, reasoning):
    # Opus with reasoning off sends no `thinking` field either, so Claude Code asks for "updates"; it is not the
    # progress route, so its message stays as before.
    message = run([UPDATE, {'type': 'text', 'text': 'Then the config.'}, CALL], model=model, reasoning=reasoning).message
    assert message.content == 'Then the config.' and message.reasoning_content == 'Checking the logs first.'


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
