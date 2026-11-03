"""Claude Code's own memory and instructions never reach Claude through native (#195).

Native loads its auto memory (``MEMORY.md``) and managed CLAUDE.md even with ``--setting-sources ''``
and, on a subscription login, prepends them to the conversation's first user turn, where Claude reads
Claude Code's notes as text smuggled into the user's message. The child is told not to load them.
Session mode's placement of native's remaining first-turn context is unchanged (test_admission_first_turn).
"""
from test_directsdk_telemetry import _turn_env


def test_native_is_told_not_to_load_its_memory_or_claude_md(profile, tmp_path):
    # "0" forces auto memory on in Claude Code; Hermes' own setting must still win.
    env = _turn_env(profile, tmp_path, CLAUDE_CODE_DISABLE_AUTO_MEMORY='0')
    assert env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] == '1'
    assert env['CLAUDE_CODE_DISABLE_CLAUDE_MDS'] == '1'
