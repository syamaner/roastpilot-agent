#!/bin/bash
# readonly-bash-guard.sh
#
# Thin, fail-closed entrypoint for the planning-architect read-only Bash guard.
# The real logic lives in readonly-bash-guard.py (the repo's declared runtime is
# Python 3.11+, so it is a guaranteed prerequisite; jq is not). This wrapper
# fails CLOSED (exit 2, block) if the guard cannot run at all, so a permissive
# parent session can never leave the planner's Bash unguarded.
#
# See docs/agent-topology.md (F1).

set -uo pipefail

INPUT="$(cat)"

# Resolve relative to the project root, not the current working directory, so a
# session launched from a subdirectory still finds the guard.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
GUARD="${PROJECT_DIR}/.claude/hooks/readonly-bash-guard.py"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Blocked: read-only guard runtime (python3) unavailable (fail-closed)." >&2
  exit 2
fi
if [ ! -f "$GUARD" ]; then
  echo "Blocked: read-only guard script not found at ${GUARD} (fail-closed)." >&2
  exit 2
fi

printf '%s' "$INPUT" | python3 "$GUARD"
exit $?
