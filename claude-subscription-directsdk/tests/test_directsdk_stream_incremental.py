"""Session streaming delivers text early and keeps the native carrier on the final chunk."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
import test_directsdk_session as base


# Reuse the session/MCP protocol, but forward SSE events immediately rather than
# buffering the HTTP response and synthesizing text deltas after it ends.
BUFFERED_READ = "raw = urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=body, headers={'Content-Type':'application/json'}), timeout=30).read().decode()"
STREAMING_READ = """response = urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=body, headers={'Content-Type':'application/json'}), timeout=30)
        frames = []
        for rawline in response:
            line = rawline.decode().rstrip('\\n')
            if line.startswith('data: '):
                frames.append(line)
                out({'type': 'stream_event', 'event': json.loads(line[6:])})
        raw = '\\n\\n'.join(frames)"""
SYNTHETIC_TEXT = "        if block['type'] == 'text':\n            out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':block['text']}}})\n"
assert base.NATIVE.count(BUFFERED_READ) == base.NATIVE.count(SYNTHETIC_TEXT) == 1
STREAMING_NATIVE = base.NATIVE.replace(BUFFERED_READ, STREAMING_READ).replace(SYNTHETIC_TEXT, '')


@pytest.fixture
def streamed_step(tmp_path, monkeypatch):
    for name, value in (('_session_failures', 0), ('_session_off', False), ('_session_noted', set())):
        monkeypatch.setattr(directsdk, name, value)
    native = tmp_path / 'native.py'
    native.write_text(STREAMING_NATIVE)

    def run(thinking_only=False):
        chunks, arrivals, stops = [], [], []

        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()

                def emit(event):
                    if event['type'] == 'message_stop':
                        stops.append(time.monotonic())
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()

                emit({'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant',
                      'model': 'sonnet', 'content': [], 'usage': dict(base.USAGE)}})
                kind = 'thinking' if thinking_only else 'text'
                emit({'type': 'content_block_start', 'index': 0,
                      'content_block': {'type': kind, kind: ''}})
                for text in (['private fixture thought'] if thinking_only else ['one ', 'two ', 'three']):
                    time.sleep(0.4)
                    emit({'type': 'content_block_delta', 'index': 0,
                          'delta': {'type': kind + '_delta', kind: text}})
                emit({'type': 'content_block_stop', 'index': 0})
                if not thinking_only:
                    emit({'type': 'content_block_start', 'index': 1, 'content_block': {
                        'type': 'tool_use', 'id': 'toolu_1', 'name': 'mcp__hermes__probe', 'input': {}}})
                    emit({'type': 'content_block_delta', 'index': 1, 'delta': {
                        'type': 'input_json_delta', 'partial_json': '{"key":"fixture"}'}})
                    emit({'type': 'content_block_stop', 'index': 1})
                emit({'type': 'message_delta', 'delta': {
                    'stop_reason': 'end_turn' if thinking_only else 'tool_use'}, 'usage': dict(base.USAGE)})
                emit({'type': 'message_stop'})

        with ThreadingHTTPServer(('127.0.0.1', 0), Peer) as peer:
            worker = threading.Thread(target=peer.serve_forever, daemon=True)
            worker.start()
            env = {'PATH': os.defpath, 'HOME': str(tmp_path),
                   'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}',
                   'CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE': '1', 'SPAWNS': str(tmp_path / 'spawns')}
            client = directsdk.Client(command=[sys.executable, str(native)], env=env, timeout=20)
            try:
                for chunk in client.create(model='sonnet', messages=[{'role': 'user', 'content': 'go'}],
                                           tools=base.TOOLS, stream=True):
                    chunks.append(chunk)
                    arrivals.append(time.monotonic())
            finally:
                client.close()
                directsdk._session_shutdown()
                peer.shutdown()
                worker.join(timeout=5)
        assert chunks[-1]._response.usage.native_admission.session == 'cold'
        return chunks, arrivals, stops

    return run


def test_session_text_arrives_before_message_stop(streamed_step):
    chunks, arrivals, stops = streamed_step()
    assert len(stops) == 1
    early = [c for c, arrived in zip(chunks, arrivals) if c.choices[0].delta.content and arrived < stops[0]]
    assert len(early) >= 2, 'text must stream before the peer writes message_stop'
    assert all(not c.choices[0].delta.tool_calls for c in chunks[:-1])
    assert chunks[-1].choices[0].delta.tool_calls
    assert ''.join(c.choices[0].delta.content or '' for c in chunks) == chunks[-1]._response.choices[0].message.content
    assert chunks[-1].choices[0].delta.reasoning_details[0]['type'].endswith('.native_assistant')


def test_session_thinking_only_final_chunk_keeps_carrier(streamed_step):
    chunks, _, _ = streamed_step(thinking_only=True)
    final = chunks[-1]
    assert final.choices[0].finish_reason == 'stop'
    assert final.choices[0].delta.content is None
    assert final.choices[0].delta.reasoning_details, 'final chunk must carry native reasoning_details'
    assert final.choices[0].delta.reasoning_details[0]['type'].endswith('.native_assistant')
    assert final._response.choices[0].message.reasoning_content is None
