# hermes-plugin-touchdesigner

Drive a live [TouchDesigner](https://derivative.ca) session from Hermes Agent
through 404.zero's free **twozero** plugin, which embeds an MCP server inside
the running TouchDesigner process (Streamable HTTP on `127.0.0.1:40404`).

This is a portable [Agent Plugins v1](https://agent-plugins.org) package. It
ships two things in one install:

- `mcp.json` — the twozero MCP server entry (`td`), registered for every
  session while the plugin is enabled. No `mcp_servers` edits in config.yaml.
- `skills/touchdesigner-mcp/` — the workflow skill: critical rules, discovery
  steps, 20+ reference files (operators, GLSL, audio-reactive, instancing,
  pitfalls), and a `scripts/setup.sh` health check.

## Install

```bash
hermes plugins install touchdesigner     # catalog name
hermes plugins enable td                 # manifest name (kept short: see below)
```

Then, once per machine, inside TouchDesigner (2025.32280+, Windows/macOS):

1. Download <https://www.404zero.com/pisang/twozero.tox>
2. Drag `twozero.tox` into the TD network editor and click **Install**.
3. twozero icon → Settings → mcp → "auto start MCP" → **Yes**.

Start TD first, then a new Hermes session. The skill is available as
`agent-plugin-td-<hash>:touchdesigner-mcp` via `skill_view` (`skills_list` shows the exact name); health check:

```bash
curl -s http://127.0.0.1:40404/mcp
```

## Why the manifest name is `td`

Hermes registers portable MCP tools as
`mcp__agent_plugin_<name>_<hash>__<server>__<tool>`. twozero's longest tool
name (`td_screen_point_to_global`) plus a `touchdesigner` namespace lands at
71 characters, past the 64-character function-name cap on OpenAI-compatible
and Bedrock wire formats. `td` keeps every tool name at or under 60.

## Security notes

- `td_execute_python` runs arbitrary Python inside TouchDesigner with no
  sandbox — the same trust level as the terminal tool. The hub is
  localhost-only and unauthenticated.
- All 36 twozero tools register. The desktop input-automation tools
  (`td_input_*`, `td_click_screen_point`, `td_get_screen_screenshot`, ...) and
  admin tools (`td_project_quit`, `td_test_session` — which exports transcripts
  to the vendor hub) are documented in the skill as opt-in: the agent is told
  to use them only on explicit request.
- Nothing in this package phones home. The only network access is the local
  MCP endpoint and the optional `twozero.tox` download in `setup.sh`.

## Migrating from the bundled skill

Earlier Hermes versions shipped `touchdesigner-mcp` as an optional skill whose
`setup.sh` wrote a `twozero_td` entry into `mcp_servers`. Remove that entry (or
a `touchdesigner` one written by `hermes mcp install`) so the server is not
loaded twice.

## Credits

Skill authored by [@kshitijk4poor](https://github.com/kshitijk4poor); twozero
by [404.zero](https://www.404zero.com). MIT.
