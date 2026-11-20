# Re-bench 2026-11-20: session mode (session.14) vs current upstream per-call

Paired bench of the fork's session mode against upstream per-call on this plugin. Bench outcome: (A), per-call matched session on these measures. **Owner decision (2026-11-20): keep session mode; revisit only if upstream syncs get painful or session-only bugs recur.**

## Setup
- Arms: per-call = upstream main 269ddcb; session = session.14 (888d13b, session kept alive). Same canary harness, sandbox, CLI 2.1.329, Opus 5.5, 3 s pause between user turns.
- Work: the bench's two 10-user-turn tasks, alternating: lt1 (coding, tool loops, ~45-50 steps) and lt2 (reasoning-heavy, ~68 steps). 4 pairs, same seed per pair, arm order alternating.
- Spend: $8.72 for the 4 pairs, plus a $2.95 pilot pair before the thinking-count instrumentation was added (reported separately, not in the statistics).
- Instrumentation: the canary's metadata-only request summary counts, per upstream request, the earlier assistant messages it resends, how many carry a thinking block, and the total thinking blocks. Quality is the bench's graded checks.

## Results (ratio = per-call / session, geometric mean over 4 pairs, 90% CI)
| metric | ratio | t 90% CI | bootstrap 90% CI |
|---|---|---|---|
| wall time per step | 0.89 | 0.80–0.99 | 0.84–0.95 |
| wall time per conversation | 0.93 | 0.86–1.01 | 0.89–0.98 |
| list cost | 0.99 | 0.92–1.07 | 0.95–1.04 |
| thinking tokens | 1.21 | 0.89–1.65 | 1.00–1.48 |
| output tokens | 0.98 | 0.91–1.05 | 0.93–1.02 |
| cache writes | 0.97 | 0.91–1.04 | 0.94–1.01 |
| steps | 1.05 | 0.97–1.14 | 1.00–1.11 |

- Cache misses at turn boundaries and tool continuations: **0 in both modes** (every pair).
- Quality: identical in every pair. lt2 20/20 checks in both. lt1 5/13 in both, the same checks failing in both modes (a task/harness ceiling, not a mode difference).
- Thinking reach: **neither mode ever drops a thinking block.** Across 8 runs and 461 requests, the count of earlier thinking blocks resent upstream never decreased from one request to the next. The resent share (~30-50% of earlier assistant messages carry thinking) reflects answers where the model chose not to think, not stripping. Current upstream per-call therefore does not have the "earlier thinking left out" problem from the OpenClaw report; there is no upstream bug to file on this.
- History size (offline, no account): per-call starts a fresh Claude Code and replays the whole history on every step. With production-sized history (1,800 rows, ~4.8 MB) one per-call step spends ~1.5 s against ~0.3 s for an empty history: ~1.2 s extra per step. The bench conversations were ~70 steps, so they did not show this. At production size that extra time is about the size of per-call's bench advantage on lt1 (1.2-1.5 s per step; under 0.3 s on lt2), so expect rough parity on wall time, not a per-call win.

## What changed since October
In October, per-call missed the cache at every user turn after an answer with thinking (6/6) and re-thought (#40, #36/#122). Upstream has since adopted the relay fixes (stable request context, cache-mark pin), and this run shows 0 misses and full thinking continuity on per-call. The October case for session mode no longer holds on these measures.

## Bench recommendation (not taken: the owner kept session mode)
1. Flip the production profile to per-call with its current fork build (setting `session_mode: false`), through a normal PM window. This is reversible by flipping it back; no code is removed yet.
2. Watch a week of its live steps: wall per step, cold rewrites, cache read share, first-words latency, any quality complaints from the owner. Compare with this week's session-mode numbers from the same log.
3. If live matches, drop the fork's session code (~1,650 plugin lines, ~5,200 test/eval lines) and its bug class. Keep the fork only for what upstream lacks (or upstream those: early opener needs core seam #150597; peer-message refusal is upstreamable), or move the production profile to upstream's plugin directly.
4. Continuity protection: LCM-X is host-side and unaffected. The prompt cache is matched on bytes server-side, so the 1 h cache and HeartBeat's keep-warm work the same with per-call (no live process needed). Per-call also removes the session-only rebuild kills.

## Limits (what this does not prove)
- n = 4 pairs; CIs are wide, and the thinking-token difference is inconclusive (per-call ~21% more, CI includes no difference).
- Bench histories are small; the large-history cost is from an offline replay timing, not a live run.
- The bench Hermes is a pinned core, not the production profile's; her LCM-X compaction and system-prompt drift affect both modes equally but were not exercised.
- Not measured: first-words latency on Telegram, and the early opener's behaviour per mode.

## Data
`2026-11-20-session-vs-percall.jsonl` beside this file: one metadata-only line per arm-run (numbers only: timings, token counts, check counts, per-request thinking-block counts). No prompts, outputs or request content.
