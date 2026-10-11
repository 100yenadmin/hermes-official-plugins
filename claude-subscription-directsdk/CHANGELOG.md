# Changelog

## [0.3.1] - Unreleased

### Added

- Per-profile `claude_config_dir` setting selects the Claude login for every native spawn in a multiplexed gateway (#34).

### Fixed

- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
