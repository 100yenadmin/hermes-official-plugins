"""Trace attribution: which profile a turn ran for (hermes-agent#135213).

Several profiles can share one Langfuse project (and one gateway, when multiplexed). Driven
through the real ``pre_api_request`` hook against the per-test HERMES_HOME; only the SDK client
is replaced by a recorder.
"""
from __future__ import annotations

import contextlib
import importlib
import sys

import pytest


class _Span:
    def update(self, **kw):
        pass

    def update_trace(self, **kw):
        pass

    def end(self, **kw):
        pass

    def start_observation(self, **kw):
        return _Span()


class _RootCM:
    def __enter__(self):
        return _Span()

    def __exit__(self, *exc):
        return False


class _Client:
    def __init__(self, roots):
        self._roots = roots

    def create_trace_id(self, seed=None):
        return f"trace::{seed}"

    def start_as_current_observation(self, **kw):
        self._roots.append(kw)
        return _RootCM()

    def flush(self):
        pass


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """Plugin wired to a recording client; returns (module, roots, propagated, home)."""
    home = tmp_path / "hermes"
    (home / "profiles" / "arkana").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))  # so HERMES_HOME (tmp/hermes) is ~/.hermes
    for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    roots: list = []
    propagated: list = []

    @contextlib.contextmanager
    def _propagate(**kw):
        propagated.append(kw)
        yield

    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client(roots))
    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    # Register through a real PluginContext: profile / cron identity come from its public seams.
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    mod.register(PluginContext(PluginManifest(name="langfuse"), PluginManager()))
    mod._TRACE_STATE.clear()
    return mod, roots, propagated, home


def _open_turn(mod, session="s1", platform="cli"):
    mod.on_pre_llm_request(
        task_id=session, session_id=session, platform=platform, model="m", provider="p", api_mode="chat",
        api_call_count=1, request_messages=[{"role": "user", "content": "hi"}],
        turn_id=f"{session}:t1", api_request_id=f"{session}:t1:api:1",
    )


def test_default_profile_is_named(traced):
    mod, roots, propagated, _ = traced
    _open_turn(mod)
    assert roots[0]["metadata"]["profile"] == "default"
    assert propagated[0]["metadata"] == {"profile": "default"}


def test_routed_turn_names_the_served_profile_not_the_launch_one(traced, monkeypatch):
    mod, roots, propagated, home = traced
    # The launch profile's env pin must not relabel a routed turn.
    monkeypatch.setenv("HERMES_PROFILE", "default")
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home / "profiles" / "arkana"))
    try:
        _open_turn(mod, session="s2")
    finally:
        reset_hermes_home_override(token)
    assert roots[0]["metadata"]["profile"] == "arkana"
    assert propagated[0]["metadata"] == {"profile": "arkana"}


# Platform / trigger / cron-job tags (hermes-agent#102755, #74930, #88502).

def test_interactive_turn_is_tagged_with_platform_and_trigger(traced):
    mod, _, propagated, _ = traced
    # A cron-shaped session id outside a cron execution must not be tagged with a job.
    _open_turn(mod, session="cron_lookalike_20260101_000000", platform="Telegram")
    assert propagated[0]["tags"] == ["hermes", "langfuse", "platform:telegram", "trigger:interactive"]


def test_cron_run_is_tagged_with_its_stable_job_id(traced):
    mod, _, propagated, _ = traced
    from cron.execution_identity import enter_cron_execution, exit_cron_execution

    # The scheduler binds the running job; legacy / hand-edited ids (not 12-hex) are valid too.
    token = enter_cron_execution({"id": "test-job"}, "exec-1", {"source": "direct"})
    try:
        _open_turn(mod, session="cron_test-job_20261012_093000", platform="cron")
    finally:
        exit_cron_execution(token)
    assert propagated[0]["tags"] == ["hermes", "langfuse", "platform:cron", "trigger:cron", "cron-job:test-job"]


# Responses API tool items in generation input (hermes-agent#84186).

_RESPONSES_ITEMS = [
    {"type": "function_call", "call_id": "call-1", "name": "web_extract",
     "arguments": '{"urls": ["https://example.com"]}'},
    {"type": "function_call_output", "call_id": "call-1", "output": '{"ok": true}'},
]


def test_serialize_messages_preserves_responses_tool_items():
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    assert mod._serialize_messages(_RESPONSES_ITEMS) == [
        {"type": "function_call", "call_id": "call-1", "name": "web_extract",
         "arguments": {"urls": ["https://example.com"]}},
        {"type": "function_call_output", "call_id": "call-1", "output": {"ok": True}},
    ]


def test_responses_tool_payloads_follow_metadata_capture_mode(monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "metadata")
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    call, output = mod._serialize_messages(_RESPONSES_ITEMS)
    assert call["arguments"] == {"omitted": True, "type": "text", "chars": 33}
    assert output["output"]["omitted"] is True and output["call_id"] == "call-1"
