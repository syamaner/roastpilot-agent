#!/bin/bash
# readonly-bash-guard.sh
#
# PreToolUse(Bash) guard for the read-only planning-architect subagent.
#
# Layered read-only enforcement (see docs/agent-topology.md, F1):
#   1. Primary control: the agent's tool list grants no Edit or Write.
#   2. permissionMode: plan restricts Bash to read-only use.
#   3. This hook is defence-in-depth for the case the docs call out, where a
#      permissive parent session (acceptEdits / auto / bypassPermissions)
#      discards the subagent's plan mode. It blocks the common Bash write
#      vectors so the planner cannot mutate repository state even then.
#
# This is the documented db-reader denylist pattern: a conservative block of
# known mutation vectors, not a proof of read-only-ness. It reads the hook JSON
# on stdin, extracts .tool_input.command, and exits 2 to block a write.

set -euo pipefail

INPUT="$(cat)"
COMMAND="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')"

if [ -z "$COMMAND" ]; then
  # No command to inspect; nothing to block.
  exit 0
fi

# Output redirection to a file, or append, or here-string-to-file style writes.
if printf '%s' "$COMMAND" | grep -Eq '(^|[^0-9>])>>?[[:space:]]*[^&|]'; then
  echo "Blocked: output redirection is a write. The planning-architect is read-only." >&2
  exit 2
fi

# Mutating commands (whole-word match, case-insensitive). Read-only git
# subcommands (log, show, diff, status, blame, rev-parse, ls-files, cat-file,
# describe, shortlog) are deliberately NOT listed and pass through.
MUTATORS='rm|rmdir|mv|cp|dd|tee|truncate|install|mkdir|touch|chmod|chown|chgrp|ln|shred|patch'
GIT_WRITES='commit|add|rm|mv|push|pull|fetch|clone|checkout|switch|reset|restore|rebase|merge|cherry-pick|revert|stash|tag|branch|apply|am|clean|gc|init|remote|config|update-ref|worktree'
INPLACE='(sed|perl|gawk|awk)[[:space:]]+[^|]*-i'
PKG='(npm|pnpm|yarn|pip|pip3|uv|poetry|brew|apt|apt-get|gem|cargo|go)[[:space:]]+(install|add|remove|uninstall|update|upgrade|ci|publish|build|run)'

if printf '%s' "$COMMAND" | grep -Eiq "\\b(${MUTATORS})\\b"; then
  echo "Blocked: mutating command detected. The planning-architect is read-only." >&2
  exit 2
fi
if printf '%s' "$COMMAND" | grep -Eiq "\\bgit[[:space:]]+(-[^[:space:]]+[[:space:]]+)*(${GIT_WRITES})\\b"; then
  echo "Blocked: git write subcommand. The planning-architect inspects history read-only (log/show/diff/blame)." >&2
  exit 2
fi
if printf '%s' "$COMMAND" | grep -Eiq "${INPLACE}"; then
  echo "Blocked: in-place edit flag. The planning-architect is read-only." >&2
  exit 2
fi
if printf '%s' "$COMMAND" | grep -Eiq "${PKG}"; then
  echo "Blocked: package/build mutation. The planning-architect is read-only." >&2
  exit 2
fi

exit 0
