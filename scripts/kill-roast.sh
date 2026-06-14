#!/usr/bin/env bash
#
# kill-roast.sh — operator tooling (not part of the agent runtime). Force-terminate EVERY RoastPilot agent
# + coffee-roaster-mcp instance (the agent's own child AND any stray uvx one) and
# free the USB serial port / mic / :8000.
#
# Use when a run wedges and Ctrl-C / the in-UI stop won't end it (issue #212:
# graceful shutdown can hang, SIGTERM is ignored, only SIGKILL works), or to clear
# leftovers before a fresh `roast-live.sh`.
#
#   ./scripts/kill-roast.sh
#
# ⚠ SAFETY: this is a FORCE-KILL (kill -9). It does NOT run the heat-off path — if a
#   run left the Hottop commanded HOT, killing the process does not cool it. Power
#   off / cut power AT THE MACHINE if there's any chance heat is still on. This only
#   frees host resources so a clean run can start.

set -uo pipefail   # NOT -e: pkill returns non-zero when nothing matches; that's fine.

PORT="${PORT:-8000}"   # match roast-live.sh's PORT override for the :PORT free-check below

echo "⚠  Force-kill does NOT turn off heat. If a run left the Hottop hot, power it"
echo "   off at the machine — this only frees host processes/ports."
echo

echo "→ before:"
ps -Ao pid,ppid,command | grep -iE 'roastpilot-agent serve|coffee-roaster-mcp|uvx coffee-roaster-mcp' | grep -v grep || echo "  (none running)"
echo

echo "→ SIGKILL all roastpilot-agent + coffee-roaster-mcp (agent child + any stray uvx)…"
pkill -9 -f 'roastpilot-agent serve' && echo "  killed: roastpilot-agent" || echo "  (no roastpilot-agent)"
pkill -9 -f 'coffee-roaster-mcp'     && echo "  killed: coffee-roaster-mcp"  || echo "  (no coffee-roaster-mcp)"
pkill -9 -f 'uvx coffee-roaster-mcp'             && echo "  killed: uvx coffee-roaster"   || echo "  (no uvx coffee)"
sleep 1   # let the OS release the serial port + the :8000 socket

echo
echo "→ after:"
LEFT="$(ps -Ao pid,command | grep -iE 'roastpilot-agent serve|coffee-roaster-mcp|uvx coffee-roaster-mcp' | grep -v grep || true)"
if [ -n "$LEFT" ]; then
  echo "  STILL ALIVE (investigate):"; echo "$LEFT"
else
  echo "  ✓ no agent / MCP processes remain"
fi
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null && echo "  ⚠ :$PORT still held" || echo "  ✓ :$PORT free"
# Best-effort serial check (device path may vary; ignore if not present).
SERIAL="$(ls /dev/cu.usbserial-* 2>/dev/null | head -1 || true)"
if [ -n "$SERIAL" ]; then
  lsof "$SERIAL" 2>/dev/null && echo "  ⚠ serial $SERIAL still held" || echo "  ✓ serial $SERIAL free"
fi
echo
echo "Done. Confirm the Hottop is physically safe, then relaunch ./scripts/roast-live.sh"
