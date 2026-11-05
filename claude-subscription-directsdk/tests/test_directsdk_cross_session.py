"""Native refuses messages from other Claude Code sessions on the machine.

Every ``claude -p`` child registers in ``~/.claude/sessions`` with a message socket, and other sessions'
ListAgents lists it. With ``--setting-sources ''`` it accepts cross-session messages by default: an idle
native runs a turn on one, and one sent while a tool runs joins that turn's next request. The settings
file the plugin passes with ``--settings`` (which Claude Code reads whatever the setting sources) tells
native to refuse them, per call and in session mode.
"""
import json

import pytest

import directsdk


@pytest.mark.parametrize("session", [False, True])
def test_native_refuses_cross_session_messages(tmp_path, session):
    body = json.dumps({"max_tokens": 64})
    command = directsdk.native_command(["claude"], "sonnet", tmp_path, body, "system", {"mcpServers": {}}, {}, session=session)
    path = command[command.index("--settings") + 1]
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert path == str(tmp_path / "settings.json")
    assert settings["crossSessionInbound"] == "refuse"
    assert settings["env"] == {"CLAUDE_CODE_EXTRA_BODY": body}
