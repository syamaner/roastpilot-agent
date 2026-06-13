#!/usr/bin/env bash
#
# roast-replay.sh — device SSE test (issue #135). NO hardware.
#
# Replays a recorded roast + serves the SPA on one LAN origin. Does all setup
# (venv, deps, SPA build), waits until the server is ACTUALLY serving, then
# prints the URL to open on your iPad/Mac. Ctrl-C stops it.
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
# Build the SPA if web/dist is missing OR stale vs the source — so a `git pull`
# that changed web/ doesn't keep serving an old bundle (e.g. a pre-dashboard stub).
if [ ! -f web/dist/index.html ] || \
   [ -n "$(find web/src web/package.json -type f -newer web/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "  building SPA (web/dist missing or stale)…"
  ( cd web && { [ -d node_modules ] || npm install; }; npm run build )
else
  echo "  SPA up to date."
fi

# LAN IP (so the iPad can reach the Mac): en0, then en1, then any non-loopback.
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127' | head -1 || true)"
[ -n "$IP" ] || IP="127.0.0.1"

echo "→ starting replay server (takes a few seconds — don't Ctrl-C yet)…"
roastpilot-agent --replay "$FIXTURE" --speed "$SPEED" --host 0.0.0.0 --port "$PORT" &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' INT TERM

# Wait until the server actually answers, OR exits during startup.
for _ in $(seq 1 120); do
  if ! kill -0 "$SRV" 2>/dev/null; then
    echo "✗ server exited during startup (see output above)." >&2
    wait "$SRV" || true
    exit 1
  fi
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/api/health"; then
    cat <<BANNER

════════════════════════════════════════════════════════════════
  ✅ READY — open in Safari on your iPad and Mac:

      http://${IP}:${PORT}/

  Replaying ${FIXTURE} at ${SPEED}x.  Ctrl-C to stop.
  Critical check: a UI disconnect must cause NO backend change —
  watch this terminal while you background / lock / wifi-blip the device.
════════════════════════════════════════════════════════════════

BANNER
    wait "$SRV"
    exit $?
  fi
  sleep 0.5
done

echo "✗ server did not become ready within ~60s." >&2
kill "$SRV" 2>/dev/null || true
exit 1
