#!/usr/bin/env bash
# Offline evals for CI: the real Claude Code binary against a loopback peer, with no credentials and no inference.
# Pinned so a new Claude Code release can't turn a PR red; bump CLAUDE_CODE_VERSION to qualify a release.
set -euo pipefail
cd "$(dirname "$0")/.."
version="${CLAUDE_CODE_VERSION:-2.1.292}"
prefix="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/claude-code.XXXXXX")"
npm install --prefix "$prefix" --no-audit --no-fund --loglevel=error "@anthropic-ai/claude-code@$version"
claude="$prefix/node_modules/.bin/claude"
"$claude" --version
for eval in evals/directsdk_admission.py evals/directsdk_cache_wire.py evals/directsdk_cache_continuity.py; do
  echo "::group::$eval"
  python "$eval" "$claude"
  echo "::endgroup::"
done
# Prefix continuity again with native's account context, as a subscription login sends it (emulated, no account).
echo "::group::evals/directsdk_cache_continuity.py --subscription"
python evals/directsdk_cache_continuity.py "$claude" --subscription
echo "::endgroup::"
