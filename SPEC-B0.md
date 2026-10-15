# SPEC B0: offline cache-continuity rehearsal (real native, synthetic peer, $0)

## Lane Contract
- Observable outcome: one new script `claude-subscription-directsdk/evals/directsdk_cache_continuity.py`. It drives the
  real DirectSDK plus the real `claude` binary against a synthetic loopback Messages peer through multi-turn conversations
  in 8 scenarios. For every upstream request it reports whether it byte-extends the cached prefix it should reuse.
- Done when, all three run and their outputs are saved under `.rehearsal-out/` in this worktree:
  - (a) Run against this tree (`--root claude-subscription-directsdk`): exit 0, all scenarios PASS.
  - (b) Run against `--root /Users/m1/repos/hermes-official-plugins-ctl-main/claude-subscription-directsdk` (main, no
    #83): S1 FAILS at user-turn boundaries. This is the positive control. Warm scenarios are skipped there, because main
    has no warm mode.
  - (c) Run against `--root /Users/m1/repos/hermes-official-plugins-ctl-b6d/claude-subscription-directsdk` (#79 before
    the #84 fix): at least one restart scenario (S3/S4/S5) FAILS at a user-turn boundary.
  - If (b) or (c) unexpectedly PASS, report that with the receipts. Never weaken the check to force a result.
- Resume identity: worktree /Users/m1/repos/hermes-official-plugins-rehearsal · branch rehearsal-b0 · base 7533993
  (main ad20043 + PR #79 f4e7333 + PR #83 470166a).
- Proof-claim boundary: wire-prefix continuity against a synthetic peer and one CLI version (2.1.299). It does NOT prove
  vendor cache hits, costs or real-model behaviour.
- Stop conditions: ~600 changed lines is a stop-and-report signal. Do not edit any existing file (plugin code, tests,
  existing evals). If the plugin seems to need a change to pass, STOP and report the receipts. Do not fix plugin code.
- Evidence path: `.rehearsal-out/` (JSON receipts plus a short summary.md). No git mutation; the orchestrator commits.
- Secret boundary: offline only. Use a fake API key; never read ~/.claude, ~/.hermes or any real credential.

## Base to copy from
`claude-subscription-directsdk/evals/directsdk_cache_wire.py` (read it fully first). Reuse its pieces:
- the `Peer` SSE server (extend it);
- the env isolation: HOME/CLAUDE_CONFIG_DIR in a temp dir, fake ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL at the peer, proxies
  at 127.0.0.1:1, NONESSENTIAL_TRAFFIC off;
- `content()` (strips cache_control), `cut(prompt, at)`, `digest()`, and its message-marker discovery.
- Load `directsdk.py` from `--root` the same way that file does, by inserting the root on sys.path. Run each root in its
  own process; it is fine to support one `--root` per invocation.

Binary: `/Users/m1/.local/share/claude/versions/2.1.299` (default for `--binary`). Default model `claude-opus-5-5`.

## How warm mode is driven
- Warm turns on when the env dict passed to `native.Client(env=...)` has `CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM=1` (see
  `_warm_enabled`). Per-call needs `=0`.
- Pool internals are module globals in `directsdk.py`: `WARM_IDLE_SECONDS`, `WARM_POOL_SIZE`, `_warm_pool`,
  `_warm_shutdown()`, `_warm_checkout()`.
- Each warm step logs `warm step: session=... mode=<reused|cold|rebuild:...> ...` and kills log `warm kill: ... reason=...`
  on logger `_warm_log` (name = the module's `__name__`). Attach a handler to capture mode and kill reason per request.
- Calls without tools stay per-call, so every request passes tools.

## Conversation shape (all scenarios)
- System message: a long synthetic system prompt, as in the base.
- 2 tools: `probe` and `probe2`. Six user turns.
- Each turn has 3 tool rounds, then a final text answer. Each round's assistant message has a THINKING block plus
  tool_use; in round 1 of every turn the peer returns TWO tool_use blocks (parallel tools).
- Tool results are ~2 KB of synthetic text each.
- The peer decides tool_use vs end_turn from a per-request script, as `peer.tools` does now.

## Scenarios
- **S1 per-call** (WARM=0).
- **S2 warm alive** (WARM=1).
- **S3 warm restart by idle:**
  - Set `native.WARM_IDLE_SECONDS = 1.0` after import.
  - Sleep 1.5 s before each new user turn (not between tool rounds), so `_warm_checkout` finds the session stale.
  - Expect mode `cold` or `rebuild:*` at each turn's first request.
- **S4 warm restart by LRU:**
  - `WARM_POOL_SIZE = 1`.
  - Before each new user turn, run one request of an unrelated second conversation (different first user text), which
    evicts the main session.
  - Exclude the unrelated requests from the main conversation's prefix chain, but keep their receipts.
- **S5 gateway restart:** before each new user turn, call `native._warm_shutdown()`, close the client, and build a new
  `native.Client`.
- **S6 review fork on another model:**
  - Between turns 3 and 4, send one request with the same history but model `claude-sonnet-5`, plus an extra trailing
    user message "Review the above.". The peer answers with text.
  - Then continue the main conversation on the main model.
  - Expect turn 4's first request to have mode `reused` and to byte-extend turn 3's prefix.
- **S7 model swap and back:**
  - Turns 1–2 on the main model, turns 3–4 on `claude-sonnet-5`, turns 5–6 on the main model.
  - Check the boundary into turn 3 against nothing; it is the declared exemption, because a model switch writes a new
    cache in Claude Code too.
  - Check turn 4 against turn 3, as usual.
  - Turn 5's first request must byte-extend the last prefix the MAIN model wrote, i.e. turn 2's last request's last marker.
- **S8 restart right after the first turn:**
  - WARM=1. Restart (as in S5) only before turn 2, then keep alive.
  - Report the turn-2 boundary separately, plus any `role: system` entries in `messages`, with length and sha256[:12].
  - This targets the 977-char system message noted in issue #84. If it still breaks the prefix, S8 FAILS. Record it
    clearly as a finding; do not exempt it.

## The check (per request, main conversation only)
- Keep the last request's last message-marker prefix (`cut(prompt, last_marker)`) per model.
- Each new request on the same model must satisfy `cut(new_prompt, old_marker) == old_prefix`. This is exactly the
  base's check, and it applies within turns and at boundaries.
- On mismatch, record the first divergent position (message index, block index, block type, text length, sha256[:12] of
  the old and new block) by walking both prompts in order, then continue. Don't abort; we want all receipts.
- Also record:
  - message-marker count (≤4 total markers, as in the base);
  - whether every THINKING block from history is still present unchanged (signed-thinking replay);
  - that the caller's history was not mutated.
- A scenario PASSES when every non-exempt request matches, there are no extra upstream requests per create(), and the
  thinking replay is intact.

## Output
- JSON to stdout: `{native_version, root, scenarios: {S1: {pass, boundaries: [{turn, match, mode, divergence?}],
  requests: N, mismatches: N, system_messages: [...], kills: [...]}, ...}}`.
- `--only S3,S5` to select scenarios. Exit 1 when any selected scenario fails.
- Docstring like the base's: what it proves and what it does not.
- Runtime target under ~15 min for all 8 scenarios. Per-call spawns are slow, so don't add more turns than specified.

## Running it in this sandbox
- Set `TMPDIR=$PWD/.tmp` (create it), because the temp dirs must live inside this worktree.
- Python: `/Users/m1/.hermes/hermes-agent/venv/bin/python` with `HERMES_DISABLE_LAZY_INSTALLS=1`.
- The peer binds 127.0.0.1. If binding or spawning the binary is blocked, STOP and report the exact error; don't work
  around it.

## Return (under 60 lines)
- Files added and their line counts.
- The three run results: per-scenario PASS/FAIL, with the divergence receipts for failures.
- Anything surprising about warm modes or kill reasons.
- Open questions.
