"""Session mode: one live native session across Hermes steps; parked hermes tool calls; rebuild on divergence."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

# A native that speaks the session protocol: SDK MCP over control requests, one tool call at a time,
# a new upstream request after every answered batch, and an idle wait for the next user frame.
NATIVE = r'''
import json, os, re, sys, urllib.request
with open(os.environ['SPAWNS'], 'a') as f:
    f.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:], 'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'), 'entrypoint': os.environ.get('CLAUDE_CODE_ENTRYPOINT'), 'compact': [os.environ.get(k) for k in ('DISABLE_COMPACT', 'DISABLE_AUTO_COMPACT')]}) + '\n')
history, queue, rpc, answered, limits = [], [], [0], {}, {}
first_query = True
def out(row):
    print(json.dumps(row), flush=True)
def persist(block, result, force=False):
    # 2.1.285: an MCP result past the tool's limit (UTF-16 units) is written under the config dir and replaced by a stub
    # in native's history; an error result past 11,024 chars is middle-truncated. FAKE_PERSIST_LIMIT stands in for a CLI
    # that persists below what tools/list advertised.
    text = ''.join(b['text'] for b in result['content'] if b['type'] == 'text')
    size = len(text.encode('utf-16-le', 'surrogatepass')) // 2
    if result.get('isError') and size > 11024:
        return text[:5000] + '\n... [%d characters truncated] ...\n' % (len(text) - 10000) + text[-5000:]
    if not force and size <= int(os.environ.get('FAKE_PERSIST_LIMIT') or limits.get(block['name'], 50000)):
        return result['content']
    config = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.environ['HOME'], '.claude')
    path = os.path.join(config, 'projects', re.sub(r'[^A-Za-z0-9]', '-', os.getcwd()), 'session-%d' % os.getpid(), 'tool-results', block['id'] + '.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(result['content'], f)
    return '<persisted-output>\nOutput too large (%dKB). Full output saved to: %s\n\nPreview (first 2KB):\n%s\n...\n</persisted-output>' % (size // 1000, path, text[:2000])
def round_budget():
    budget = os.environ.get('FAKE_ROUND_BUDGET')
    if not budget:
        return
    results = []
    for message in reversed(history):
        if message['role'] != 'user':
            break
        results.extend(b for b in message['content'] if b['type'] == 'tool_result')
    def text(result):
        content = result['content']
        return content if isinstance(content, str) else ''.join(b['text'] for b in content if b['type'] == 'text')
    def size(result):
        return len(text(result).encode('utf-16-le', 'surrogatepass')) // 2
    total = sum(size(result) for result in results)
    for result in sorted(results, key=size, reverse=True):
        if total <= int(budget):
            break
        before = size(result)
        result['content'] = persist({'id':result['tool_use_id']}, {'content':[{'type':'text','text':text(result)}]}, force=True)
        total += size(result) - before
def call_next():
    block = queue[0]
    rpc[0] += 1
    params = {'name':block['name'][len('mcp__hermes__'):],'arguments':block['input']}
    # Anthropic's Agent SDK sends no claudecode/toolUseId; FAKE_TOOL_USE_ID_RPCS names only that many first RPCs.
    if not os.environ.get('FAKE_NO_TOOL_USE_ID') and rpc[0] <= int(os.environ.get('FAKE_TOOL_USE_ID_RPCS', '1000000')):
        params['_meta'] = {'claudecode/toolUseId':block['id']}
    out({'type':'control_request','request_id':'c%d' % rpc[0],'request':{'subtype':'mcp_message','server_name':'hermes','message':{'jsonrpc':'2.0','id':rpc[0],'method':'tools/call','params':params}}})
def wire():
    global first_query
    # Real native merges consecutive user messages; NATIVE_CONTEXT adds its per-request context the way 2.1.287+
    # does: the live process keeps its first-turn reminder in history; later context stays ephemeral.
    reminder = '\n<system-reminder>userEmail: user@example.com</system-reminder>'
    if first_query and os.environ.get('NATIVE_CONTEXT') and len(history) == 1:
        history[0]['content'].insert(0, {'type':'text','text':reminder})
    first_query = False
    messages = []
    for message in json.loads(json.dumps(history)):
        if messages and message['role'] == messages[-1]['role'] == 'user':
            messages[-1]['content'] += message['content']
        else:
            messages.append(message)
    if os.environ.get('FAKE_REVERSE'):
        messages[-1]['content'].reverse()  # intact results, forwarded in another order
    if not os.environ.get('NATIVE_CONTEXT'):
        return messages
    newest = messages[-1]['content']
    if len(messages) > 1 and newest[-1]['type'] == 'tool_result':
        if isinstance(newest[-1]['content'], str):
            newest[-1]['content'] += reminder
        else:
            newest[-1]['content'].append({'type':'text','text':reminder})
    elif len(messages) > 1:
        newest.append({'type':'text','text':reminder})
    if len(messages) > 1:
        messages[-2]['content'][-1]['cache_control'] = {'type':'ephemeral'}
    return messages + [{'role':'system','content':[{'type':'text','text':'Today is 2026-10-05.','cache_control':{'type':'ephemeral'}}]}]
def query():
    body = json.dumps({'messages': wire()}).encode()
    try:
        raw = urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=body, headers={'Content-Type':'application/json'}), timeout=30).read().decode()
    except urllib.error.HTTPError as error:
        out({'type':'assistant','error':'rate_limit' if error.code == 429 else 'unknown','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'API Error: %d' % error.code}]}})
        out({'type':'result','subtype':'success','is_error':True,'num_turns':1})
        return
    message = None
    for frame in raw.split('\n\n'):
        if frame.startswith('data: '):
            event = json.loads(frame[6:])
            if event['type'] == 'message_start':
                message = event['message']
            elif event['type'] == 'content_block_start':
                message['content'].append(event['content_block'])
            elif event['type'] == 'content_block_delta' and event['delta']['type'] == 'input_json_delta':
                try:
                    message['content'][event['index']]['input'] = json.loads(event['delta']['partial_json'] or '{}')
                except ValueError:
                    pass  # A call cut off mid-argument (max_tokens): native keeps it in its transcript as streamed.
            elif event['type'] == 'message_delta':
                message.update(event['delta'])
    for block in message['content']:
        if block['type'] == 'text':
            out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':block['text']}}})
        out({'type':'assistant','message':dict(message, content=[block])})
    out({'type':'stream_event','event':{'type':'message_stop'}})
    history.append({'role':'assistant','content':message['content']})
    tools = [b for b in message['content'] if b['type'] == 'tool_use'] if message.get('stop_reason') == 'tool_use' else []
    if tools:
        queue.extend(tools[::-1] if os.environ.get('FAKE_REVERSE_CALLS') else tools)  # parallel calls can dispatch in any order
        call_next()
    else:
        out({'type':'result','subtype':'success','is_error':False,'num_turns':1,'usage':message['usage']})
for line in sys.stdin:
    row = json.loads(line)
    if row['type'] == 'control_request':
        out({'type':'control_response','response':{'subtype':'success','request_id':row['request_id'],'response':{}}})
        out({'type':'control_request','request_id':'list','request':{'subtype':'mcp_message','server_name':'hermes','message':{'jsonrpc':'2.0','id':0,'method':'tools/list'}}})
    elif row['type'] == 'control_response' and row['response']['request_id'] == 'list':
        with open(os.environ['SPAWNS'] + '.tools', 'a') as f:
            f.write(json.dumps(row['response']['response']['mcp_response']['result']['tools']) + '\n')
        # Without _meta an MCP tool persists past min(100,000, 50,000); _meta counts up to the 500,000 ceiling.
        for tool in row['response']['response']['mcp_response']['result']['tools']:
            meta = (tool.get('_meta') or {}).get('anthropic/maxResultSizeChars')
            limits['mcp__hermes__' + tool['name']] = min(meta, 500000) if isinstance(meta, (int, float)) and meta > 0 else 50000
    elif row['type'] == 'control_response':
        result = row['response']['response']['mcp_response']['result']
        block = queue.pop(0)
        content = persist(block, result)
        history.append({'role':'user','content':[{'type':'tool_result','tool_use_id':block['id'],'content':content, **({'is_error':True} if result.get('isError') else {})}]})
        out({'type':'user','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':block['id'],'content':content}]},'tool_use_result':result['content']})
        if queue:
            call_next()
        else:
            round_budget()  # Native's aggregate rewrite happens AFTER its stdout echoes.
            query()
    elif row['type'] in ('user', 'assistant'):
        history.append(row['message'])
        if row.get('shouldQuery') is False:
            out({'type':'result','num_turns':0,'is_error':False})
        elif row['type'] == 'user' and os.environ.get('AUTH_FAIL'):
            # Logged out: native answers itself before any upstream request.
            out({'type':'assistant','error':'authentication_failed','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'Not logged in'}]}})
            out({'type':'result','subtype':'success','is_error':True,'num_turns':1,'result':'Not logged in'})
        elif row['type'] == 'user' and os.environ.get('NATIVE_ERROR'):
            # A plan limit native answers itself, with no upstream request.
            out({'type':'assistant','error':os.environ['NATIVE_ERROR'],'message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'limit reached'}]}})
            out({'type':'result','subtype':'success','is_error':True,'num_turns':1})
        elif row['type'] == 'user':
            query()
'''

USAGE = {'input_tokens': 1, 'output_tokens': 2, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 4}


def sse(message_id, blocks, stop, details=None):
    events = [{'type': 'message_start', 'message': {'id': message_id, 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': dict(USAGE)}}]
    for index, block in enumerate(blocks):
        if block['type'] == 'tool_use':
            events += [{'type': 'content_block_start', 'index': index, 'content_block': dict(block, input={})},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}}]
        else:
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': block['text']}}]
        events.append({'type': 'content_block_stop', 'index': index})
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop, **({'stop_details': details} if details else {})}, 'usage': dict(USAGE)}, {'type': 'message_stop'}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


class Upstream:
    """Answers the newest native message: a user text asks for two parallel probe calls, tool results get echoed."""
    def __init__(self, hang=None):
        self.bodies, self.hang = [], hang or threading.Event()
        self.reply = None
        outer = self
        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.bodies.append(body)
                if outer.hang.is_set():
                    time.sleep(30)
                    return
                # Native's trailing per-request context (NATIVE_CONTEXT) is not part of the conversation.
                messages = [m for m in body['messages'] if m['role'] != 'system']
                last = messages[-1]
                content = last['content'] if isinstance(last['content'], list) else [{'type': 'text', 'text': last['content']}]
                if 'slow' in content[0].get('text', ''):
                    time.sleep(1.5)
                results = [b for m in messages[-2:] for b in (m['content'] if isinstance(m['content'], list) else []) if b.get('type') == 'tool_result']
                n = len(outer.bodies)
                text = ''.join(b.get('text', '') for b in content if b.get('type') == 'text')
                if 'limit' in text:
                    error = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'slow down'}}).encode()
                    self.send_response(429)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                if results and last['role'] == 'user' and content[0].get('type') == 'tool_result':
                    texts = [r['content'][0]['text'] if isinstance(r['content'], list) else r['content'] for r in results]
                    payload = sse(f'm{n}', [{'type': 'text', 'text': 'got ' + '+'.join(texts)}], 'end_turn')
                elif 'refuse' in text:
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}r', 'name': 'mcp__hermes__probe', 'input': {}}], 'refusal',
                                  {'category': 'cyber', 'explanation': 'The request was declined.'})
                elif 'ghost' in text or 'bare' in text:
                    name = 'mcp__hermes__ghost' if 'ghost' in text else 'probe'
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}g', 'name': name, 'input': {'key': 'g'}}], 'tool_use')
                elif 'parallel' in text:
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}a', 'name': 'mcp__hermes__probe', 'input': {'key': 'a'}},
                                            {'type': 'tool_use', 'id': f'toolu_{n}b', 'name': 'mcp__hermes__probe', 'input': {'key': 'b'}}], 'tool_use')
                else:
                    payload = sse(f'm{n}', [{'type': 'text', 'text': 'echo ' + content[0].get('text', '')}], 'end_turn')
                if outer.reply:
                    payload = outer.reply(n, body)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


TOOLS = [{'type': 'function', 'function': {'name': 'probe', 'description': 'probe', 'parameters': {'type': 'object', 'properties': {'key': {'type': 'string'}}}}}]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Process-wide session-mode state starts fresh for every test.
    monkeypatch.setattr(directsdk, '_session_failures', 0)
    monkeypatch.setattr(directsdk, '_session_off', False)
    monkeypatch.setattr(directsdk, '_session_noted', set())
    upstream = Upstream()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    spawns = tmp_path / 'spawns.jsonl'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{upstream.server.server_port}',
           'CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE': '1', 'SPAWNS': str(spawns)}
    clients = []
    def client(drop=()):
        clients.append(directsdk.Client(command=[sys.executable, str(native)], env={k: v for k, v in env.items() if k not in drop}, timeout=20))
        return clients[-1]
    def spawned():
        return [json.loads(line) for line in spawns.read_text().splitlines()] if spawns.exists() else []
    fixture = SimpleFixture(client, spawned, upstream)
    fixture.manifests = lambda: [json.loads(line) for line in Path(str(spawns) + '.tools').read_text().splitlines()]
    yield fixture
    for c in clients:
        c.close()
    directsdk._session_shutdown()
    upstream.close()


class SimpleFixture:
    def __init__(self, client, spawned, upstream):
        self.client, self.spawned, self.upstream = client, spawned, upstream


def step(client, messages, stream=False):
    result = client.create(model='sonnet', messages=messages, tools=TOOLS, stream=stream)
    if stream:
        chunks = list(result)
        final = chunks[-1]
        return final._response
    return result


def as_history(response):
    message = response.choices[0].message.model_dump()
    message['content'] = (message.get('content') or '').strip()
    message.pop('reasoning_content', None)
    return message


def alive(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False

@pytest.mark.parametrize('bare', [False, True])
def test_session_long_tool_name_round_trips_and_retains_session(rig, bare):
    name = 'public_probe_' + 'x' * 48
    tools = [{'type': 'function', 'function': {'name': name}}]
    encoded, manifest, names = directsdk.request_body({'tools': tools})
    alias = names[name]
    native_name = alias if bare else directsdk.PREFIX + alias
    rig.upstream.reply = lambda n, body: sse(f'm{n}', [
        {'type': 'tool_use', 'id': f'toolu_{n}', 'name': native_name, 'input': {}}
    ], 'tool_use') if n == 1 else sse(f'm{n}', [{'type': 'text', 'text': 'done'}], 'end_turn')
    history = [{'role': 'user', 'content': 'long-tool'}]
    client = rig.client()
    first = client.create(model='sonnet', messages=history, tools=tools)
    call = first.choices[0].message.tool_calls[0]
    assert call.function.name == name
    assert first.choices[0].message.reasoning_details[0]['messages'][0]['content'][0]['name'] == directsdk.PREFIX + alias
    history += [as_history(first), {'role': 'tool', 'tool_call_id': call.id, 'content': 'public result'}]
    second = client.create(model='sonnet', messages=history, tools=tools)
    # A bare call is repaired for replay, but native did not park it on the MCP server.
    assert second.usage.native_admission.session == ('cold' if bare else 'reused')
    assert second.choices[0].message.content == 'done'
    assert len(rig.spawned()) == (2 if bare else 1)
    advertised = rig.manifests()[0]
    assert [{k: v for k, v in t.items() if k != '_meta'} for t in advertised] == manifest
    assert json.loads(encoded)['tools'][0]['name'] == directsdk.PREFIX + advertised[0]['name']



def test_one_native_serves_tool_rounds_and_turns_with_hermes_results(rig):
    messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    calls = first.choices[0].message.tool_calls
    assert [json.loads(c.function.arguments)['key'] for c in calls] == ['a', 'b']
    assert first.usage.model_dump()['native_admission']['session'] == 'cold'
    # Native already asked the hermes MCP server for the first call; it stays parked while Hermes runs tools.
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'BANANA'},
                 {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'APPLE'}]
    # Hermes builds request-local clients: a fresh client must still find the session by history.
    second = step(rig.client(), messages, stream=True)
    assert second.choices[0].message.content == 'got APPLE+BANANA'
    assert second.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None, 'session': 'reused'}
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(rig.client(), messages)
    assert third.choices[0].message.content == 'echo next turn'
    assert third.usage.model_dump()['native_admission']['session'] == 'reused'
    spawned = rig.spawned()
    assert len(spawned) == 1 and len(rig.upstream.bodies) == 3
    argv = spawned[0]['argv']
    assert '--max-turns' not in argv and argv[argv.index('--allowedTools') + 1] == 'mcp__hermes'
    assert json.loads(argv[argv.index('--mcp-config') + 1]) == {'mcpServers': {'hermes': {'type': 'sdk', 'name': 'hermes'}}}
    # The carrier is the baseline one, so a later rebuild (or a cold client) replays it unchanged.
    assert second.choices[0].message.reasoning_details[0]['type'] == directsdk.CARRIER


def test_restart_replays_string_tool_results_as_kept_alive_mcp_blocks(rig):
    client = rig.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    messages += [as_history(first)] + [
        {'role': 'tool', 'tool_call_id': call.id, 'content': f'Result {i}'}
        for i, call in enumerate(first.choices[0].message.tool_calls)]
    second = step(client, messages)

    def results(body):
        return [b['content'] for m in body['messages'] for b in m['content'] if b['type'] == 'tool_result']

    kept_alive = results(rig.upstream.bodies[-1])
    assert kept_alive == [[{'type': 'text', 'text': f'Result {i}'}] for i in range(2)]
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    original = json.dumps(messages)
    directsdk._session_shutdown()
    step(client, messages)
    assert results(rig.upstream.bodies[-1]) == kept_alive
    assert json.dumps(messages) == original


def test_divergence_kills_the_session_and_replays_full_history(rig):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': f'R{i}' * 50} for i, c in enumerate(calls)]
    old = rig.spawned()[0]['pid']
    second = step(rig.client(), messages)
    messages += [as_history(second)]
    # Host compaction rewrites a tool result: the session no longer prefixes the request.
    messages[2] = dict(messages[2], content='[compacted] R0')
    messages.append({'role': 'user', 'content': 'after compaction'})
    third = step(rig.client(), messages)
    assert third.usage.model_dump()['native_admission']['session'] == 'rebuild:prefix'
    assert third.choices[0].message.content == 'echo after compaction'
    spawned = rig.spawned()
    assert len(spawned) == 2 and not alive(old)
    # The rebuild replayed the full (edited) history: every tool result's text, not an excerpt.
    replayed = rig.upstream.bodies[-1]['messages']
    results = [b for m in replayed if isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'tool_result']
    assert [r['content'] for r in results] == [
        [{'type': 'text', 'text': text}] for text in ('[compacted] R0', 'R1' * 50)]
    # The rebuilt session is live again for the next step.
    messages += [as_history(third), {'role': 'user', 'content': 'again'}]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert len(rig.spawned()) == 2


def test_unexpected_suffix_rebuilds(rig):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    calls = first.choices[0].message.tool_calls
    # An interrupted step: Hermes answers one call only and appends a user row.
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'A'},
                 {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'B'}, {'role': 'user', 'content': 'steer'}]
    result = step(rig.client(), messages)
    assert result.usage.model_dump()['native_admission']['session'] == 'rebuild:suffix'
    assert len(rig.spawned()) == 2


def test_cancel_kills_the_session_mode(rig):
    rig.upstream.hang.set()
    client = rig.client()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(step, client, [{'role': 'user', 'content': 'call probe in parallel'}])
        deadline = time.monotonic() + 10
        while not rig.upstream.bodies and time.monotonic() < deadline:
            time.sleep(.05)
        client.cancel()
        with pytest.raises(RuntimeError, match='cancelled'):
            future.result(timeout=10)
    pid = rig.spawned()[0]['pid']
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._session_pool


def test_tool_free_calls_and_unflagged_clients_stay_per_call(rig, tmp_path):
    client = rig.client()
    # Auxiliary one-shots carry no tools: they never start a session.
    result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'title please'}])
    assert result.choices[0].message.content == 'echo title please'
    assert result.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None}
    assert '--max-turns' in rig.spawned()[0]['argv']
    assert not directsdk._session_pool


def test_shutdown_leaves_no_native_behind(rig):
    step(rig.client(), [{'role': 'user', 'content': 'call probe in parallel'}])
    pid = rig.spawned()[0]['pid']
    assert alive(pid) and len(directsdk._session_pool) == 1
    directsdk._session_shutdown()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._session_pool


def test_interleaved_conversations_keep_separate_natives(rig):
    left = [{'role': 'user', 'content': 'call probe in parallel'}]
    right = [{'role': 'user', 'content': 'call probe in parallel, please'}]
    for messages in (left, right):
        first = step(rig.client(), messages)
        messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': c.id[-2:] + '-' + messages[0]['content'][-6:]}
                                           for c in first.choices[0].message.tool_calls]
    answers = [step(rig.client(), messages) for messages in (right, left)]
    assert [a.usage.model_dump()['native_admission']['session'] for a in answers] == ['reused', 'reused']
    assert answers[0].choices[0].message.content == 'got 2a-please+2b-please'
    assert answers[1].choices[0].message.content == 'got 1a-rallel+1b-rallel'
    assert len(rig.spawned()) == 2 and len(directsdk._session_pool) == 2



# ---- Codex review (v2): each test reproduces one finding at 4f8016d ----

LAUNCHER = 'import subprocess, sys\nsys.exit(subprocess.call([sys.executable] + sys.argv[1:]))\n'


@pytest.mark.parametrize('launcher', [False, True])
def test_r2_identical_visible_histories_never_share_a_native(rig, tmp_path, launcher):
    """R2: two conversations with the same visible text differ only in native message identity."""
    def client():
        c = rig.client()
        if launcher:
            shim = tmp_path / 'launcher.py'
            shim.write_text(LAUNCHER)
            c.command = [sys.executable, str(shim)] + c.command[1:]
        return c
    a = [{'role': 'user', 'content': 'hi'}]
    b = [{'role': 'user', 'content': 'hi'}]
    sessions = []
    for messages in (a, b):
        messages.append(as_history(step(client(), messages)))
        sessions += [s for s in directsdk._session_pool if s not in sessions]
    assert a[1]['content'] == b[1]['content'] == 'echo hi'
    # One identity throughout: the pool's own sessions and Popen pids. Behind a launcher (a Windows venv python)
    # the pid native reports is not the one Popen holds.
    session_a, session_b = sessions
    pids = session_a.pid, session_b.pid
    b.append({'role': 'user', 'content': 'b turn two'})
    assert step(client(), b).choices[0].message.content == 'echo b turn two'
    # B's own native continued; A's native was neither used nor killed; nothing replaced either.
    assert len(directsdk._session_pool) == 2 and set(directsdk._session_pool) == {session_a, session_b}
    assert (session_a.pid, session_b.pid) == pids and session_a.alive() and session_b.alive() and len(rig.spawned()) == 2
    assert len(session_b.consumed) == 4 and len(session_a.consumed) == 2


def test_r3_changed_native_carrier_forces_rebuild(rig):
    """R3: same visible text, different (here: removed) native carrier must not continue the old native state."""
    messages = [{'role': 'user', 'content': 'hi'}]
    messages.append(as_history(step(rig.client(), messages)))
    messages[1] = {k: v for k, v in messages[1].items() if k != 'reasoning_details'}
    messages.append({'role': 'user', 'content': 'again'})
    result = step(rig.client(), messages)
    assert result.usage.model_dump()['native_admission']['session'] != 'reused'
    assert len(rig.spawned()) == 2


def test_r5_cancel_after_tool_calls_kills_the_parked_native(rig):
    """R5: create() returned tool calls, Hermes is running tools, the user interrupts: client.cancel()."""
    client = rig.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    pid = rig.spawned()[0]['pid']
    client.cancel()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._session_pool
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'cold'


def test_r9_pool_cap_holds_with_busy_sessions(rig, monkeypatch):
    """R9: with every session slot busy, an extra conversation takes the per-call path; nothing exceeds the cap."""
    monkeypatch.setattr(directsdk, '_session_cap', lambda: 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(step, rig.client(), [{'role': 'user', 'content': 'slow hi'}])
        deadline = time.monotonic() + 10
        while not rig.upstream.bodies and time.monotonic() < deadline:
            time.sleep(.02)
        other = step(rig.client(), [{'role': 'user', 'content': 'hi'}])
        assert slow.result(timeout=20).choices[0].message.content == 'echo slow hi'
    assert other.choices[0].message.content == 'echo hi'
    assert 'session' not in other.usage.model_dump()['native_admission']
    assert '--max-turns' in rig.spawned()[1]['argv']
    assert len(directsdk._session_pool) <= 1


def test_rauth_launch_identity_is_part_of_reuse(rig, tmp_path):
    """R-auth: a different native config dir (or command) with identical history must not reuse the old child."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    other = rig.client()
    other.env['CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'] = str(tmp_path / 'other-config')
    assert step(other, messages).usage.model_dump()['native_admission']['session'] != 'reused'
    assert len(rig.spawned()) == 2


def test_session_native_uses_active_profiles_config_dir(rig, profile, tmp_path):
    from test_directsdk_config_dir import _set_config_dir
    selected = str(tmp_path / 'profile-login')
    _set_config_dir(selected)
    response = step(rig.client(), [{'role': 'user', 'content': 'hello'}])
    assert response.usage.model_dump()['native_admission']['session'] == 'cold'
    assert [child['config_dir'] for child in rig.spawned()] == [selected]


def test_changed_profile_config_dir_rebuilds_session_conversation(rig, profile, tmp_path):
    from test_directsdk_config_dir import _set_config_dir
    first_dir, changed_dir = (str(tmp_path / name) for name in ('first-login', 'changed-login'))
    _set_config_dir(first_dir)
    client = rig.client()
    messages = [{'role': 'user', 'content': 'hello'}]
    messages += [as_history(step(client, messages)), {'role': 'user', 'content': 'again'}]
    old = rig.spawned()[0]['pid']
    _set_config_dir(changed_dir)
    response = step(client, messages)
    assert response.usage.model_dump()['native_admission']['session'] == 'rebuild:ident'
    assert [child['config_dir'] for child in rig.spawned()] == [first_dir, changed_dir]
    assert not alive(old)


def test_a_fork_on_another_model_leaves_the_chat_session_alive(rig):
    """A review on another model shares the chat's history but must not retire the chat's native."""
    client = rig.client()
    messages = [{'role': 'user', 'content': 'hello'}]
    messages += [as_history(step(client, messages)), {'role': 'user', 'content': 'again'}]
    chat = rig.spawned()[0]['pid']
    fork = client.create(model='opus', messages=messages, tools=TOOLS)
    assert fork.usage.model_dump()['native_admission']['session'] == 'cold'
    assert alive(chat)
    assert step(client, messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert rig.spawned()[0]['pid'] == chat and len(rig.spawned()) == 2
    client.close()


def test_rauth_conflicting_override_is_refused_on_reuse(rig, monkeypatch):
    """R-auth: the inherited-environment refusal runs on every session step, not only at spawn."""
    url = f'http://127.0.0.1:{rig.upstream.server.server_port}'
    class Local(directsdk.Admission):
        def __init__(self, upstream, timeout, queried=None, first_turn=None):
            super().__init__(url, timeout, queried, first_turn=first_turn)
    monkeypatch.setattr(directsdk, 'Admission', Local)
    fixture = rig.client()
    for key, value in fixture.env.items():
        if key != 'ANTHROPIC_BASE_URL':
            monkeypatch.setenv(key, value)
    monkeypatch.delenv('ANTHROPIC_BASE_URL', raising=False)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_FOUNDRY_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(key, raising=False)
    client = directsdk.Client(command=fixture.command, timeout=20)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-not-a-key')
    with pytest.raises(ValueError, match='ANTHROPIC_API_KEY'):
        step(client, messages)
    client.close()


def test_logged_out_session_native_raises_the_login_hint(rig):
    """NON_BLOCKING: session keeps baseline's ClaudeCodeLoggedOut type and hint."""
    from directsdk_setup import LOGGED_OUT_HINT
    client = rig.client()
    client.env['AUTH_FAIL'] = '1'
    with pytest.raises(directsdk.ClaudeCodeLoggedOut, match='Not logged in'):
        step(client, [{'role': 'user', 'content': 'hi'}])
    assert LOGGED_OUT_HINT


# ---- Codex re-review (v3) ----

def test_r5_cancel_during_handoff_is_not_published(rig, monkeypatch):
    """R5 race: the step detached its process, then cancel() arrives before release publishes the session."""
    client = rig.client()
    release = directsdk._session_release
    def racing(*args):
        client.cancel()  # lands while the session is still busy and no process is attached
        return release(*args)
    monkeypatch.setattr(directsdk, '_session_release', racing)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    monkeypatch.setattr(directsdk, '_session_release', release)
    pid = rig.spawned()[0]['pid']
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._session_pool
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'cold'


def test_routine_close_after_a_step_keeps_the_session(rig):
    client = rig.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages, stream=True)  # the stream is drained and closed by step()
    client.close()
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'


def test_reaper_start_failure_leaks_no_reservation(rig, monkeypatch):
    """A failing reaper Thread.start must not leave a busy reservation or a dead reaper object behind."""
    import threading
    monkeypatch.setattr(directsdk, '_session_cap', lambda: 1)
    monkeypatch.setattr(directsdk, '_session_reaper', None)
    real = threading.Thread
    class Failing(real):
        def start(self):
            if getattr(self, '_target', None) is directsdk._session_reap:
                raise RuntimeError('injected: cannot start thread')
            return super().start()
    monkeypatch.setattr(threading, 'Thread', Failing)
    result = step(rig.client(), [{'role': 'user', 'content': 'hi'}])
    assert result.choices[0].message.content == 'echo hi'
    assert not directsdk._session_pool and directsdk._session_reaper is None
    monkeypatch.setattr(threading, 'Thread', real)
    again = step(rig.client(), [{'role': 'user', 'content': 'hi again'}])
    assert again.usage.model_dump()['native_admission']['session'] == 'cold'
    assert len(directsdk._session_pool) == 1 and directsdk._session_reaper.is_alive()


def test_reuse_key_ignores_volatile_env_but_not_config_dir(rig, tmp_path):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = rig.client()
    first.env['CLAUDE_PID'] = '111'
    one = step(first, messages)
    messages += [as_history(one)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in one.choices[0].message.tool_calls]
    second = rig.client()
    second.env['CLAUDE_PID'] = '222'
    two = step(second, messages)
    assert two.usage.model_dump()['native_admission']['session'] == 'reused'
    messages += [as_history(two), {'role': 'user', 'content': 'next'}]
    third = rig.client()
    third.env.update(CLAUDE_PID='333', CLAUDE_CONFIG_DIR=str(tmp_path / 'elsewhere'))
    assert step(third, messages).usage.model_dump()['native_admission']['session'] != 'reused'
    assert len(rig.spawned()) == 2


# ---- v7: interplay with upstream main since ef73726 ----

@pytest.mark.parametrize('stream', [False, True])
def test_refusal_after_a_tool_call_is_content_filter_and_never_reused(rig, stream):
    """4c4cfbb/065edc7/31b591f: the session step answers a refusal as baseline does, and the session is not kept."""
    messages = [{'role': 'user', 'content': 'please refuse'}]
    client = rig.client()
    if stream:
        chunks = list(client.create(model='sonnet', messages=messages, tools=TOOLS, stream=True))
        last = chunks[-1].choices[0]
        assert last.finish_reason == 'content_filter' and last.delta.refusal == 'The request was declined.'
        assert not any(c.choices[0].delta.tool_calls for c in chunks)
        response = chunks[-1]._response
    else:
        response = step(client, messages)
    message = response.choices[0].message
    assert response.choices[0].finish_reason == 'content_filter' and message.refusal == 'The request was declined.'
    assert not message.tool_calls
    # The carrier keeps native's turn as sent, cut-off call included.
    assert [b['type'] for b in message.reasoning_details[0]['messages'][0]['content']] == ['tool_use']
    assert not directsdk._session_pool
    messages += [as_history(response), {'role': 'user', 'content': 'try again'}]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'cold'


def test_failures_carry_the_status_hermes_routes_on(rig):
    """A relayed upstream status and a native-answered limit both reach Hermes as status_code."""
    with pytest.raises(directsdk.ClaudeAPIError, match='Incomplete upstream response') as upstream:
        step(rig.client(), [{'role': 'user', 'content': 'hit the limit'}])
    assert upstream.value.status_code == 429
    client = rig.client()
    client.env['NATIVE_ERROR'] = 'rate_limit'
    with pytest.raises(directsdk.ClaudeAPIError, match='limit reached') as native:
        step(client, [{'role': 'user', 'content': 'hi'}])
    assert native.value.status_code == 429
    assert not directsdk._session_pool


def test_a_tool_call_cut_off_mid_argument_retires_the_session(rig):
    """55c1b45: the relay drops a call cut off at max_tokens, but a kept-alive native keeps it in its own transcript,
    where it would reach the API with no tool_result. The next step starts a native that replays Hermes' history."""
    events = [{'type': 'message_start', 'message': {'id': 'm1', 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': dict(USAGE)}},
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'writing'}},
              {'type': 'content_block_stop', 'index': 0},
              {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'toolu_cut', 'name': 'mcp__hermes__probe', 'input': {}}},
              {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"key":'}},
              {'type': 'content_block_stop', 'index': 1},
              {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': dict(USAGE)}, {'type': 'message_stop'}]
    rig.upstream.reply = lambda n, body: ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
    messages = [{'role': 'user', 'content': 'write the file'}]
    first = step(rig.client(), messages)
    rig.upstream.reply = None
    assert first.choices[0].finish_reason == 'length' and not first.choices[0].message.tool_calls
    assert first.choices[0].message.content.strip() == 'writing'
    assert not directsdk._session_pool
    messages += [as_history(first), {'role': 'user', 'content': 'continue'}]
    second = step(rig.client(), messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'cold'
    assert second.choices[0].message.content == 'echo continue'
    sent = [b for m in rig.upstream.bodies[-1]['messages'] for b in (m['content'] if isinstance(m['content'], list) else [])]
    assert not any(b.get('type') == 'tool_use' for b in sent)


@pytest.mark.parametrize('trigger, offered', [('ghost', 'mcp__hermes__ghost'), ('bare', 'mcp__hermes__probe')])
def test_unparked_tool_names_go_to_hermes_and_rebuild(rig, trigger, offered):
    """A call outside the inventory or a bare offered name (#39, #62): Hermes gets the call; native never parked it, so the next step replays."""
    messages = [{'role': 'user', 'content': 'call ' + trigger}]
    first = step(rig.client(), messages)
    (call,) = first.choices[0].message.tool_calls
    assert first.choices[0].finish_reason == 'tool_calls' and call.function.name == offered[len(directsdk.PREFIX):]
    assert first.choices[0].message.reasoning_details[0]['messages'][0]['content'][0]['name'] == offered
    assert not directsdk._session_pool
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': call.id, 'content': 'unknown tool'}]
    second = step(rig.client(), messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'cold'
    assert second.choices[0].message.content == 'got unknown tool'


def test_traffic_policy_and_proxy_are_part_of_launch_identity(rig, monkeypatch):
    """72249fa / 7d2ae80: a session launched under one telemetry setting or proxy route is not reused under another."""
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: True)
    messages = [{'role': 'user', 'content': 'hi'}]
    messages.append(as_history(step(rig.client(), messages)))
    messages.append({'role': 'user', 'content': 'same policy'})
    messages.append(as_history(step(rig.client(), messages)))
    assert len(rig.spawned()) == 1
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: False)
    messages.append({'role': 'user', 'content': 'telemetry off'})
    response = step(rig.client(), messages)
    assert response.usage.model_dump()['native_admission']['session'] != 'reused' and len(rig.spawned()) == 2
    messages += [as_history(response), {'role': 'user', 'content': 'via proxy'}]
    proxied = rig.client()
    proxied.env['HTTPS_PROXY'] = 'http://127.0.0.1:9'  # the loopback http upstream is never proxied
    response = step(proxied, messages)
    assert response.usage.model_dump()['native_admission']['session'] != 'reused'
    assert len(rig.spawned()) == 3
    # A user-exported DO_NOT_TRACK reaches the child unchanged (apply_traffic_policy never removes it): a change starts a new native.
    messages += [as_history(response), {'role': 'user', 'content': 'do not track'}]
    untracked = rig.client()
    untracked.env.update(HTTPS_PROXY='http://127.0.0.1:9', DO_NOT_TRACK='1')
    assert step(untracked, messages).usage.model_dump()['native_admission']['session'] != 'reused'
    assert len(rig.spawned()) == 4


def test_each_step_runs_under_its_own_timeout_and_trace_header(rig):
    """67a6b05 / 522b25e (#85): rearm takes the step's timeout; a per-step traceparent never forces a rebuild."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = rig.client().create(model='sonnet', messages=messages, tools=TOOLS, timeout=20,
                                 extra_headers={'traceparent': '00-' + '1' * 32 + '-' + '1' * 16 + '-01'})
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    second = rig.client().create(model='sonnet', messages=messages, tools=TOOLS, timeout=17,
                                  extra_headers={'traceparent': '00-' + '2' * 32 + '-' + '2' * 16 + '-01'})
    assert second.usage.model_dump()['native_admission']['session'] == 'reused'
    (session,) = directsdk._session_pool
    assert session.admission.timeout == 17


def test_killing_a_session_mode_leaves_the_shared_cwd(rig):
    """f57ddbb/8a8338b: the cwd is shared across clients; a session kill removes only its own per-session root."""
    client = rig.client()
    step(client, [{'role': 'user', 'content': 'call probe in parallel'}])
    (session,) = directsdk._session_pool
    root, cwd = session.root, Path(client._workdir())
    assert root.name.startswith('claude-directsdk-session-') and root != cwd
    directsdk._session_shutdown()
    assert not root.exists() and cwd.is_dir()


REMINDER = '\n<system-reminder>userEmail: user@example.com</system-reminder>'
MARK = {'type': 'ephemeral'}


def context_client(rig):
    client = rig.client()
    client.env['NATIVE_CONTEXT'] = '1'
    return client


def message_marks(body):
    return [(i, j) for i, m in enumerate(body['messages']) for j, b in enumerate(m['content']) if 'cache_control' in b]


def prefix(body, at):
    """The messages through block ``at`` as the cache reads them; markers are not prompt content."""
    def plain(value):
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
        return [plain(v) for v in value] if isinstance(value, list) else value
    i, j = at
    return json.dumps(plain(body['messages'][:i] + [dict(body['messages'][i], content=body['messages'][i]['content'][:j + 1])]))


def test_session_live_first_turn_and_cached_prefix_replay_unchanged(rig):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(rig), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k}
                                      for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(rig), messages)
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(context_client(rig), messages)
    assert [r.usage.model_dump()['native_admission']['session'] for r in (first, second, third)] == ['cold', 'reused', 'reused']
    assert len(rig.spawned()) == 1 and len(rig.upstream.bodies) == 3
    bodies = rig.upstream.bodies
    first_turn = (0, len(bodies[0]['messages'][0]['content']) - 1)
    assert all(prefix(body, first_turn) == prefix(bodies[0], first_turn) for body in bodies[1:])
    assert all([b['text'] for b in body['messages'][0]['content']] == ['call probe in parallel', REMINDER]
               for body in bodies)
    for before, after in zip(bodies, bodies[1:]):
        mark = message_marks(before)[-1] if message_marks(before) else (0, 0)
        assert prefix(after, mark) == prefix(before, mark)


def test_session_steps_get_mains_restoration_and_breakpoint_pinning(rig):
    """456fdb0/fda48c3/f8630df: every session request passes through main's admission rewrites, anchored on the frame
    native replays next (Hermes' frame on a launch or user turn, native's own MCP result shape on a tool round)."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(rig), messages)
    calls = first.choices[0].message.tool_calls
    # #77: put the frame first; sticky memory preserves that order on later session requests too.
    assert [b['text'] for b in rig.upstream.bodies[0]['messages'][0]['content']] == ['call probe in parallel', REMINDER]
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(rig), messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'reused'
    tool_round = rig.upstream.bodies[1]
    # Native's reminder is not forwarded as tool output; the trailing context mark moves onto the last result (#33).
    assert tool_round['messages'][2]['content'] == [
        {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': [{'type': 'text', 'text': 'APPLE'}]},
        {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': [{'type': 'text', 'text': 'BANANA'}], 'cache_control': MARK}]
    assert message_marks(tool_round) == [(1, 1), (2, 1)]
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(context_client(rig), messages)
    assert third.usage.model_dump()['native_admission']['session'] == 'reused' and len(rig.spawned()) == 1
    turn = rig.upstream.bodies[2]
    assert message_marks(turn) == [(3, 0), (4, 0)]
    # Each request replays the previous one's prefix through its last breakpoint, so that entry is read.
    for before, after in ((rig.upstream.bodies[0], tool_round), (tool_round, turn)):
        mark = message_marks(before)[-1] if message_marks(before) else (0, 0)
        assert prefix(after, mark) == prefix(before, mark)


def test_cold_replay_reads_the_session_first_turn_frame_prefix(rig):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(rig), messages)
    before = rig.upstream.bodies[0]
    assert [b['text'] for b in before['messages'][0]['content']] == ['call probe in parallel', REMINDER]
    (session,) = directsdk._session_pool
    assert session.admission.first_turn['permutation'] == (1, 1)
    directsdk._session_shutdown()
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'}
                                     for c in first.choices[0].message.tool_calls]
    second = step(context_client(rig), messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'cold' and len(rig.spawned()) == 2
    after = rig.upstream.bodies[1]
    assert [b['text'] for b in after['messages'][0]['content']] == ['call probe in parallel']
    mark = message_marks(before)[-1]
    assert mark == (0, 0) and prefix(after, mark) == prefix(before, mark)
    (fresh,) = directsdk._session_pool
    assert fresh.admission is not session.admission and fresh.admission.first_turn == {'context': None}


def test_a_cold_launch_on_a_tool_result_frame_restores_hermes_results(rig):
    """456fdb0/85dacbf: a session launch replaying Hermes' tool results forwards them without native's reminder."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(rig), messages)
    calls = first.choices[0].message.tool_calls
    directsdk._session_shutdown()  # reaped meanwhile: the next step launches on the full history
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(rig), messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'cold' and len(rig.spawned()) == 2
    assert rig.upstream.bodies[-1]['messages'][2]['content'] == [
        {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': [{'type': 'text', 'text': 'APPLE'}]},
        {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': [{'type': 'text', 'text': 'BANANA'}], 'cache_control': MARK}]


# ---- v8: review of PR #99 ----

# A native that refuses the session launch before any upstream request; its per-call launch is the usual fake.
NO_SESSION = "import sys\nif '--allowedTools' in sys.argv: print('fixture: session protocol unavailable', flush=True); sys.exit(0)\n" + NATIVE


def no_session_client(rig, tmp_path):
    native = tmp_path / 'no_session.py'
    native.write_text(NO_SESSION)
    client = rig.client()
    client.command = [sys.executable, str(native)]
    return client


@pytest.mark.parametrize('stream', [False, True])
def test_session_protocol_failure_before_admission_falls_back_per_call(rig, tmp_path, stream):
    """A session launch that dies before admission serves this step per-call: one upstream request, no kept session."""
    response = step(no_session_client(rig, tmp_path), [{'role': 'user', 'content': 'hello'}], stream=stream)
    assert response.choices[0].message.content == 'echo hello'
    assert response.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None}
    assert len(rig.upstream.bodies) == 1 and not directsdk._session_pool
    assert [('--max-turns' in s['argv']) for s in rig.spawned()] == [True]
    # A healthy session native beside it still reuses its process.
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert len(rig.spawned()) == 2 and len(directsdk._session_pool) == 1


def test_cancelled_session_failure_never_falls_back(rig, tmp_path, monkeypatch, caplog):
    """A cancel that lands while the session launch fails ends the step: no per-call replay, no upstream request."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    client = no_session_client(rig, tmp_path)
    spawn = directsdk.Client._session_spawn
    def cancelling(self, *args):
        try:
            spawn(self, *args)
        finally:
            self.cancel()
    monkeypatch.setattr(directsdk.Client, '_session_spawn', cancelling)
    with pytest.raises(RuntimeError):
        step(client, [{'role': 'user', 'content': 'hello'}])
    assert not rig.upstream.bodies and not rig.spawned() and not directsdk._session_pool
    assert 'session fallback' not in caplog.text and 'session kill' in caplog.text


# ---- large tool results: native 2.1.285 persists an MCP result past the tool's limit and keeps a stub ----

def persisted(tmp_path):
    return sorted(str(p) for p in tmp_path.rglob('tool-results/*'))


def tool_round(rig, big, is_error=False):
    """A parallel round answered with one large result and one small one; returns the history and the step's response."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': big, **({'is_error': True} if is_error else {})},
                 {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'small'}]
    sent = len(rig.upstream.bodies)
    response = step(rig.client(), messages)
    return messages, response, sent


def upstream_results(body):
    return [(b['content'] if isinstance(b['content'], str) else ''.join(x['text'] for x in b['content'])).split('\n<system-reminder>')[0]
            for m in body['messages'] if isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'tool_result']


def test_a_tool_round_over_the_message_budget_is_replayed_not_answered_over_mcp(rig, tmp_path, caplog):
    caplog.set_level('INFO', logger=directsdk.__name__)
    client = rig.client()
    client.env['FAKE_ROUND_BUDGET'] = '200000'
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    # Each has 100,000 characters but 100,001 UTF-16 units: the fake's strict >200,000 budget fires.
    texts = [letter * 99_999 + '\U0001F600' for letter in ('A', 'B')]
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': call.id, 'content': text}
                                     for call, text in zip(first.choices[0].message.tool_calls, texts)]
    sent = len(rig.upstream.bodies)
    old = rig.spawned()[0]['pid']
    response = step(rig.client(), messages)
    admission = response.usage.model_dump()['native_admission']
    results = upstream_results(rig.upstream.bodies[-1])
    assert {'mode': admission['session'], 'full_texts': results == texts,
            'stub': any('<persisted-output>' in result for result in results)} == {
                'mode': 'rebuild:oversize', 'full_texts': True, 'stub': False}
    assert admission['upstream_requests'] == 1 and len(rig.upstream.bodies) == sent + 1
    assert not alive(old) and len(rig.spawned()) == 2 and not persisted(tmp_path)
    assert 'reason=tool-result-over-mcp-limit' in caplog.text
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert len(rig.spawned()) == 2 and upstream_results(rig.upstream.bodies[-1]) == texts


def test_idle_defaults_cover_the_prompt_cache_and_stay_below_clear_tool_uses():
    # A user-idle session must outlive native's 1-hour prompt cache, or a reply inside the hour respawns native and
    # re-writes history the cache still holds; both limits stay below native's 3,900 s clear_tool_uses idle trigger.
    if 'CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_IDLE_SECONDS' not in os.environ:
        assert 3600 <= directsdk.SESSION_IDLE_SECONDS < 3900
    assert directsdk.SESSION_PARKED_IDLE_SECONDS < 3900


def test_a_session_parked_on_a_long_tool_survives_the_idle_limit(rig, monkeypatch):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    old = rig.spawned()[0]['pid']
    monkeypatch.setattr(directsdk, 'SESSION_IDLE_SECONDS', 0.2)
    monkeypatch.setattr(directsdk, 'SESSION_PARKED_IDLE_SECONDS', 3600)
    time.sleep(0.25)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': call.id, 'content': 'result'}
                                     for call in first.choices[0].message.tool_calls]
    response = step(rig.client(), messages)
    assert response.usage.model_dump()['native_admission']['session'] == 'reused'
    assert alive(old) and len(rig.spawned()) == 1
    # Settled user-idle sessions still expire at the shorter limit.
    time.sleep(0.25)
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] != 'reused'
    assert not alive(old) and len(rig.spawned()) == 2


def test_lru_evicts_a_user_idle_session_before_a_tool_parked_one(rig, monkeypatch):
    monkeypatch.setattr(directsdk, '_session_cap', lambda: 2)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    (parked,) = directsdk._session_pool
    step(rig.client(), [{'role': 'user', 'content': 'idle conversation'}])
    idle = next(session for session in directsdk._session_pool if session is not parked)
    # Explicit ordering avoids relying on clock resolution or launch timing.
    parked.last_used, idle.last_used = time.monotonic() - 1, time.monotonic()
    step(rig.client(), [{'role': 'user', 'content': 'third conversation'}])
    assert parked.alive() and not idle.alive() and len(directsdk._session_pool) == 2
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': call.id, 'content': 'result'}
                                     for call in first.choices[0].message.tool_calls]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert len(rig.spawned()) == 3


@pytest.mark.parametrize('size', [49_999, 50_001, 100_000, directsdk.SESSION_ROUND_SAFE - len('small')])
def test_large_results_reach_upstream_intact_over_mcp(rig, tmp_path, size):
    """tools/list advertises anthropic/maxResultSizeChars, so a result past native's 50,000 default is not persisted."""
    big = ''.join('line %06d of a large tool result\n' % i for i in range(size // 31 + 1))[:size]
    messages, response, sent = tool_round(rig, big)
    admission = response.usage.model_dump()['native_admission']
    assert admission['session'] == 'reused' and 'unrestored' not in admission
    assert len(rig.upstream.bodies) == sent + 1 and upstream_results(rig.upstream.bodies[-1]) == [big, 'small']
    assert response.choices[0].message.content == 'got ' + big + '+small'
    assert not persisted(tmp_path) and len(rig.spawned()) == 1
    # The session stays live with Hermes' text in its own history.
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert upstream_results(rig.upstream.bodies[-1]) == [big, 'small']


@pytest.mark.parametrize('big, is_error', [('x' * (directsdk.SESSION_RESULT_CHARS + 1), False),
                                           # 250,001 code points are 500,002 UTF-16 units, which is what native counts.
                                           ('\U0001F600' * (directsdk.SESSION_RESULT_CHARS // 2 + 1), False),
                                           ('E' * 20_000, True)], ids=['over-ceiling', 'over-ceiling-in-utf16', 'long-error'])
def test_a_result_native_would_rewrite_is_replayed_on_a_new_native(rig, tmp_path, caplog, big, is_error):
    """Never delivered over MCP: the parked session is killed and the step replays the history, one upstream request."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    messages, response, sent = tool_round(rig, big, is_error)
    admission = response.usage.model_dump()['native_admission']
    assert admission == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None, 'session': 'rebuild:oversize'}
    assert len(rig.upstream.bodies) == sent + 1 and upstream_results(rig.upstream.bodies[-1]) == [big, 'small']
    old, new = [s['pid'] for s in rig.spawned()]
    assert not alive(old) and not persisted(tmp_path)
    assert 'reason=tool-result-over-mcp-limit' in caplog.text
    # The rebuilt native holds the full text and serves the next step rig.
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    again = step(rig.client(), messages)
    assert again.usage.model_dump()['native_admission']['session'] == 'reused' and len(rig.spawned()) == 2
    assert upstream_results(rig.upstream.bodies[-1]) == [big, 'small']


def test_a_delivered_result_native_rewrote_is_answered_then_replayed(rig, tmp_path, caplog):
    """Defense in depth: a CLI that persists below the advertised limit costs this step's text, never later steps."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    client = rig.client()
    client.env['FAKE_PERSIST_LIMIT'] = '1000'
    first = step(client, messages)
    calls = first.choices[0].message.tool_calls
    big = 'B' * 2_000
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': big}, {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'small'}]
    response = step(rig.client(), messages)
    admission = response.usage.model_dump()['native_admission']
    assert admission['session'] == 'reused' and admission['unrestored']
    assert response.choices[0].message.content.startswith('got <persisted-output>') and len(persisted(tmp_path)) == 1
    assert not directsdk._session_pool and 'unrestored=1' in caplog.text and 'reason=native-rewrote-tool-result' in caplog.text
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    again = step(rig.client(), messages)
    assert again.usage.model_dump()['native_admission']['session'] == 'cold'
    assert upstream_results(rig.upstream.bodies[-1]) == [big, 'small']


def test_intact_results_forwarded_in_another_order_keep_the_session(rig):
    """Unrestored alone is no rewrite: the same texts in another order keep the session."""
    client = rig.client()
    client.env['FAKE_REVERSE'] = '1'
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'APPLE'}, {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'BANANA'}]
    response = step(rig.client(), messages)
    admission = response.usage.model_dump()['native_admission']
    assert admission['session'] == 'reused' and admission['unrestored'] and response.choices[0].message.content == 'got BANANA+APPLE'
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(rig.client(), messages).usage.model_dump()['native_admission']['session'] == 'reused'
    assert len(rig.spawned()) == 1


def _set_session_mode(value):
    """Write the setting where `hermes config set plugins.entries.<id>.settings.session_mode` puts it."""
    import directsdk_setup
    from hermes_cli.config import get_config_path
    get_config_path().write_text("plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        session_mode: %s\n"
                                 % (directsdk_setup.PLUGIN_ID, value), encoding='utf-8')


@pytest.mark.parametrize('setting, env, expected', [
    (None, {}, False), ('true', {}, True), ('false', {}, False), ('"on"', {}, True), ("''", {}, False), ('null', {}, False),
    ('true', {'CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE': '0'}, False), ('false', {'CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE': '1'}, True),
    ('true', {'CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE': ''}, True)])
def test_session_follows_the_profile_setting_unless_the_env_flag_is_set(profile, setting, env, expected):
    if setting is not None:
        _set_session_mode(setting)
    assert directsdk._session_enabled(env) is expected


def test_each_profile_of_a_multi_profile_process_reads_its_own_session_setting(profile, tmp_path):
    """A desktop backend or gateway serving several profiles loads only its launch home's .env: the setting is per profile."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    session_home = tmp_path / 'session-profile'
    session_home.mkdir()
    token = set_hermes_home_override(session_home)
    try:
        _set_session_mode('true')
        assert directsdk._session_enabled({}) is True
    finally:
        reset_hermes_home_override(token)
    assert directsdk._session_enabled({}) is False


def test_the_profile_setting_alone_turns_session_mode_on(profile, rig):
    _set_session_mode('true')
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(drop=('CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE',)), messages)
    assert first.usage.model_dump()['native_admission']['session'] == 'cold'
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': c.id[-4:]} for c in calls]
    second = step(rig.client(drop=('CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE',)), messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'reused'
    (spawned,) = rig.spawned()
    assert spawned['argv'][spawned['argv'].index('--allowedTools') + 1] == 'mcp__hermes'


# ---- session mode review: RAM-sized pool, launch-failure latch, toolUseId fallback, reaped natives ----

GIB = 1024 ** 3


@pytest.mark.parametrize('ram, setting, cap', [(64 * GIB, None, 8), (8 * GIB, None, 5), (2 * GIB, None, 1), (GIB, None, 0),
                                               (None, None, 8), (64 * GIB, 3, 3), (8 * GIB, 0, 0)])
def test_pool_cap_is_the_setting_cut_to_a_quarter_of_ram(monkeypatch, ram, setting, cap):
    monkeypatch.setattr(directsdk, '_physical_ram', lambda: ram)
    monkeypatch.setattr(directsdk, 'session_pool', lambda: setting)
    assert directsdk._session_cap() == cap


def test_physical_ram_is_read_without_a_dependency():
    assert directsdk._physical_ram() > 0


@pytest.mark.parametrize('value, expected', [('3', 3), ('0', 0), ('-1', None), ("'many'", None), ('true', None)])
def test_session_pool_setting(profile, value, expected):
    import directsdk_setup
    from hermes_cli.config import get_config_path
    get_config_path().write_text("plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        session_pool: %s\n"
                                 % (directsdk_setup.PLUGIN_ID, value), encoding='utf-8')
    assert directsdk_setup.session_pool() == expected


def test_a_low_ram_machine_runs_per_call_and_says_why_once(rig, monkeypatch, caplog):
    caplog.set_level('INFO', logger=directsdk.__name__)
    monkeypatch.setattr(directsdk, '_physical_ram', lambda: GIB)
    for text in ('one', 'two'):
        response = step(rig.client(), [{'role': 'user', 'content': text}])
        assert response.choices[0].message.content == 'echo ' + text
        assert 'session' not in response.usage.model_dump()['native_admission']
    assert [('--max-turns' in s['argv']) for s in rig.spawned()] == [True, True] and not directsdk._session_pool
    assert caplog.text.count('session mode off: the pool cap is 0') == 1


def test_two_failed_session_launches_in_a_row_turn_session_mode_off(rig, tmp_path, caplog):
    """A CLI whose control protocol changed degrades to per-call instead of retrying the launch every turn."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    broken = no_session_client(rig, tmp_path)
    assert step(broken, [{'role': 'user', 'content': 'one'}]).choices[0].message.content == 'echo one'
    # A launch that reaches admission in between resets the count.
    assert step(rig.client(), [{'role': 'user', 'content': 'healthy'}]).usage.model_dump()['native_admission']['session'] == 'cold'
    assert step(broken, [{'role': 'user', 'content': 'two'}]).choices[0].message.content == 'echo two'
    assert directsdk._session_enabled(broken.env) is True
    assert step(broken, [{'role': 'user', 'content': 'three'}]).choices[0].message.content == 'echo three'
    assert directsdk._session_enabled(broken.env) is False
    assert step(broken, [{'role': 'user', 'content': 'four'}]).choices[0].message.content == 'echo four'
    assert caplog.text.count('session fallback') == 3
    warnings = [r for r in caplog.records if r.levelname == 'WARNING' and 'session mode off for this process' in r.getMessage()]
    assert len(warnings) == 1


def answered(body):
    """The tool results of the newest native request, as (tool_use_id, text)."""
    return [(b['tool_use_id'], ''.join(x['text'] for x in b['content']) if isinstance(b['content'], list) else b['content'])
            for m in body['messages'] if isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'tool_result']


def test_tool_calls_without_tool_use_id_match_by_name_and_arguments(rig):
    client = rig.client()
    client.env['FAKE_NO_TOOL_USE_ID'] = '1'
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    a, b = first.choices[0].message.tool_calls
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': b.id, 'content': 'BANANA'},
                 {'role': 'tool', 'tool_call_id': a.id, 'content': 'APPLE'}]
    second = step(client, messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'reused'
    assert answered(rig.upstream.bodies[-1]) == [(a.id, 'APPLE'), (b.id, 'BANANA')]


def test_identical_tool_calls_without_tool_use_id_are_replayed(rig):
    client = rig.client()
    client.env['FAKE_NO_TOOL_USE_ID'] = '1'
    twins = [{'type': 'tool_use', 'id': f'toolu_twin{i}', 'name': 'mcp__hermes__probe', 'input': {'key': 'same'}} for i in (1, 2)]
    rig.upstream.reply = lambda n, body: sse(f'm{n}', twins, 'tool_use')
    messages = [{'role': 'user', 'content': 'call probe twice'}]
    first = step(client, messages)
    rig.upstream.reply = None
    one, two = first.choices[0].message.tool_calls
    assert (one.id, two.id) == ('toolu_twin1', 'toolu_twin2')
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': two.id, 'content': 'TWO'},
                 {'role': 'tool', 'tool_call_id': one.id, 'content': 'ONE'}]
    second = step(client, messages)
    # Native may dispatch identical calls in either order: which RPC was which is unknown, so the results are replayed.
    assert second.usage.model_dump()['native_admission']['session'] == 'rebuild:unpaired'
    assert sorted(answered(rig.upstream.bodies[-1])) == [('toolu_twin1', 'ONE'), ('toolu_twin2', 'TWO')]


@pytest.mark.parametrize('tool_use_id', [False, True])
def test_identical_tool_calls_dispatched_out_of_order_keep_their_results(rig, tool_use_id):
    client = rig.client()
    client.env['FAKE_REVERSE_CALLS'] = '1'
    if not tool_use_id:
        client.env['FAKE_NO_TOOL_USE_ID'] = '1'
    twins = [{'type': 'tool_use', 'id': f'toolu_twin{i}', 'name': 'mcp__hermes__probe', 'input': {'key': 'same'}} for i in (1, 2)]
    rig.upstream.reply = lambda n, body: sse(f'm{n}', twins, 'tool_use')
    messages = [{'role': 'user', 'content': 'call probe twice'}]
    first = step(client, messages)
    rig.upstream.reply = None
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': 'toolu_twin1', 'content': 'ONE'},
                 {'role': 'tool', 'tool_call_id': 'toolu_twin2', 'content': 'TWO'}]
    second = step(client, messages)
    # Named or not, identical calls are replayed before any upstream request sees a guessed pairing.
    assert second.usage.model_dump()['native_admission']['session'] == 'rebuild:unpaired'
    assert sorted(answered(rig.upstream.bodies[-1])) == [('toolu_twin1', 'ONE'), ('toolu_twin2', 'TWO')]
    assert len(rig.spawned()) == 2


def test_identical_unnamed_calls_after_a_named_one_are_replayed(rig):
    """An earlier RPC that carried claudecode/toolUseId says nothing about later ones: unnamed identical calls
    dispatched out of order must not swap results."""
    client = rig.client()
    client.env.update(FAKE_TOOL_USE_ID_RPCS='2', FAKE_REVERSE_CALLS='1')
    twins = [{'type': 'tool_use', 'id': f'toolu_twin{i}', 'name': 'mcp__hermes__probe', 'input': {'key': 'same'}} for i in (1, 2)]
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)  # two distinct calls, both named
    rig.upstream.reply = lambda n, body: sse(f'm{n}', twins, 'tool_use')
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': c.id} for c in first.choices[0].message.tool_calls]
    second = step(client, messages)  # identical calls, unnamed, dispatched B then A
    rig.upstream.reply = None
    assert second.usage.model_dump()['native_admission']['session'] == 'reused'
    messages += [as_history(second), {'role': 'tool', 'tool_call_id': 'toolu_twin1', 'content': 'ONE'},
                 {'role': 'tool', 'tool_call_id': 'toolu_twin2', 'content': 'TWO'}]
    third = step(client, messages)
    assert third.usage.model_dump()['native_admission']['session'] == 'rebuild:unpaired'
    assert sorted(answered(rig.upstream.bodies[-1]))[-2:] == [('toolu_twin1', 'ONE'), ('toolu_twin2', 'TWO')]


def test_tool_call_arguments_match_as_exact_json(rig):
    """true is not 1: Python equality would pair the second call's RPC with the first call."""
    client = rig.client()
    client.env.update(FAKE_NO_TOOL_USE_ID='1', FAKE_REVERSE_CALLS='1')
    calls = [{'type': 'tool_use', 'id': 'toolu_true', 'name': 'mcp__hermes__probe', 'input': {'value': True}},
             {'type': 'tool_use', 'id': 'toolu_one', 'name': 'mcp__hermes__probe', 'input': {'value': 1}}]
    rig.upstream.reply = lambda n, body: sse(f'm{n}', calls, 'tool_use')
    messages = [{'role': 'user', 'content': 'call probe twice'}]
    first = step(client, messages)
    rig.upstream.reply = None
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': 'toolu_true', 'content': 'TRUE'},
                 {'role': 'tool', 'tool_call_id': 'toolu_one', 'content': 'ONE'}]
    second = step(client, messages)
    assert second.usage.model_dump()['native_admission']['session'] == 'reused'
    assert sorted(answered(rig.upstream.bodies[-1])) == [('toolu_one', 'ONE'), ('toolu_true', 'TRUE')]


def test_integers_native_rounds_together_count_as_identical_calls():
    """Native parses JSON into doubles: 2**53 and 2**53 + 1 arrive as one number, so their calls are ambiguous."""
    key = directsdk._json_key
    assert key({'value': 9007199254740992}) == key({'value': 9007199254740993})
    assert key({'value': 9007199254740992}) != key({'value': 9007199254740994})
    assert key({'value': True}) != key({'value': 1}) and key({'value': 1}) == key({'value': 1.0})
    assert key({'value': 10 ** 400}) == key({'value': 10 ** 401})  # both overflow to Infinity, as in native
    session = directsdk.NativeSession('ident', None)
    session.calls = [{'type': 'tool_use', 'id': 'a', 'name': 'mcp__hermes__probe', 'input': {'value': 9007199254740992}},
                     {'type': 'tool_use', 'id': 'b', 'name': 'mcp__hermes__probe', 'input': {'value': 9007199254740993}}]
    assert session.unpaired()


def test_a_launch_admitted_between_failed_launches_resets_the_count(rig, tmp_path):
    """An admitted launch whose step failed upstream (here a 429) still proves the launch works."""
    broken = no_session_client(rig, tmp_path)
    step(broken, [{'role': 'user', 'content': 'one'}])
    with pytest.raises(directsdk.ClaudeAPIError):
        step(rig.client(), [{'role': 'user', 'content': 'hit the limit'}])
    step(broken, [{'role': 'user', 'content': 'two'}])
    assert directsdk._session_enabled(broken.env) is True


def test_an_admitted_stream_closed_early_resets_the_count(rig, tmp_path):
    """Stream.close() ends the generator with GeneratorExit, past the except path: the admitted launch still counts."""
    broken = no_session_client(rig, tmp_path)
    step(broken, [{'role': 'user', 'content': 'one'}])
    stream = rig.client().create(model='sonnet', messages=[{'role': 'user', 'content': 'hello'}], tools=TOOLS, stream=True)
    next(iter(stream))
    stream.close()
    assert directsdk._session_failures == 0
    step(broken, [{'role': 'user', 'content': 'two'}])
    assert directsdk._session_enabled(broken.env) is True


def test_a_launch_counts_once_admitted_not_when_its_stream_ends(rig, tmp_path):
    """Another chat's failed launch while an admitted stream is still open must not switch session mode off."""
    broken = no_session_client(rig, tmp_path)
    step(broken, [{'role': 'user', 'content': 'one'}])
    stream = rig.client().create(model='sonnet', messages=[{'role': 'user', 'content': 'hello'}], tools=TOOLS, stream=True)
    chunks = iter(stream)
    next(chunks)
    step(broken, [{'role': 'user', 'content': 'two'}])
    assert directsdk._session_enabled(broken.env) is True
    list(chunks)
    assert directsdk._session_failures == 1


def test_session_mode_drops_an_inherited_entrypoint(rig):
    """A desktop entrypoint makes native send a summary request the relay denies; per-call keeps it (replay test).
    Claude Code's own compaction stays off: Hermes owns the context. Since 2.1.299 either flag also stops idle
    compaction, which an attended session runs after ~54 min idle, before the session's idle reaper fires."""
    client = rig.client()
    client.env['CLAUDE_CODE_ENTRYPOINT'] = 'claude-desktop'
    step(client, [{'role': 'user', 'content': 'hello'}])
    (spawned,) = rig.spawned()
    assert spawned['entrypoint'] is None
    assert spawned['compact'] == ['1', '1']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX reaping')
def test_a_session_native_reaped_elsewhere_is_replayed_not_failed(rig):
    """Hermes' Kanban dispatcher calls os.waitpid(-1, WNOHANG) every tick and can collect an exited session native
    before its owner does (teknium's review of #89). That native counts as exited: the next turn replays on a new one."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(rig.client(), messages)
    (session,) = directsdk._session_pool
    process = session.process
    # Hold the owner's wait lock so this test, like the dispatcher, collects the exit before the session's reader.
    with process._waitpid_lock:
        os.kill(process.pid, signal.SIGKILL)
        assert os.waitpid(process.pid, 0)[0] == process.pid
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    second = step(rig.client(), messages)
    assert second.choices[0].message.content == 'got X+X'
    assert second.usage.model_dump()['native_admission']['session'] in ('cold', 'rebuild:prefix', 'rebuild:suffix')
    assert len(rig.spawned()) == 2 and len(directsdk._session_pool) == 1 and directsdk._session_pool[0] is not session



def test_session_result_never_enters_per_call_drain(rig, monkeypatch):
    def forbidden(*args):
        pytest.fail('session entered per-call drain')
    monkeypatch.setattr(directsdk, '_hand_off', forbidden)
    client = rig.client()
    messages = [{'role': 'user', 'content': 'hello'}]
    first = step(client, messages)
    process = directsdk._session_pool[0].process
    assert process.poll() is None and process not in directsdk._draining
    messages += [as_history(first), {'role': 'user', 'content': 'again'}]
    assert step(client, messages).usage.native_admission.session == 'reused'
    assert len(rig.spawned()) == 1


def test_session_restart_and_per_call_fallback_share_conversation_id(rig, monkeypatch):
    messages = [{'role': 'user', 'content': 'hello'}]
    first = step(rig.client(), messages)
    messages += [as_history(first), {'role': 'user', 'content': 'again'}]
    directsdk._session_shutdown()
    second = step(rig.client(), messages)
    messages += [as_history(second), {'role': 'user', 'content': 'fallback'}]
    directsdk._session_shutdown()
    monkeypatch.setattr(directsdk, '_session_admit', lambda session: False)
    assert 'session' not in step(rig.client(), messages).usage.model_dump()['native_admission']
    ids = [child['argv'][child['argv'].index('--session-id') + 1] for child in rig.spawned()]
    assert len(ids) == 3 and len(set(ids)) == 1


def test_session_skills_reminder_survives_reuse_and_replay(rig):
    from test_directsdk_skills_reminder import tool
    tools = [tool('probe'), tool('skill_view')]
    client = rig.client()
    messages = [{'role': 'user', 'content': 'hello'}]
    def ask():
        return client.create(model='sonnet', messages=messages, tools=tools)
    first = ask()
    messages += [as_history(first), {'role': 'user', 'content': 'again'}]
    second = ask()
    kept = json.loads(json.dumps(rig.upstream.bodies[-1]['messages']))
    assert len(rig.spawned()) == 1
    for frame in kept:
        if frame['role'] == 'user':
            assert frame['content'][-1] == {'type': 'text', 'text': directsdk.SKILLS_REMINDER}
    # Restart the same turn: old and new reminders occupy exactly the kept-native positions.
    directsdk._session_shutdown()
    ask()
    assert [m for m in rig.upstream.bodies[-1]['messages'] if m['role'] == 'user'] == [
        m for m in kept if m['role'] == 'user']
    assert len(rig.spawned()) == 2
    assert all(directsdk.SKILLS_REMINDER not in str(m) for m in messages)
    # A tool continuation carries no reminder and replay strips old assistant usage (#92).
    messages += [as_history(second), {'role': 'user', 'content': 'call probe in parallel'}]
    third = ask()
    messages += [as_history(third)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'}
                                     for c in third.choices[0].message.tool_calls]
    directsdk._session_shutdown()
    ask()
    wire = rig.upstream.bodies[-1]['messages']
    assert all('usage' not in m for m in wire if m['role'] == 'assistant')
    assert all(b.get('text') != directsdk.SKILLS_REMINDER for b in wire[-1]['content'])


@pytest.mark.parametrize('session_mode', ['0', '1'])
def test_auxiliary_one_shot_never_uses_session(rig, session_mode):
    client = rig.client()
    client.env['CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE'] = session_mode
    response = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'summary'}],
                             hermes_auxiliary=True)
    assert 'session' not in response.usage.model_dump()['native_admission']
    assert not directsdk._session_pool
    assert '--max-turns' in rig.spawned()[0]['argv']
    # The real-binary cache-wire eval separately proves the one-shot has no message mark.


@pytest.mark.parametrize('phase', ['initialize', 'replay', 'mcp'])
def test_session_stdin_writes_are_bounded_and_abort_relay(tmp_path, phase):
    import subprocess
    script = tmp_path / 'blocked.py'
    script.write_text('import time; time.sleep(30)')
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                               text=True, **directsdk._own_process_group())
    session = directsdk.NativeSession('fixture', None)
    session.process = process
    session.admission = directsdk.Admission('http://127.0.0.1:1', .15)
    row = {'type': 'user', 'message': {'role': 'user', 'content': 'x' * 1_000_000}}
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='timed out'):
            if phase == 'mcp':
                session._reply('fixture', row)
            elif phase == 'initialize':
                session.send({'type': 'control_request', 'request': row})
            else:
                session.send(row)
        assert time.monotonic() - start < 3
        assert session.admission.cancelled
        process.wait(timeout=3)
    finally:
        session.kill('test-cleanup')
        process.stdin.close()


def test_session_reader_clears_unreaped_group_before_wait(monkeypatch):
    from types import SimpleNamespace
    calls = []
    process = SimpleNamespace(stdout=[], wait=lambda: calls.append('wait'))
    session = directsdk.NativeSession('fixture', None)
    session.process = process
    monkeypatch.setattr(directsdk.os, 'waitid', lambda *args: None, raising=False)
    monkeypatch.setattr(directsdk, '_unreaped', lambda p, block=False: calls.append(('unreaped', block)) or True)
    monkeypatch.setattr(directsdk, 'kill_process_tree', lambda p: calls.append('kill'))
    session.read()
    if os.name != 'nt':
        assert calls == [('unreaped', True), 'kill', 'wait']
    assert session.events.get_nowait() is None


def test_session_kill_never_signals_a_reaped_process(monkeypatch):
    from types import SimpleNamespace
    calls = []
    process = SimpleNamespace(wait=lambda **kwargs: calls.append('wait'))
    session = directsdk.NativeSession('fixture', None)
    session.process = process
    monkeypatch.setattr(directsdk, '_unreaped', lambda p, block=False: False)
    monkeypatch.setattr(directsdk.os, 'killpg', lambda *args: pytest.fail('signalled reaped group'), raising=False)
    if os.name == 'nt':
        process.poll = lambda: 0
    session.kill('idle-reaper')
    session.kill('lru')
    assert calls == ['wait']


def test_close_during_active_session_turn_kills_native_and_next_turn_replays(rig):
    client = rig.client()
    messages = [{'role': 'user', 'content': 'hello'}]
    first = step(client, messages)
    process = directsdk._session_pool[0].process
    messages += [as_history(first), {'role': 'user', 'content': 'slow turn'}]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(step, client, messages)
        deadline = time.monotonic() + 3
        while len(rig.upstream.bodies) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(rig.upstream.bodies) == 2
        client.close()
        with pytest.raises(RuntimeError, match='cancelled'):
            future.result(timeout=3)
    process.wait(timeout=3)
    assert not directsdk._session_pool
    assert step(rig.client(), messages).usage.native_admission.session == 'cold'
