# Changelog

## [0.4.0+session.10] - Unreleased

Merged with main at e726eb7 (plugin 0.4.0): everything main shipped since 8b5b629 reaches the session-mode build.
Session mode carries main's per-request checks on every step.

- A launch profile's own turns keep its own Claude login under multiplexing. A Desktop backend that serves several profiles turns multiplexing on, and main's served-profile guard then counted the launch profile's own turns as served. A launch profile whose login is the shared `~/.claude` (no `claude_config_dir`, no token in its `.env`) was refused with "This profile has no Claude login of its own" on every Desktop turn. Core marks the launch profile's scope unstamped, or stamped with the process home, so those turns now keep the launch login. A served profile without a login of its own is still refused.
- A changed `upstream_ca_file` (another path, new content, or added or removed while Hermes exports the same bundle) starts a new session instead of reusing one that trusts the old bundle.
- The first-run login settle drops `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` unless you exported it, as main's `/usage` probe does.

## [0.4.0] - Unreleased

### Added

- `max_concurrent` setting (default 4) caps the Claude Code turn processes one Hermes process runs at once, across subagents, auxiliary calls and the profiles of a multiplexed gateway (#329). A subagent fan-out used to start one process per call at the same moment (about 250 MB each, all on one subscription). A call past the cap waits for a free place within its own timeout instead of failing or falling back; an interrupt ends the wait at once without starting Claude Code; a wait over 2 s is logged. `/usage` and the model picker do not count.
- Organization gateway opt-in (#46): per-profile `upstream_url`, `upstream_proxy` (HTTP CONNECT, credentials in `CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY_AUTH`) and `upstream_ca_file` settings route every relay (turns, `/usage`, the picker handshake) through the organization's gateway. Claude Code sends only Messages requests to that relay; its own account calls (`/usage`, bootstrap, MCP directory, telemetry when on) go to `api.anthropic.com` and now take the configured proxy and CA too (`HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS` on the child). Any one set is the opt-in; a set but invalid value (including a non-string one such as `upstream_proxy: false`) refuses the request instead of going to Anthropic directly. One warning per profile names the host the Claude login's bearer goes to. An inherited `ANTHROPIC_BASE_URL` stays refused. Supersedes P#257 (thanks @salch-cred), P#130 (thanks @alextidalrise) and P#23 (thanks @MematiBas42), which selected the route from environment variables.
- `extra_usage` setting (`allow`, `notify`, `ask`, `stop`; default `notify`) decides what a request does once Claude Code reports the account is drawing paid extra usage (#43). It reads native's `rate_limit_event` (`isUsingOverage`, `resetsAt`), which the plugin used to drop (P#281, thanks @salch-cred), and keeps the state in memory per process and login; no files are written. `ask` and `stop` refuse before Claude Code starts, with the reset time and the `hermes config set` line that allows it (`extra_usage_allowed_until` in `ask`). A refusal is a plan-limit stop, so Hermes does not switch to a fallback provider. In `ask` and `stop` a check that cannot tell (no usage answer) refuses with its reason instead of sending a possibly paid turn, and the check asks about the turn's own login and route. Supersedes P#282 (thanks @creditlabsonarc-rgb), whose marker file and guessed expiry are replaced by native's own reset time.

### Changed

- `claude_code_telemetry` is off by default (#331): every Claude Code child starts with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY` and `DISABLE_ERROR_REPORTING` set unless the setting is turned on, which also takes about 0.25 s off each spawn. The model picker stays complete: its zero-inference `initialize` handshake leaves `DISABLE_TELEMETRY` and the nonessential-traffic flag unset (either one shrinks the CLI picker from 13 rows to 5 on 2.1.319), so it lists the same 12 routes as with telemetry on. A flag you export yourself is kept.

### Fixed

- An empty `.oauth_refresh.lock` that a Claude Code left after exiting mid-refresh (anthropics/claude-code#95236) is removed from the child's own login directory before every spawn, once it is past Claude Code's 60 s stale window; before, every request failed with "another Claude Code process is refreshing it" until the user deleted it. A lock with contents or a fresh mtime is never touched (P#25, thanks @LevSky22 and @100yenadmin).
- The picker and `/usage` probes own their Claude Code child: it is killed and waited for before its scratch directory is removed, on every path, and at interpreter exit. `hermes status` on Windows printed a `PermissionError [WinError 32]` traceback, left a `claude-directsdk-picker-*` directory and a running `claude.exe` behind, because the catalog refresh thread was still inside the handshake when Hermes exited (C#146288). A launcher that exits before the CLI it started (a shell or npm shim) no longer leaves that CLI running: probes and turns share one process-group helper that signals the group while the leader is still unreaped.
- Setup no longer reports a Console API key, an API-key helper or a cloud backend login (Bedrock, Vertex, Foundry) as a working subscription login: `claude auth status` says `loggedIn: true` for those too, so setup passed and the first turn failed. It now says which login Claude Code would use and how to switch (P#28, thanks @100yenadmin).
- Claude Code is no longer looked up under a relative `HOME`, which resolved against the current directory and could run a `claude` planted there (P#27, thanks @grantjayy).
- The "Third-party apps now draw from your extra usage" 400 is classified as a billing stop with a message that names the plan settings and the `hermes-agent` caller, instead of an unknown error that core answered silently from a fallback provider (P#296).
- A multiplexed gateway (`gateway.multiplex_profiles: true`) runs each profile's Claude Code on that profile's own login. `CLAUDE_CODE_OAUTH_TOKEN` in a served profile's `.env` now reaches its Claude Code children (read from core's per-turn secret scope; multiplexing never loads it into the process environment). A served profile without a token of its own no longer inherits the gateway process's token, `ANTHROPIC_CUSTOM_HEADERS` or `CLAUDE_CONFIG_DIR`, with or without `claude_config_dir` (Claude Code prefers the token over the selected login directory), so no profile runs on another profile's account. A served profile with neither its own token nor its own config directory is refused with a login error naming both ways to give it one (`claude_config_dir`, or `CLAUDE_CODE_OAUTH_TOKEN` in its `.env`), since Claude Code would otherwise read `~/.claude` under the inherited home. With no secret scope bound, the request and the setup probes fail closed instead of guessing. Single-profile installs and an explicit `Client(env=...)` are unchanged, and the process environment is never modified (#34, #313, thanks @kalfian).
- `/usage` shows the extra-usage state, usage credits spent, this week's share per surface and the per-model limits beside the plan limits. They were dropped whenever Claude Code answered with `limits`, which current versions always do.
- `/usage` shows the plan limits with `claude_code_telemetry: false`: the CLI skips its `get_usage` fetch under `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, so the zero-inference usage probe no longer sets that one flag (telemetry and error reporting stay off; a flag the user exported is kept).

## [0.3.5+session.9] - 2026-11-17

### Fixed

- The plugin keeps working after Hermes' frozen import copy is deleted under a running process
  (hermes-agent#156493). Since Hermes 0.23 a provider plugin is imported from a copy in the temp
  directory, which Hermes' scratch pruner or macOS's daily temp cleanup (files older than three
  days) can remove. The client module is now imported with the package instead of on first use,
  and the inert MCP helper script is read at import and written next to each request's tools.json
  instead of being run from the copy.

## [0.3.5+session.8] - 2026-11-14

Merged with main at 8b5b629 (plugin 0.3.5): everything main shipped since the fork's base b5932f1
reaches the session-mode build, including the Fable 5.2 and Mythos pins, the account's own model
picker, one OAuth refresh re-issue after a revoked login, the no-tools MCP skip, dropped inherited
API keys, the shared-date environment fix (#243), streaming on capable hosts and its long-tool-call
liveness.

### Fixed

- Session mode keeps long tool calls alive for Hermes' stale detector, as per-call does on main: an empty chunk for every
  native stream event that carries no text (pings stay silent).
- Main's shared-date environment fix (#243) works in session mode's whole-history date scan.

### Removed

- The skills-index reminder and the pruned-skills reminder (#30), which upstream declined; a skills nudge belongs in
  core's prompt for every provider.

## [0.3.3+session.7] - Unreleased

### Fixed

- Other Claude Code sessions on the machine can no longer put text into Hermes' conversations through native. Every `claude -p` child registers as a peer session (other sessions' ListAgents lists it) and, with `--setting-sources ''`, accepted cross-session messages: an idle native ran a turn on one, and one sent while a tool ran joined that turn's next request, so it reached Claude mid-turn. Native's settings file now sets `crossSessionInbound: refuse`, per call and in session mode; senders get a "refused" notice. Natives still appear in those listings.

## [0.3.3+session.6] - Unreleased

- Record Claude Code's subscription rate-limit events and log their window and usage metadata in per-call and session mode.
- Drop Claude Code's prepended account context and memory in both modes, keeping session history stable across live requests and restarts.

## [0.3.3+session.5] - Unreleased

### Fixed

- The same work uses about 1.7x less of the subscription's 5-hour window: native now runs as `hermes-agent` instead of `claude -p`'s default `sdk-cli`, a name subscriptions meter about 2x heavier per token (anthropics/claude-code#97074; live on Max with Opus 5.5: 12% vs 7% of the window for the same $2.19 of requests). A name inherited from a host (`sdk-py`, `claude-desktop`) is overwritten too, in session mode as in per-call, and native's artifact scratchpad line, which a non-SDK name switches on, stays out of every request.
- Claude Code's own memory and instructions no longer reach Claude inside the user's message, where Claude took them for a prompt-injection attempt: native's auto memory (`MEMORY.md`) and CLAUDE.md files are not loaded (`--setting-sources ''` left auto memory and the managed CLAUDE.md on) (#195). The context native still prepends to the first user turn (the account-email reminder) keeps session mode's placement behind Hermes' frame.

## [0.3.3+session.4] - Unreleased

### Changed

- The harness note asks for one short sentence whenever Claude is about to use tools. The "skip it for a single quick lookup" exception is gone: on a slow machine a lookup can take a minute, and with the exception Opus 5.5 skipped the sentence on lookup-shaped requests.

### Removed

- `progress_updates` and its routing. Opus 5.5 writes its progress notes as text blocks, which Hermes already shows as interim messages, so the routing almost never fired, and the "updates" display it requested left the reasoning panel empty. Requests that use adaptive thinking send display "summarized" again, as before 0.3.3+session.3.

## [0.3.3+session.3] - Unreleased

### Added

- `claude_guidance` (on by default): fixed Claude harness notes at the end of the system text of agent steps (requests with tools), part of the session identity. `settle_answers` (off by default) adds one more note.

## [0.3.1] - Unreleased

### Added

- Fable 5.2 (`claude-fable-5-2[1m]`, 1M), released November 9, 2026; the `fable` alias moves to it (pin `claude-fable-5-1` to keep Fable 5.1). It needs Claude Code 2.1.321 or newer. Thinking stays mandatory, and the usage-credits note follows Anthropic's plan rule as for Fable 5 and 5.1. Mythos 5.2 (`claude-mythos-5-2[1m]`, 1M) is pinned as access-gated like Mythos 5.1.
- Mythos 5.1 (`claude-mythos-5-1[1m]`, 1M) for organizations Anthropic has verified. It is listed only when the account's own picker offers it, never from the pinned catalog, so accounts without access do not see it. Reasoning-off requests ask for the lowest effort instead of a disable Mythos rejects.
- Per-profile `claude_config_dir` setting selects the Claude login for every native spawn in a multiplexed gateway (#34).
- Opt-in session mode (`session_mode`, off by default): one Claude Code process per conversation, pool sized from physical RAM (`session_pool`), every failure path ending at per-call; replaces #79.

### Changed

- A tool-less request no longer starts the inert MCP helper or writes its manifest: titles, summaries and other tool-less calls skip ~0.6 s of Python start plus one server handshake per call (#42).

### Fixed

- An Anthropic API key in Hermes' environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_FOUNDRY_API_KEY`), set for Hermes' own `anthropic` provider, no longer blocks this provider: it is dropped from every Claude Code child instead of failing the turn (an explicit `Client(env=...)` keeps it, like the other inherited-environment guards), so both providers work from the same Hermes and a subscription turn never bills API usage. Setup and model discovery no longer pass it to `claude auth status` either.
- Preserve the upstream request ID in incomplete-response errors, including an HTTP 200 stream cut short by EOF or a connection reset. Only a bounded, validated ID is added to the error text and metadata, without enabling debug logging or changing retry admission.
- The picker lists the account's own models: models access-gated per account (Mythos 5.1) and older ones it still runs (Opus 4.7, Fable 5) appear exactly when Claude Code offers them. Native serves that catalog only first-party (behind any `ANTHROPIC_BASE_URL` it logs `[servedCatalog] off (not_first_party)` and answers a generic list), so the `initialize` handshake now talks to Anthropic directly; it sends no user turn, so no Messages request. An inherited auth or backend override keeps the handshake behind the relay.
- The Fable usage-credits note follows Anthropic's plan rule for both Fable 5 and 5.1: shown on Pro, not on Max or Ultra (it was shown on Ultra), and on Team/Enterprise worded `usage credits on standard seats`, since the handshake does not name the seat tier and premium seats include Fable.
- The same work uses about 1.7x less of the subscription's 5-hour window: native now runs as `hermes-agent` instead of `claude -p`'s default `sdk-cli`, a name subscriptions meter about 2x heavier per token (anthropics/claude-code#97074; live on Max with Opus 5.5: 12% vs 7% of the window for the same $2.19 of requests). A name inherited from a host (`sdk-py`, `claude-desktop`) is overwritten too, and native's artifact scratchpad line, which a non-SDK name switches on, stays out of every request.
- Claude Code's own context no longer reaches Claude inside the user's message, where Claude took it for a prompt-injection attempt: native's auto memory (`MEMORY.md`) and CLAUDE.md files are not loaded (`--setting-sources ''` left auto memory and the managed CLAUDE.md on), and the relay drops the context native prepends to the conversation's first user turn (the account-email reminder) instead of moving it behind Hermes' frame, matching how every later request replays that turn (#195, thanks @obahareth).
- Claude's reasoning reaches Hermes again: adaptive-thinking requests ask for `thinking.display: "summarized"`, as core's Anthropic adapter does. Claude 4.7 and later default the display to omitted, which returned signed but empty thinking blocks and left the reasoning panel blank (#45, thanks @mister-ideas). A turn of thinking alone, with no text and no tool call, still reaches Hermes without reasoning, so Hermes nudges the model on instead of taking the thinking as the final answer and ending the tool loop (thanks @100yenadmin, @shaggy2626).
- Long tool calls are no longer cancelled as stalls: the relay asks the API to stream each tool's arguments as they are written (`eager_input_streaming`). Native enables that only for a first-party base URL, so behind the relay a whole-file `write_file` argument arrived in one burst after minutes of silence (live: 89.8 s with no chunk on Opus 5.5) and Hermes' stall timer killed the healthy call.
- Report `Claude request cancelled` when a request is cancelled while native acknowledges replayed history, and include native's exit code and stderr when native exits before that acknowledgment (#122, thanks @Eros-H).
- Restore gzip compression for large direct Anthropic requests, with a process-wide latch and one uncompressed resend when request encoding is rejected.
- A reasoning-off request (`reasoning.enabled: false` or effort `none`) on a route that cannot disable thinking (Haiku 5.5, Sonnet 5.5, Opus 5.5, Fable) now asks for `effort: low` instead of sending no effort, where native thought at its default effort. Routes that accept the disable still receive it.
- Structured output no longer 400s upstream on schema keywords native rejects: `minimum`/`maximum`/`exclusiveMinimum`/`exclusiveMaximum`/`multipleOf` on integer and number, `maxItems`/`uniqueItems` on array, `minProperties`/`maxProperties` on object, and `type` beside `enum` (a nullable enum) are dropped from the wire schema only, and an array `minItems` above 1 is sent as 1. Hermes still validates the reply against the full schema, so the constraints hold, and core no longer retries without a schema (a doubled call).
- Tell the host the request is alive while native streams events that carry no text (tool-call arguments as `input_json_delta`, thinking signatures, block boundaries). Hermes' stale detector only sees chunks, so a long tool call used to look like a stall and the host cancelled a healthy request at its stale budget, then replayed it (#35). Each such event now yields a content-free chunk; pings do not, since they show an open connection and not model work. Final responses are unchanged.
- Keep one native session per conversation (`--session-id` from the system prompt and first frame): with feature flags on, native names a per-session scratchpad directory in the environment message after the first user turn, so a new session per spawn changed it on every request and each round re-wrote the conversation to the prompt cache.
- Short model names such as `sonnet-5-5`, `sonnet-5.5`, `opus-4.8` and `haiku-4-5` resolve to their pinned routes (and thinking policy) instead of reaching Claude Code verbatim and 404ing into the fallback chain (#103).
- `/usage` asks Claude Code for the plan limits (`get_usage` control request) instead of reading its login: it now works with `auth.adopt_external_logins: false`, follows the per-profile `claude_config_dir`, and the plugin never holds the CLI's token (#82).
- Return completed per-call answers at native's result event while its shutdown drains in the background with a bounded grace period (~0.6 s per request under the default telemetry setting). Only a clean admitted answer takes this path; its exit code is no longer read.
- Accept Hermes tool names of 51–64 characters through stable 50-character aliases, mapping native tool calls back to the original names and preserving replay.
- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
- Carry the login identity Claude Code's local check reads (`USER`/`LOGNAME`, `USERNAME` on Windows) into the child env, derived from the parent process or the running uid: without it a perfectly logged-in CLI reports `loggedIn: false` instantly, with no stderr and no upstream request — the confirmed mechanism behind gateway-only `ClaudeCodeLoggedOut` failures (#44).
- Capture native stderr (bounded, drained off-thread) and include its first lines in `ClaudeCodeLoggedOut` and native-failure errors, so a gateway-only failure is diagnosable instead of vanishing into `DEVNULL` (#44).
- A `ClaudeCodeLoggedOut` error names the Claude Code binary it ran and the login directory that spawn used (its `CLAUDE_CONFIG_DIR`, else its own `HOME`'s `.claude`), so a gateway that resolves a different CLI or login than your shell is visible in the error itself (#47).
