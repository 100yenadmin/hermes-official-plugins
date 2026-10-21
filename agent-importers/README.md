<p align="center"><img src="../assets/agent-importers.jpg" alt="Agent Importers: official Hermes Agent plugin" width="100%"></p>

# Agent Importers for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly built into Hermes Agent core (`hermes_cli/agent_import.py`).

`hermes import-agent` brings an existing **Claude Code** (`~/.claude`), **OpenAI Codex CLI** (`~/.codex`) or **Cursor** (`~/.cursor`) setup into Hermes with one command. Every run shows a per-item preview first, `--dry-run` never touches disk, and API keys and credentials are never imported.

## Install

```bash
hermes plugins install agent-importers
# or straight from this repo:
hermes plugins install NousResearch/hermes-official-plugins#agent-importers
```

Upgrading from a Hermes version that had `import-agent` built in: the command, its flags and your
sync manifest (`import-sync.json` in the profile) are unchanged. Profiles that registered a sync
source get the plugin automatically on update; everyone else sees the install line when they run
`hermes import-agent`.

## Usage

```bash
hermes import-agent                    # auto-detect ~/.claude, ~/.codex or ~/.cursor
hermes import-agent claude-code        # import from ~/.claude
hermes import-agent codex              # import from ~/.codex
hermes import-agent cursor             # import from ~/.cursor
hermes import-agent claude-code --dry-run          # preview only
hermes import-agent codex --source /path/to/.codex # custom location
hermes import-agent claude-code --overwrite --yes  # replace conflicts, skip prompts
hermes import-agent --sync             # re-import every registered source that changed
hermes import-agent --sync --dry-run   # preview what a sync would do
```

It imports into the active profile (`hermes -p <name> import-agent ...` for another one).

## What maps where

### Claude Code (`~/.claude`)

| Claude Code | Hermes |
|---|---|
| `CLAUDE.md` | Memory entries in `memories/MEMORY.md` |
| `settings.json` `permissions.allow` (`Bash(...)` rules) | `command_allowlist` in `config.yaml` |
| `settings.json` `permissions.deny` (`Bash(...)` rules) | `approvals.deny` in `config.yaml` |
| `~/.claude.json` / `settings.json` `mcpServers` | `mcp_servers` in `config.yaml` |
| `skills/<name>/` (dirs with `SKILL.md`) | `skills/claude-code-imports/<name>/` |
| `commands/*.md` | Reported as skipped (no direct equivalent) |

Claude's `Bash(npm run test:*)` prefix rules become `npm run test*` globs. Non-`Bash` rules and bare `Bash` are reported as not mapped.

### Codex CLI (`~/.codex`)

| Codex | Hermes |
|---|---|
| `AGENTS.md` | Memory entries in `memories/MEMORY.md` |
| `config.toml` `[mcp_servers.*]` | `mcp_servers` in `config.yaml` |
| `memories/*.md` | Memory entries in `memories/MEMORY.md` |
| `skills/<name>/` (dirs with `SKILL.md`) | `skills/codex-imports/<name>/` |

### Cursor (`~/.cursor`)

| Cursor | Hermes |
|---|---|
| `AGENTS.md` | Memory entries in `memories/MEMORY.md` |
| `rules/**/*.md`, `rules/**/*.mdc` (User Rules) | Memory entries in `memories/MEMORY.md` |
| `mcp.json` `mcpServers` | `mcp_servers` in `config.yaml` |
| `skills/**/<name>/` (dirs with `SKILL.md`, category folders allowed) | `skills/cursor-imports/<name>/` |

`.mdc` frontmatter (`description`, `globs`, `alwaysApply`) is Cursor routing metadata and is stripped; only the rule body is imported. A skill is the folder holding `SKILL.md`, so `skills/shipping/land-it/` imports as `cursor-imports/land-it/`; two skills with the same name in different folders are reported as a conflict.

## What is never imported

Credential files (`~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.cursor/cli-config.json`) are never read, and MCP server environment variables or headers with secret-looking names (`*_TOKEN`, `*_API_KEY`, `Authorization`, ...) are stripped and listed in the report so you can re-add them deliberately. Run `hermes setup` for providers, or put secrets in the profile's `.env`.

## Behaviour

- Existing MCP servers and skills with the same name are conflicts and skipped unless `--overwrite`.
- Memory entries are merged and deduplicated into the existing store; the previous `MEMORY.md` is backed up as `MEMORY.md.bak.<timestamp>` first.
- A `config.yaml` that cannot be read or parsed is never rewritten: the import records an error and leaves the file byte-identical. Writes keep your comments.
- Non-interactive runs (no TTY) stop after the preview unless you pass `--yes`.

## Sync

Every successful import registers its source in the profile's `import-sync.json`. `hermes import-agent --sync` re-imports any registered source whose files changed, without prompts, so it fits a cron job:

- The change check is a digest over exactly the files the importer reads; credential files never trigger a sync.
- Skills previously imported are refreshed in place when the source copy changes, unless you edited the imported copy (then it is a conflict and left alone).
- A run with errors keeps the old digest, so the next sync retries.

`hermes profile create <name> --clone --sync-imports` carries the manifest into a new profile, so `hermes -p <name> import-agent --sync` keeps pulling from the same trees.

## Compatibility

On a Hermes version that still has `import-agent` built in, this plugin stays inert and the built-in command runs.

## License

MIT
