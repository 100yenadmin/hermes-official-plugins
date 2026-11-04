"""Request-local early interim delivery, with the returned native response untouched."""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent_runtime_helpers import strip_think_blocks
from agent.chat_completion_helpers import _assistant_content_for_storage
from agent.conversation_loop import _STALE_MARKER_RE
from agent.stream_delivery import StreamDeliveryMixin

import directsdk
import directsdk_setup
import test_directsdk_session as T
from test_directsdk_session import rig  # noqa: F401


class Agent(StreamDeliveryMixin):
    """Hermes' own interim delivery and dedup. ``messages``: the plugin's emits; ``recorded``: its direct
    records; ``posts``: what reached the chat."""

    def __init__(self, fail=None):
        self.messages, self.recorded, self.posts = [], [], []
        self.fail = fail
        self.stopped = False
        self._in_hermes = False
        self.interim_assistant_callback = self._post

    def _post(self, text, already_streamed=False):
        if self.fail == 'callback':
            self.fail = None  # one failed delivery, then the gateway recovers
            raise RuntimeError('synthetic callback failure')
        self.posts.append(text)

    def _strip_think_blocks(self, content):
        return strip_think_blocks(self, content)

    def _enqueue_stream_hook(self, event, **fields):
        pass

    def _emit_interim_assistant_message(self, message):
        self.messages.append((message, self.stopped))
        if self.fail == 'emit':
            raise RuntimeError('synthetic emitter failure')
        self._hermes(StreamDeliveryMixin._emit_interim_assistant_message, message)

    def _record_delivered_interim_text(self, text):
        if not self._in_hermes:
            if self.fail == 'record':
                raise RuntimeError('synthetic recorder failure')
            self.recorded.append(text)
        StreamDeliveryMixin._record_delivered_interim_text(self, text)

    def _hermes(self, method, *args):
        self._in_hermes = True
        try:
            method(self, *args)
        finally:
            self._in_hermes = False

    def step_end(self, message):
        """Hermes' own interim copy of a tool step (turn_tool_round): stored content, the bare-marker drop, emit."""
        content = _assistant_content_for_storage(self, message)
        if message.tool_calls and _STALE_MARKER_RE.fullmatch((message.content or '').strip()):
            content = ''
        self._hermes(StreamDeliveryMixin._emit_interim_assistant_message, {'role': 'assistant', 'content': content})


@pytest.fixture
def opener(rig, tmp_path, monkeypatch, caplog):
    # The shared fake normally omits block-start events on stdout. Emit native's real boundary,
    # and split each text into two deltas to check that the exact accumulated string is handed.
    source = T.NATIVE.replace(
        "    for block in message['content']:\n",
        "    for block in message['content']:\n"
        "        out({'type':'stream_event','event':{'type':'content_block_start','content_block':block}})\n")
    # The shared fake leaves text-delta assembly to the relay; opener tests need that text on stdout.
    source = source.replace(
        "            elif event['type'] == 'content_block_delta' and event['delta']['type'] == 'input_json_delta':",
        "            elif event['type'] == 'content_block_delta' and event['delta']['type'] == 'text_delta':\n"
        "                message['content'][event['index']]['text'] += event['delta']['text']\n"
        "            elif event['type'] == 'content_block_delta' and event['delta']['type'] == 'input_json_delta':")
    source = source.replace(
        "            out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':block['text']}}})",
        "            for text in (block['text'][:3], block['text'][3:]):\n"
        "                out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':text}}})")
    (tmp_path / 'native.py').write_text(source)
    # Pin the new setting without reading an operator profile.
    monkeypatch.setattr(directsdk, 'early_opener_enabled', lambda: True)
    caplog.set_level(logging.DEBUG, logger=directsdk._session_log.name)
    return rig


def text(value):
    return {'type': 'text', 'text': value}


def call(index=1):
    return {'type': 'tool_use', 'id': f'toolu_{index}', 'name': 'mcp__hermes__probe', 'input': {'key': 'a'}}


def run(rig, blocks, agent=None, stop='tool_use', stream=False, **kwargs):
    # ``agent`` deliberately lives on this synchronous caller's stack, as it does in Hermes.
    rig.upstream.reply = lambda n, body: T.sse('opener-message', blocks, stop)
    client = rig.client()
    return client.create(model='sonnet', messages=[{'role': 'user', 'content': 'opener test'}],
                         tools=T.TOOLS, stream=stream, **kwargs)


def watch_stop(monkeypatch, target):
    receive = directsdk.NativeSession.receive

    def watched(self, request, timeout):
        event = receive(self, request, timeout)
        if event and event.get('type') == 'stream_event' and event['event']['type'] == 'message_stop':
            target.stopped = True
        return event

    monkeypatch.setattr(directsdk.NativeSession, 'receive', watched)


@pytest.mark.parametrize('stream', [False, True], ids=['response', 'stream'])
def test_opener_before_stop_exact_text_and_response_unchanged(opener, monkeypatch, caplog, stream):
    value = '  Checking PRIVATE_SENTINEL now.\n'
    agent = Agent()
    watch_stop(monkeypatch, agent)
    lookup = directsdk._hermes_interim_agent
    looked_up = []

    def counted_lookup():
        looked_up.append(True)
        return lookup()

    monkeypatch.setattr(directsdk, '_hermes_interim_agent', counted_lookup)
    result = run(opener, [text(value), call()], agent, stream=stream)
    assert looked_up == [True]
    if stream:
        # Iteration happens after run() returned: the lookup must already have captured its agent.
        result = list(result)[-1]._response
    # Hermes' stored form of the text: stripped, as its own step-end copy is.
    assert agent.messages == [({'role': 'assistant', 'content': value.strip()}, False)]
    assert agent.recorded == []
    assert any(r.getMessage().startswith(f'session opener: handed chars={len(value.strip())} after=')
               and r.getMessage().endswith('s') for r in caplog.records)
    assert 'PRIVATE_SENTINEL' not in caplog.text
    assert 'session opener: tail' not in caplog.text
    agent.step_end(result.choices[0].message)
    assert agent.posts == [value.strip()]
    directsdk._session_shutdown()
    # Use the identical fake response and launch identity for a byte-for-byte response comparison.
    control_agent = Agent()
    control_agent.interim_assistant_callback = None
    control = run(opener, [text(value), call()], control_agent)
    assert json.dumps(result.model_dump(), sort_keys=True) == json.dumps(control.model_dump(), sort_keys=True)


def test_two_tools_then_tail_handed_and_full_text_recorded(opener, caplog):
    agent = Agent()
    result = run(opener, [text('Checking.'), call(), call(2), text(' Also checking.')], agent)
    assert [m for m, _ in agent.messages] == [
        {'role': 'assistant', 'content': 'Checking.'},
        {'role': 'assistant', 'content': ' Also checking.'}]
    assert agent.recorded == ['Checking. Also checking.']
    assert result.choices[0].message.content == 'Checking. Also checking.'
    assert caplog.text.count('session opener: handed') == 1
    assert 'session opener: tail chars=15' in caplog.text
    agent.step_end(result.choices[0].message)
    assert agent.posts == ['Checking.', 'Also checking.']


def test_blank_tail_leaves_hermes_copy_deduplicated(opener, caplog):
    agent = Agent()
    result = run(opener, [text('Checking.'), call(), text(' \n')], agent)
    assert [m['content'] for m, _ in agent.messages] == ['Checking.']
    # Hermes' stored copy of the step equals the opener, which it already recorded as shown.
    assert agent.recorded == []
    assert 'session opener: tail' not in caplog.text
    agent.step_end(result.choices[0].message)
    assert agent.posts == ['Checking.']


@pytest.mark.parametrize('tail', ['', ' Then PASSWORD=hunter2hunter2hunter2 too.'], ids=['opener', 'tail'])
def test_secret_shaped_text_is_shown_redacted_and_once(opener, caplog, tail):
    agent = Agent()
    value = 'Setting GITHUB_TOKEN=abc123def456ghi789jkl now.'
    blocks = [text(value), call()] + ([text(tail)] if tail else [])
    result = run(opener, blocks, agent)
    stored = _assistant_content_for_storage(agent, result.choices[0].message)
    assert 'abc123def456ghi789jkl' not in stored  # Hermes redacts it; the test would prove nothing otherwise
    agent.step_end(result.choices[0].message)
    # The opener, then the tail Hermes' copy would have added: together exactly Hermes' stored text.
    assert ' '.join(agent.posts) == stored and len(agent.posts) == (2 if tail else 1)
    assert not any(secret in post for post in agent.posts for secret in ('abc123def456ghi789jkl', 'hunter2'))
    assert 'abc123def456ghi789jkl' not in caplog.text


def test_bare_marker_opener_is_left_to_hermes(opener):
    agent = Agent()
    result = run(opener, [text('[memory]'), call()], agent)
    assert agent.messages == agent.recorded == []
    agent.step_end(result.choices[0].message)
    assert agent.posts == []


def test_text_already_shown_this_turn_keeps_hermes_message(opener):
    agent = Agent()
    StreamDeliveryMixin._record_delivered_interim_text(agent, 'Checking.')
    result = run(opener, [text('Checking.'), call(), text(' Also X.')], agent)
    assert agent.messages == agent.recorded == []
    agent.step_end(result.choices[0].message)
    assert agent.posts == ['Checking. Also X.']


def test_undelivered_opener_is_not_counted(opener, caplog):
    agent = Agent('callback')
    result = run(opener, [text('Checking.'), call(), text(' More.')], agent)
    assert [m['content'] for m, _ in agent.messages] == ['Checking.']
    assert agent.recorded == [] and agent.posts == []
    assert 'session opener: handed' not in caplog.text
    # Hermes' own step-end copy then shows the whole text.
    agent.step_end(result.choices[0].message)
    assert agent.posts == ['Checking. More.']


@pytest.mark.parametrize('case', ['thinking', 'blank', 'final'])
def test_no_text_or_no_tool_start_has_no_opener(opener, monkeypatch, case):
    agent = Agent()
    if case == 'thinking':
        # Keep thinking on the existing stream branch; it must not become opener text.
        receive = directsdk.NativeSession.receive
        pending = []

        def thinking(self, request, timeout):
            if pending:
                return pending.pop()
            event = receive(self, request, timeout)
            if event and event.get('type') == 'stream_event' and event['event']['type'] == 'content_block_start':
                pending.append(event)
                event = {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                         'delta': {'type': 'thinking_delta', 'thinking': 'private thought'}}}
            return event

        monkeypatch.setattr(directsdk.NativeSession, 'receive', thinking)
        blocks, stop = [call()], 'tool_use'
    elif case == 'blank':
        blocks, stop = [text(' \n'), call()], 'tool_use'
    else:
        blocks, stop = [text('Final answer.')], 'end_turn'
    run(opener, blocks, agent, stop=stop)
    assert agent.messages == agent.recorded == []


@pytest.mark.parametrize('tail', ['', ' Continued.'], ids=['equals', 'longer'])
def test_dropped_cutoff_call_no_tail_or_record(opener, monkeypatch, caplog, tail):
    agent = Agent()
    watch_stop(monkeypatch, agent)
    # Start a call on stdout, but let the relay retain only final text, like a cut-off argument.
    native = opener.client().command[1]
    source = Path(native).read_text()
    anchor = "        out({'type':'assistant','message':dict(message, content=[block])})"
    source = source.replace(anchor, anchor + "\n        if block is message['content'][0]:\n"
                            "            out({'type':'stream_event','event':{'type':'content_block_start','content_block':"
                            + repr(call()) + "}})")
    Path(native).write_text(source)
    # A final-only response has no real tool calls even though stdout announced a tool block.
    result = run(opener, [text('Checking.'), text(tail)], agent, stop='end_turn')
    assert agent.messages == [({'role': 'assistant', 'content': 'Checking.'}, False)]
    assert agent.recorded == []
    assert result.choices[0].message.tool_calls is None
    assert ('session opener: final equals opener' in caplog.text) is (not tail)


@pytest.mark.parametrize('failure', ['emit', 'record'])
def test_agent_exception_fails_open(opener, caplog, failure):
    agent = Agent(failure)
    blocks = [text('Checking.'), call(), call(2), text(' More.')]
    result = run(opener, blocks, agent)
    directsdk._session_shutdown()
    control_agent = Agent()
    control_agent.interim_assistant_callback = None
    control = run(opener, blocks, control_agent)
    assert result.model_dump() == control.model_dump()
    assert agent.recorded == []
    assert sum(m['content'] == 'Checking.' for m, _ in agent.messages) == 1
    assert 'session opener:' in caplog.text


def test_missing_agent_and_disabled_option(opener, monkeypatch):
    assert directsdk._hermes_interim_agent() is None
    run(opener, [text('Checking.'), call()])
    directsdk._session_shutdown()
    agent = Agent()
    monkeypatch.setattr(directsdk, 'early_opener_enabled', lambda: False)
    monkeypatch.setattr(directsdk, '_hermes_interim_agent', lambda: pytest.fail('disabled lookup'))
    run(opener, [text('Checking.'), call()], agent)
    assert agent.messages == agent.recorded == []


def test_lookup_nearest_callbackless_agent_stops_search():
    agent = Agent()

    def nearer():
        agent = Agent()
        agent.interim_assistant_callback = None
        return directsdk._hermes_interim_agent()

    assert nearer() is None


def test_lookup_two_frames_up_and_64_frame_bound():
    agent = Agent()

    def recurse(depth):
        return recurse(depth - 1) if depth else directsdk._hermes_interim_agent()

    assert recurse(0) is agent
    assert recurse(62) is agent  # 63 recursive frames plus the owner frame.
    assert recurse(63) is None


@pytest.mark.parametrize('missing', ['_emit_interim_assistant_message', '_record_delivered_interim_text',
                                     '_interim_text_was_delivered'])
def test_lookup_missing_method_is_ignored(missing):
    agent = SimpleNamespace(interim_assistant_callback=lambda: None,
                            _emit_interim_assistant_message=lambda message: None,
                            _record_delivered_interim_text=lambda value: None,
                            _interim_text_was_delivered=lambda value: False)
    setattr(agent, missing, None)
    assert directsdk._hermes_interim_agent() is None


def test_lookup_exception_fails_open():
    class Broken:
        def __getattribute__(self, name):
            raise RuntimeError('synthetic attribute failure')

    agent = Broken()
    assert directsdk._hermes_interim_agent() is None


@pytest.mark.parametrize('setting, expected', [(None, True), (False, False), ('off', False), (True, True)])
def test_early_opener_setting_defaults_on(monkeypatch, setting, expected):
    monkeypatch.setattr(directsdk_setup, '_setting', lambda key: setting if key == 'early_opener' else None)
    assert directsdk_setup.early_opener_enabled() is expected
