"""register(ctx): ``hermes import-agent`` keeps core's spelling and flags, and stays inert on a core
that still ships its own copy (two subparsers of one name would abort plugin-command wiring)."""

from __future__ import annotations

import argparse

import pytest

import agent_importers_plugin


class RecordingCtx:
    def __init__(self):
        self.cli = []

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)


def _register(monkeypatch, core_ships: bool) -> RecordingCtx:
    monkeypatch.setattr(agent_importers_plugin, "_core_ships_import_agent", lambda: core_ships)
    ctx = RecordingCtx()
    agent_importers_plugin.register(ctx)
    return ctx


def test_registers_import_agent_with_cores_flags(monkeypatch):
    (cmd,) = _register(monkeypatch, core_ships=False).cli
    assert cmd["name"] == "import-agent"
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command").add_parser(cmd["name"])
    cmd["setup_fn"](sub)
    sub.set_defaults(func=cmd["handler_fn"])
    args = parser.parse_args(["import-agent", "codex", "--source", "/x", "--dry-run", "--overwrite", "-y"])
    assert (args.agent, args.source, args.dry_run, args.overwrite, args.yes, args.sync) == (
        "codex", "/x", True, True, True, False)
    assert parser.parse_args(["import-agent", "--sync"]).sync is True
    with pytest.raises(SystemExit):
        parser.parse_args(["import-agent", "windsurf"])


def test_inert_on_a_core_that_still_ships_import_agent(monkeypatch):
    assert _register(monkeypatch, core_ships=True).cli == []


def test_real_discovery_matches_the_running_core(tmp_path, monkeypatch):
    """Loaded the way users get it: copied into the profile's plugins/ and enabled in config.yaml."""
    import shutil

    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    shutil.copytree(agent_importers_plugin.__path__[0], home / "plugins" / "agent-importers",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text("plugins:\n  enabled: [agent-importers]\n", encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    registered = "import-agent" in manager._cli_commands
    assert registered is not agent_importers_plugin._core_ships_import_agent()
