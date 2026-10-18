"""A restarted session replays the kept-alive opening byte for byte when skill_view is offered.

Native puts its account context in front of the opening user turn; the relay moves it behind the queried frame. The
queried frame must include the skills reminder, or a kept-alive opening reads [host, context, reminder] while a restart
rebuilds [host, reminder, context] and every restart rewrites the whole cached history.
"""
import test_directsdk_session as T
from test_directsdk_session import as_history, rig  # noqa: F401
from test_directsdk_skills_reminder import tool

import directsdk

_OLD_CONTEXT = """    if first_query and os.environ.get('NATIVE_CONTEXT') and len(history) == 1:
        history[0]['content'].insert(0, {'type':'text','text':reminder})"""
# A restarted native also opens with its account context, in front of the newest user turn.
_NEW_CONTEXT = """    prepend_context = first_query and os.environ.get('NATIVE_CONTEXT') and (
        len(history) == 1 or os.environ.get('NATIVE_RESTART_CONTEXT'))
    if prepend_context:
        history[-1]['content'].insert(0, {'type':'text','text':reminder})"""
_OLD_TOOL_TAIL = "    if len(messages) > 1 and newest[-1]['type'] == 'tool_result':"
_OLD_USER_TAIL = "    elif len(messages) > 1:\n        newest.append"


def _restart_context_native():
    native = T.NATIVE
    assert _OLD_CONTEXT in native and _OLD_TOOL_TAIL in native and _OLD_USER_TAIL in native
    native = native.replace(_OLD_CONTEXT, _NEW_CONTEXT)
    native = native.replace(_OLD_TOOL_TAIL, "    if not prepend_context and len(messages) > 1 and newest[-1]['type'] == 'tool_result':")
    return native.replace(_OLD_USER_TAIL, "    elif not prepend_context and len(messages) > 1:\n        newest.append")


def _opening(body):
    return [{k: v for k, v in block.items() if k != 'cache_control'} for block in body['messages'][0]['content']]


def test_a_restart_replays_the_kept_alive_opening_with_the_skills_reminder(rig, tmp_path):
    (tmp_path / 'native.py').write_text(_restart_context_native())
    tools = T.TOOLS + [tool('skill_view')]
    client = rig.client()
    client.env.update(NATIVE_CONTEXT='1', NATIVE_RESTART_CONTEXT='1')
    history = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = client.create(model='sonnet', messages=history, tools=tools)
    kept = _opening(rig.upstream.bodies[0])
    assert len(kept) == 3  # host text, native's account context, the skills reminder
    calls = first.choices[0].message.tool_calls
    history += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': t} for c, t in zip(calls, ('A', 'B'))]
    second = client.create(model='sonnet', messages=history, tools=tools)
    assert second.usage.native_admission.session == 'reused'
    assert _opening(rig.upstream.bodies[-1]) == kept
    history += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    directsdk._session_shutdown()
    restarted = client.create(model='sonnet', messages=history, tools=tools)
    assert restarted.usage.native_admission.session == 'cold'
    assert _opening(rig.upstream.bodies[-1]) == kept
