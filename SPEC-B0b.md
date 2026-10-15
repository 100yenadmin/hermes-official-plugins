# SPEC B0b: rehearsal fidelity — emulate a subscription login offline so native emits its account-email reminder

## Lane Contract
- Observable outcome: `evals/directsdk_cache_continuity.py --subscription` makes the real `claude` 2.1.299 emit its
  per-request account-email context reminder against the synthetic loopback peer. That reproduces the per-call cache
  break seen live on unfixed main.
- Done when (outputs in `.rehearsal-out/b0b/`):
  1. `--subscription` against `--root /Users/m1/repos/hermes-official-plugins-ctl-main/claude-subscription-directsdk`:
     S1 FAILS at user-turn boundaries, and the wires contain the reminder (count requests whose text contains
     `The user's email address is`).
  2. `--subscription` against `--root /Users/m1/repos/hermes-official-plugins-rehearsal/claude-subscription-directsdk`
     (main + #83 + warm): S1 PASSES, and S2 PASSES. Report S3–S8 as they come out; a date-line break in warm restarts
     is already known and being fixed elsewhere.
  3. Without `--subscription`, behaviour is unchanged.
- Resume identity: /Users/m1/repos/hermes-official-plugins-rehearsal · branch rehearsal-b0 · HEAD 8e16509.
- Proof-claim boundary: an emulation of native's subscription-mode prompt shape. Not real auth, cache hits or cost.
- Stop conditions:
  - Edit only `claude-subscription-directsdk/evals/directsdk_cache_continuity.py`.
  - Never use or read a real credential, keychain item, ~/.claude or ~/.hermes.
  - Every fake value must be an obvious public fixture (`public@example.invalid`, all-zero UUIDs).
  - If the reminder can't be produced without real credentials or network, STOP and report what native requires.
- Evidence path: `.rehearsal-out/b0b/`. No git mutation.
- Secret boundary: fixtures only.

## What is known
- The reminder is built in native's user-context code (found in the 2.1.299 binary; search near
  `user_context_started`):
  `let M = a.ANTHROPIC_UNIX_SOCKET ? void 0 : Tn()?.emailAddress ?? (he() && hl() ? a.CLAUDE_CODE_USER_EMAIL : void 0)`.
  Then `userEmail: "The user's email address is ${M}. Use it only to identify ..."`.
- **Tried already, with no reminder in either case:**
  - (a) `CLAUDE_CODE_OAUTH_TOKEN=<fake>`, no ANTHROPIC_API_KEY, and `$CLAUDE_CONFIG_DIR/.claude.json` with an
    `oauthAccount` {emailAddress, accountUuid, organizationUuid, ...} plus `hasCompletedOnboarding`;
  - (b) the same, plus `CLAUDE_CODE_USER_EMAIL` and a fake `$CLAUDE_CONFIG_DIR/.credentials.json` with `claudeAiOauth`
    {accessToken, refreshToken, expiresAt, scopes, subscriptionType:"max"}.
- So find what `Tn()`, `he()` and `hl()` are. Extract the minified definitions from the binary: it embeds the JS source,
  so search for `function Tn(` and nearby. Then satisfy them with fixtures only.
  - Possibilities: a different config path; a keychain-only credential read on macOS (then look for a plaintext fallback
    env such as a "use plaintext credentials" switch); an account-info cache file; or an env that marks the session as
    first-party OAuth.
- The peer must answer any extra endpoint native calls in this mode (e.g. profile or bootstrap). Serve minimal public
  fixtures on the peer, and log which paths native hits.
- Probe used so far, for reference: `/Users/m1/Codex/session-notes/2026-10-15/eva-parity-audit/artifacts/probe_oauth.py`.
  Run it with `PYTHONPATH=claude-subscription-directsdk/evals`.

## Running in this sandbox
- `TMPDIR=$PWD/.tmp`
- `/Users/m1/.hermes/hermes-agent/venv/bin/python` with `HERMES_DISABLE_LAZY_INSTALLS=1`

## Return (under 40 lines)
- What native required, with evidence.
- The flag's env/config/peer changes.
- Run results 1–3, the reminder counts, and the first-divergence receipts.
