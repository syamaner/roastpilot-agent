#!/usr/bin/env bash
#
# roast-live.sh — supervised live roast THROUGH the agent (issue #134).
# REQUIRES the Hottop (USB serial) + USB mic. Supervised, abort-ready.
#
# Points the agent's spawned coffee-roaster-mcp child at your proven MCP config,
# does setup, waits until the server is ACTUALLY serving, prints the dashboard
# URL + the safety banner, then runs. Stay at the machine.
#
# Usage:   OPENROUTER_API_KEY=sk-... ./scripts/roast-live.sh
#          (omit the key to run advisory-paused — controller still runs safety)
# Env:     PORT=8000
#          COFFEE_ROASTER_MCP_CONFIG=~/roasts/coffee-roaster-mcp.yaml  (default)
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PORT="${PORT:-8000}"
: "${COFFEE_ROASTER_MCP_CONFIG:=$HOME/roasts/coffee-roaster-mcp.yaml}"
export COFFEE_ROASTER_MCP_CONFIG

if [ ! -f "$COFFEE_ROASTER_MCP_CONFIG" ]; then
  echo "ERROR: MCP config not found: $COFFEE_ROASTER_MCP_CONFIG" >&2
  echo "       Set COFFEE_ROASTER_MCP_CONFIG to your coffee-roaster-mcp.yaml" >&2
  echo "       (template: docs/examples/coffee-roaster-mcp.known-good.yaml)." >&2
  exit 1
fi

echo "→ preparing (venv, deps, SPA build)…"
if [ ! -x .venv/bin/python ]; then
  python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -e . --group dev
# Build the SPA if web/dist is missing OR stale vs the source — so a `git pull`
# that changed web/ doesn't keep serving an old bundle (e.g. a pre-dashboard stub).
if [ ! -f web/dist/index.html ] || \
   [ -n "$(find web/src web/package.json -type f -newer web/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "  building SPA (web/dist missing or stale)…"
  ( cd web && { [ -d node_modules ] || npm install; }; npm run build )
else
  echo "  SPA up to date."
fi

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127' | head -1 || true)"
[ -n "$IP" ] || IP="127.0.0.1"

ADV="advisor configured"
[ -n "${OPENROUTER_API_KEY:-}" ] || ADV="ADVISORY-PAUSED (no OPENROUTER_API_KEY)"

echo "→ starting agent + spawning MCP child (takes a few seconds — don't Ctrl-C yet)…"
echo "  MCP config: ${COFFEE_ROASTER_MCP_CONFIG}"
roastpilot-agent serve --host 0.0.0.0 --port "$PORT" &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' INT TERM

# Wait until serving, OR exits during startup (e.g. MCP child fails closed).
for _ in $(seq 1 120); do
  if ! kill -0 "$SRV" 2>/dev/null; then
    echo "✗ agent exited during startup — likely the MCP child / Hottop could not start (fail-closed). See output above." >&2
    wait "$SRV" || true
    exit 1
  fi
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/api/health"; then
    cat <<BANNER

════════════════════════════════════════════════════════════════════
  ✅ READY — SUPERVISED LIVE ROAST (#134). Stay at the machine, abort-ready.

  Advisor    : ${ADV}
  Dashboard  : http://${IP}:${PORT}/

  ⚠  Ctrl-C is NOT an emergency stop — it leaves the heater at its
     last setpoint. Use the in-UI EMERGENCY STOP or cut machine power.
     (A restart re-enters operator_recovery_required; never auto-resumes.)

  ⚠  Run the §1 COLD pre-flight BEFORE charging beans:
       • e-stop drives heat → 0
       • an over-limit command is CLAMPed/REJECTed in the trace
       • kill the agent mid-(dry)-run → restart enters recovery, commands nothing
════════════════════════════════════════════════════════════════════

BANNER
    wait "$SRV"
    exit $?
  fi
  sleep 0.5
done

echo "✗ agent did not become ready within ~60s." >&2
kill "$SRV" 2>/dev/null || true
exit 1
