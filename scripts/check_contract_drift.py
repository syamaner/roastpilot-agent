#!/usr/bin/env python3
"""Continuous contract-drift gate: regenerate the SPA contract surface from the
real server, then fail if it differs from the committed mirror (#121).

The SPA hand-mirrors the agent's SSE + REST contract in TypeScript. PR #120 made
that mirror drift-detectable two ways — the vitest runs the SPA parsers against
the committed fixture, and a deliberate server rename goes red when the dev
regenerates — but an *accidental* server-side field rename where nobody
regenerated slipped through: the committed fixture went stale and CI stayed green
(the #115 drift class). This script closes that gap continuously.

It regenerates the committed contract fixtures from the live server models (the
source of truth), with every volatile field normalized to a fixed sentinel so the
regeneration is byte-deterministic, then compares the result to what is committed.
A non-empty diff means the server's typed event/snapshot surface reshaped without
the fixture being regenerated — drift. The script restores the working tree to
its pre-run state regardless of outcome, so it never leaves the fixtures dirty.

This is the ``git diff --exit-code`` half of the #121 guard; the
``test_committed_*_is_in_sync_with_server`` tests in
``tests/test_contract_fixtures.py`` are the in-process half (same builders,
default-on in ``pytest``). Either alone catches accidental drift; both are wired
into CI so a green build genuinely means the SPA mirror matches the server.

Usage:
    python scripts/check_contract_drift.py

Exit codes:
    0 — the committed fixtures match a fresh regeneration (no drift).
    1 — drift detected (the diff and the regenerate hint are printed).
    2 — the regeneration step itself failed (a bug in the fixture pipeline).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# The committed contract surface this gate regenerates and compares. Paths are
# resolved from this file so the script is CWD-independent (CI invokes it from the
# repo root, but a teammate may run it from anywhere).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_DIR = _REPO_ROOT / "tests" / "fixtures" / "contract"
_CONTRACT_TEST = _REPO_ROOT / "tests" / "test_contract_fixtures.py"

_REGEN_HINT = (
    "Drift detected: the committed SPA contract fixtures no longer match a fresh "
    "regeneration from the server. A server model (SseEvent / TelemetryEventData "
    "/ RoastDetail / RoastSummary or an enum value) reshaped without the fixture "
    "being regenerated — the #115 drift class.\n\n"
    "If the server change is intended, regenerate the mirror and review the SPA "
    "TypeScript types alongside the diff:\n"
    "    REGEN_CONTRACT_FIXTURES=1 python -m pytest tests/test_contract_fixtures.py\n"
    "then commit the updated tests/fixtures/contract/*.json (and any matching "
    "web/src/lib/types.ts / web/src/pages/dashboard/events.ts edits)."
)


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a subprocess from the repo root, capturing text output."""
    return subprocess.run(  # noqa: S603 — fixed, non-user-supplied argv.
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )


def _git_diff() -> str:
    """The current ``git diff`` of the contract fixture directory (text)."""
    result = _run(["git", "diff", "--", str(_CONTRACT_DIR)])
    return result.stdout


def main() -> int:
    """Regenerate the contract fixtures and fail on any drift from committed.

    Returns:
        A process exit code (0 clean, 1 drift, 2 regeneration error).
    """
    # Refuse to run on an already-dirty fixture dir: we restore via ``git
    # checkout`` at the end, which would clobber a teammate's in-progress edit.
    pre_existing = _git_diff()
    if pre_existing.strip():
        print(
            "tests/fixtures/contract/ has uncommitted changes; commit or stash "
            "them before running the drift gate (it restores the tree on exit).",
            file=sys.stderr,
        )
        return 2

    # Regenerate the committed fixtures from the live server, deterministically.
    regen = _run(
        [sys.executable, "-m", "pytest", str(_CONTRACT_TEST), "-q"],
        env=_regen_env(),
    )
    if regen.returncode != 0:
        print(regen.stdout, file=sys.stderr)
        print(regen.stderr, file=sys.stderr)
        print(
            "Contract-fixture regeneration FAILED — this is a fixture-pipeline "
            "bug, not drift. Fix the regeneration before trusting the gate.",
            file=sys.stderr,
        )
        return 2

    diff = _git_diff()
    try:
        if diff.strip():
            print(diff)
            print(_REGEN_HINT, file=sys.stderr)
            return 1
        print("Contract fixtures are in sync with the server — no drift.")
        return 0
    finally:
        # Always restore the committed fixtures so the run leaves the tree clean,
        # whether it found drift or not.
        _run(["git", "checkout", "--", str(_CONTRACT_DIR)])


def _regen_env() -> dict[str, str]:
    """The current environment with the fixture-regeneration flag enabled."""
    import os

    env = dict(os.environ)
    env["REGEN_CONTRACT_FIXTURES"] = "1"
    return env


if __name__ == "__main__":
    raise SystemExit(main())
