"""``hermes import-agent``: argparse tree and the preview → confirm → apply flow."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .importer import AGENT_DEFAULT_DIRS, SUPPORTED_AGENTS, AgentImporter, detect_agents

logger = logging.getLogger(__name__)

HELP = "Import a Claude Code, Codex CLI or Cursor setup into Hermes"
DESCRIPTION = (
    "One-command import of another coding agent's setup into Hermes. Maps CLAUDE.md/AGENTS.md "
    "instructions, Cursor rules, permission allowlists, MCP servers, skills, and memories into their Hermes "
    "equivalents. Always shows a preview before making changes. API keys and credentials are "
    "never imported — run 'hermes setup' for those.")

_ANSI = {"green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "dim": "\033[2m",
         "magenta": "\033[35m"}


def _color(text: str, name: str) -> str:
    return f"{_ANSI[name]}{text}\033[0m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else text


def _dirs_hint() -> str:
    dirs = [f"~/{AGENT_DEFAULT_DIRS[a]}" for a in SUPPORTED_AGENTS]
    return ", ".join(dirs[:-1]) + f" or {dirs[-1]}"


def setup_cli(parser) -> None:
    """Attach the ``import-agent`` arguments (same spelling core shipped)."""
    parser.add_argument(
        "agent", nargs="?", choices=list(SUPPORTED_AGENTS),
        help=f"Which agent to import from (default: auto-detect {_dirs_hint()})")
    parser.add_argument(
        "--source", help=f"Path to the agent's config directory (default: {_dirs_hint()})")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only — stop after showing what would be imported")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing Hermes items on name conflicts (default: skip)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    parser.add_argument(
        "--sync", action="store_true",
        help="Re-import every previously imported source whose files changed since the last "
             "import (registered in HERMES_HOME/import-sync.json). Runs without prompts; "
             "combine with --dry-run to preview.")


def handle_cli(args) -> None:
    import_agent_command(args)


def import_agent_command(args) -> None:
    """Handle ``hermes import-agent``."""
    from hermes_cli.cli_output import (print_error, print_header, print_info, print_success,
                                       prompt_yes_no)
    from hermes_cli.config import get_config_path, load_config, save_config
    from hermes_constants import display_hermes_home, get_hermes_home

    if getattr(args, "sync", False):
        from .sync import sync_imported_agents
        sync_imported_agents(args)
        return

    agent, explicit_source, overwrite = args.agent, args.source, args.overwrite

    if agent is None:
        detected = detect_agents()
        if not detected:
            print()
            print_error(f"No supported agent setup found ({_dirs_hint()}).")
            print_info("Specify one explicitly: hermes import-agent claude-code --source /path")
            return
        if len(detected) > 1 and explicit_source is None:
            print()
            print_info("Multiple agent setups detected: " + ", ".join(detected))
            print_info("Pick one: " + "   or   ".join(f"hermes import-agent {a}" for a in detected))
            return
        agent = detected[0]
    source_dir = Path(explicit_source or Path.home() / AGENT_DEFAULT_DIRS[agent])

    print()
    print(_color("┌─────────────────────────────────────────────────────────┐", "magenta"))
    print(_color("│          ☤ Hermes — Import From Another Agent          │", "magenta"))
    print(_color("└─────────────────────────────────────────────────────────┘", "magenta"))
    if not source_dir.is_dir():
        print()
        print_error(f"Agent directory not found: {source_dir}")
        print_info(f"Specify a custom path: hermes import-agent {agent} --source /path/to/{AGENT_DEFAULT_DIRS[agent]}")
        return
    hermes_home = get_hermes_home()
    print()
    print_header("Import Settings")
    print_info(f"Agent:       {agent}")
    print_info(f"Source:      {source_dir}")
    print_info(f"Target:      {hermes_home}")
    print_info(f"Overwrite:   {'yes' if overwrite else 'no (skip conflicts)'}")
    print_info("Secrets:     never imported — run 'hermes setup' for credentials")
    # Ensure config.yaml exists before the import tries to merge into it
    if not get_config_path().exists():
        save_config(load_config())

    def run_import(execute: bool, phase: str) -> Optional[dict[str, Any]]:
        """Run the importer; on failure print the error and return None."""
        try:
            return AgentImporter(agent, source_dir.resolve(), hermes_home.resolve(),
                                 execute=execute, overwrite=overwrite).run()
        except Exception as e:
            print()
            print_error(f"Import{phase} failed: {e}")
            logger.debug(f"import-agent{phase} error", exc_info=True)
            return None

    # Phase 1: preview (always)
    preview = run_import(False, " preview")
    if preview is None:
        return
    summary = preview.get("summary", {})
    if summary.get("imported", 0) == 0 and summary.get("conflict", 0) == 0:
        print()
        print_info(f"Nothing to import from {agent}.")
        print_import_report(preview, dry_run=True)
        return
    print()
    print_header(f"Import Preview — {summary.get('imported', 0)} item(s) would be imported")
    print_info("No changes have been made yet. Review the list below:")
    print_import_report(preview, dry_run=True)
    if args.dry_run:
        return

    # Phase 2: confirm and execute
    print()
    if not args.yes:
        if not sys.stdin.isatty():
            print_info("Non-interactive session — preview only.")
            print_info(f"To execute, re-run with: hermes import-agent {agent} --yes")
            return
        if not prompt_yes_no("Proceed with import?", default=True):
            print_info("Import cancelled.")
            return
    report = run_import(True, "")
    if report is None:
        return
    print_import_report(report, dry_run=False)
    from .sync import update_sync_manifest
    try:
        update_sync_manifest(agent, source_dir.resolve(), hermes_home.resolve(), overwrite, report)
        print_info("Source registered for sync — re-run 'hermes import-agent --sync' "
                   "any time to pull in changes.")
    except OSError as exc:
        logger.warning("Could not update import sync manifest: %s", exc)
    print()
    print_success("Import complete.")
    print_info("API keys and credentials were NOT imported — run 'hermes setup' "
               f"to configure providers, or add them to {display_hermes_home()}/.env.")


def print_import_report(report: dict[str, Any], dry_run: bool) -> None:
    """Print a formatted per-item import report."""
    from hermes_cli.cli_output import print_header, print_info

    print()
    print_header("Dry Run Results" if dry_run else "Import Results")
    if dry_run:
        print_info("No files were modified. This is a preview of what would happen.")
    print()
    # (status, colour, group heading, summary label)
    groups = (
        ("imported", "green", "✓ Would import" if dry_run else "✓ Imported",
         "would import" if dry_run else "imported"),
        ("conflict", "yellow", "⚠ Conflicts (skipped — use --overwrite to force)", "conflict(s)"),
        ("skipped", "dim", "─ Skipped", "skipped"),
        ("error", "red", "✗ Errors", "error(s)"))
    for status, col, label, _ in groups:
        group_items = [i for i in report.get("items", []) if i.get("status") == status]
        if not group_items:
            continue
        print(_color(f"  {label}:", col))
        for item in group_items:
            tail = ("→ " + str(item.get("destination") or "").replace(str(Path.home()), "~")
                    if status == "imported" else f" {item.get('reason', '')}")
            print(f"      {item.get('kind', 'unknown'):<22s} {tail}")
        print()
    if stripped := report.get("stripped_secrets"):
        print(_color("  ⚷ Secrets stripped (never imported):", "yellow"))
        for name in stripped:
            print(f"      {name}")
        print_info("Re-add credentials deliberately via 'hermes setup' or the profile's .env.")
        print()
    summary = report.get("summary", {})
    parts = [f"{summary[k]} {label}" for k, _, _, label in groups if summary.get(k)]
    if parts:
        print_info(f"Summary: {', '.join(parts)}")
