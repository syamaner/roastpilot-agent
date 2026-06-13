#!/usr/bin/env bash
#
# roast-live.sh — supervised live roast THROUGH the agent (issue #134).
# REQUIRES the Hottop (USB serial) + USB mic. Supervised, abort-ready.
#
# Points the agent's spawned coffee-roaster-mcp child at your proven MCP config
# (the one that did a real FC-detecting roast), does setup, prints the dashboard
# URL, then launches. Stay at the machine.
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
if [ ! -f web/dist/index.html ]; then
  ( cd web && npm install && npm run build )
fi

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127' | head -1 || true)"
[ -n "$IP" ] || IP="127.0.0.1"

ADV="advisor configured"
[ -n "${OPENROUTER_API_KEY:-}" ] || ADV="ADVISORY-PAUSED (no OPENROUTER_API_KEY)"

cat <<BANNER

════════════════════════════════════════════════════════════════════
  SUPERVISED LIVE ROAST (#134) — stay at the machine, abort-ready.

  MCP config : ${COFFEE_ROASTER_MCP_CONFIG}
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

exec roastpilot-agent serve --host 0.0.0.0 --port "$PORT"
