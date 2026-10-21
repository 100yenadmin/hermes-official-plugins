"""agent-importers — ``hermes import-agent`` for Hermes Agent.

Imports another coding agent's setup (instructions, permission rules, MCP servers, skills,
memories) into the active Hermes profile, preview first, secrets never. Formerly built into
hermes-agent core (``hermes_cli/agent_import.py``); the command spelling, its flags and the
per-profile sync manifest (``HERMES_HOME/import-sync.json``) are unchanged.

Wiring goes only through the public plugin API (``ctx.register_cli_command``).
"""

from __future__ import annotations

from pathlib import Path

from .cli import DESCRIPTION, HELP, handle_cli, setup_cli


def _core_ships_import_agent() -> bool:
    """A core from before the move still registers its own ``hermes import-agent``; adding a second
    subparser of that name would abort the CLI's plugin-command wiring, so this copy stays inert
    there. Looked up next to the running core's own modules, so an editable install of another
    checkout cannot answer for it."""
    from hermes_cli import config
    return (Path(config.__file__).parent / "agent_import.py").is_file()


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    if _core_ships_import_agent():
        return
    ctx.register_cli_command(name="import-agent", help=HELP, description=DESCRIPTION,
                             setup_fn=setup_cli, handler_fn=handle_cli)


__all__ = ["register"]
