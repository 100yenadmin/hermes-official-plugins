---
name: meshy-3d
description: "Create 3D models with Meshy AI from text or images, then texture, remesh, rig, animate, convert, resize, or prepare them for 3D printing (printability repair, multicolor 3MF, split into parts, slicer handoff). Use when the user asks for a 3D model, game asset, rigged character, animation, AR/USDZ file or 3D print."
version: 1.0.0
author: Nous Research
license: MIT
metadata:
  tags: "meshy, 3d, text-to-3d, image-to-3d, texturing, rigging, animation, 3d-printing, game-assets, mcp"
---

<!-- Workflow rules adapted from meshy-mcp-server 0.6.4 (https://github.com/meshy-dev/meshy-mcp-server, MIT, Copyright (c) 2026 Meshy AI). -->

# Meshy 3D

Generates and edits 3D assets on Meshy's cloud through the `meshy` MCP server bundled in this
plugin: text or image to 3D, texturing, remeshing, rigging, animation, format conversion,
real-world resizing, UV unwrapping, 2D concept images, printability analysis and repair,
multicolor 3MF, splitting into printable parts and Creative Lab products. Most calls spend
the user's Meshy credits.

## When to Use

- The user wants a 3D model, prop, character, game asset, AR file (USDZ) or 3D print.
- The user has a Meshy task or model URL to texture, rig, animate, convert, resize or repair.
- The user asks about their Meshy balance, spend or earlier generations.
- NOT for editing a local mesh by hand (use Blender or a mesh tool) or for 2D images unrelated
  to a 3D job.

## Prerequisites

- The `meshy` plugin installed and enabled, then a NEW Hermes session so the MCP tools load.
  Node.js 18+ with `npx` on PATH (the first start downloads the pinned package).
- `MESHY_API_KEY` in the Hermes profile's `.env` (`~/.hermes/.env`, or the profile's own).
  Keys start with `msy_` and come from meshy.ai → Settings → API (Pro plan or above). Hermes
  hands MCP servers only a filtered environment, so a key exported in a shell alone is not
  seen. Never ask for the key in chat; tell the user where to put it.

## How to Run

The tools sit behind Hermes' tool search, not in the visible tool list. Find them with
`tool_search` (for example `meshy text to 3d`, `meshy download model`, `meshy rig`), load
the schema with `tool_describe`, then invoke the exact returned name with `tool_call`.
Never guess a prefixed name. Never run `npx add-mcp`, `hermes mcp add meshy` or any other
setup step from Meshy's docs: the plugin already registers the server, and a second
`mcp_servers.meshy` entry in `config.yaml` replaces the plugin's pinned one.

The server is marked untrusted, so Hermes asks the user before every call that is not
read-only (every create, download, cancel). Read-only calls (balance, status, lists, slicer
detection) run without a prompt. When a call comes back "The user did not approve", stop:
the user declined, or the run is unattended (cron, `chat -q`, a gateway with no approver)
and cannot approve. Do not retry, inspect config or look for a workaround; tell the user the
exact call you would make and its cost, and that an interactive session can approve it.

## Quick Reference

Full list with credits: [tools](references/tools.md). Scenario playbooks (printing, game
engine, character animation, AR, retexture, conversion, UV unwrap, Creative Lab, cheap
topology, print in parts): [workflows](references/workflows.md). Read the workflow for the
user's goal before the first paid call.

| Goal | Chain |
|---|---|
| Model from text | `meshy_text_to_3d` (preview, untextured) → `meshy_get_task_status` → `meshy_text_to_3d_refine` (textures) → `meshy_download_model` |
| Model from a photo | `meshy_image_to_3d` with `file_path` (textured by default) → status → download |
| Concept first | `meshy_text_to_image` → user approves → `meshy_image_to_3d` with `input_task_id` |
| Rigged + animated character | generate with `pose_mode: "t-pose"`, textured → `meshy_rig` → `meshy_list_animations` → `meshy_animate` |
| Print | `meshy_send_to_slicer` (detect, free) → generate with `target_formats: ["obj"]` → `meshy_analyze_printability` (free) → download with `print_ready: true` |
| Balance / spend | `meshy_check_balance`, `meshy_list_usage` (free) |

## Procedure

1. **Pick the scenario** from [workflows](references/workflows.md) and settle the output
   format before generating: `target_formats` is fixed at creation (`fbx` for engines,
   `usdz` for AR, `obj`/`3mf` for printing, `glb` by default).
2. **Confirm cost before every credit-spending call.** Name the tool, the `ai_model` where
   there is a choice, the credit cost from [tools](references/tools.md) and any surcharge (8K
   textures, Ultra geometry), and wait for the user's yes. Hermes' approval prompt is the
   second gate, not the first. Offer `meshy_check_balance` when the job is large.
3. **Submit once.** Every create returns `{task_id, status}` (Creative Lab waits and returns
   the model). Keep the `task_id` and task type.
4. **Wait** with `meshy_get_task_status` (`wait: true`, default 300 s per call). Generation
   usually takes 2–3 minutes. A wait that times out leaves the task running: poll the same
   `task_id` again.
5. **Never re-create on doubt.** If a create fails with a timeout, network error or 5xx, the
   task may exist and be billed: check `meshy_list_tasks` with that `task_type` first.
6. **Download** with `meshy_download_model`. Pass `save_to` as an ABSOLUTE path in the user's
   project or wherever they asked. Without it, files land under the plugin's data directory
   (`meshy_output/`), not the current project. Report every saved path and the file size.
7. **Report** the result: files and paths, formats, credits spent, and the `task_id` for
   follow-ups (refine, rig, convert reuse it instead of regenerating).

## Pitfalls

- A text-to-3d preview has no textures: refine it (10 credits) or use the image route, which
  is textured in one task.
- Rigging needs a textured humanoid at ≤300K faces; remesh first if it is denser.
- Animation `action_id`s come from `meshy_list_animations`; never invent one.
- UV unwrap needs a GLB with ≤40,000 faces.
- `meshy_cancel_task` on a finished task DELETES it (and needs `confirm_delete: true`);
  confirm with the user before calling it.
- `meshy_list_tasks` without `task_types` covers only the five generation types; an empty
  list does not mean there are no rigging, printing or Creative Lab tasks.
- Local inputs (`file_path`, `file_paths`) must be absolute paths the server can read; never
  base64-encode an image yourself.
- `meshy_send_to_slicer` returns a `launch_command` to run with the terminal tool. Run it only
  after the user picks the slicer.
- Errors mentioning `401` or an invalid key mean `MESHY_API_KEY` is missing or wrong in the
  profile `.env`; the user must fix it and start a new session.

## Verification

- `meshy_check_balance` answers with a credit balance: server connected and key valid.
- After a download, the reported path exists and its size is non-zero.
