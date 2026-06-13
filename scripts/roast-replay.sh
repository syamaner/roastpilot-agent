#!/usr/bin/env bash
#
# roast-replay.sh — device SSE test (issue #135). NO hardware.
#
# Replays a recorded roast + serves the SPA on one LAN origin, then prints the
# URL to open on your iPad/Mac. Does all setup (venv, deps, SPA build) for you.
#
# Usage:   ./scripts/roast-replay.sh
# Env:     PORT=8000  FIXTURE=tests/fixtures/replay/session-2  SPEED=1
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PORT="${PORT:-8000}"
FIXTURE="${FIXTURE:-tests/fixtures/replay/session-2}"
SPEED="${SPEED:-1}"

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

# LAN IP (so the iPad can reach the Mac): en0, then en1, then any non-loopback.
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127' | head -1 || true)"
[ -n "$IP" ] || IP="127.0.0.1"

cat <<BANNER

════════════════════════════════════════════════════════════════
  Device SSE test (#135) — open in Safari on your iPad and Mac:

      http://${IP}:${PORT}/

  Replaying ${FIXTURE} at ${SPEED}x.  Ctrl-C to stop.
  Critical check: a UI disconnect must cause NO backend change —
  watch this terminal (the decision trace) while you background /
  lock / wifi-blip the device.
════════════════════════════════════════════════════════════════

BANNER

exec roastpilot-agent --replay "$FIXTURE" --speed "$SPEED" --host 0.0.0.0 --port "$PORT"
