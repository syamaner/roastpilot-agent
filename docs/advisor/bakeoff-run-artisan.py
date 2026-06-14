"""Artisan-expanded advisor bake-off run (14 Jun 2026).

Fixes the ``N=2`` caveat of the first run (``bakeoff-run-6model.py``) by
replaying the operator's quality-filtered Artisan roasts (drop < 198 °C — the
operator's bitterness ceiling) instead of only the two 7-Jun live captures.

Design (operator decision, 14 Jun):

- **Prompt:** v2 only. The first run showed v3 did not beat v2 (it *hurt*
  gemini-flash-lite), so v3 is not re-paid for here.
- **Roster A — the cheap FC-viable set, on all 28 roasts:** the run whose job
  is to make the drop-F1 robust on real data and confirm the standout.
- **Opus 3-roast spot-check:** the incumbent frontier on a DTR-spanning subset,
  to re-confirm (cheaply) the over-hold finding on Artisan data without paying
  for all 28.

The fixtures are anonymized (``artisan-NN``, no roast dates) and gitignored;
this runner + the emitted scorecard are committed. Re-run with the key:
``OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-artisan.py``
(regenerate the fixtures first with ``scripts/alog_to_fixture.py``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import advisor_bakeoff as b  # noqa: E402
from advisor_bakeoff import (  # noqa: E402
    Candidate,
    Tier,
    render_replay_report,
    replay_cells_to_json,
    run_replay_bakeoff,
)

from roastpilot_agent.models import RoastPhase  # noqa: E402

DEV = (RoastPhase.DEVELOPMENT,)
PRE = (RoastPhase.ROASTING_PRE_FIRST_CRACK,)

# Roster A: the ≤3 s FC-viable cheap set (operator's >3 s exclusion), verified
# against OpenRouter's live catalog in the first run.
ROSTER_CHEAP: tuple[Candidate, ...] = (
    Candidate("google/gemini-3.1-flash-lite", Tier.ULTRA_FLASH, DEV),
    Candidate("openai/gpt-5.4-nano", Tier.ULTRA_FLASH, DEV),
    Candidate("openai/gpt-4.1-mini", Tier.ULTRA_FLASH, DEV),
    Candidate("meta-llama/llama-3.3-70b-instruct", Tier.SPEED_AND_POWER, PRE),
)
# Incumbent frontier — spot-check only (over-hold re-confirmation on Artisan).
ROSTER_OPUS: tuple[Candidate, ...] = (
    Candidate("anthropic/claude-opus-4.8", Tier.INCUMBENT, b.PHASE_ORDER),
)

FIXTURES = REPO_ROOT / ".artisan-fixtures"
ALL_ROASTS: tuple[Path, ...] = tuple(sorted((FIXTURES).glob("artisan-*/roast.jsonl")))
# DTR-spanning subset for the Opus spot-check: mid / high / low DTR near ceiling.
SPOT_LABELS = ("artisan-09", "artisan-17", "artisan-28")
SPOT_ROASTS: tuple[Path, ...] = tuple(FIXTURES / label / "roast.jsonl" for label in SPOT_LABELS)

CADENCE_SECONDS = 30.0
OUT_JSON = REPO_ROOT / "docs" / "advisor" / "bakeoff-results-artisan-2026-06-14.json"
OUT_MD = REPO_ROOT / "docs" / "advisor" / "bakeoff-results-artisan-2026-06-14.md"


async def main() -> int:
    """Run both passes, merge, and write the anonymized scorecard."""
    missing = [str(p) for p in (*ALL_ROASTS, *SPOT_ROASTS) if not p.exists()]
    if not ALL_ROASTS or missing:
        print("fixtures missing — run scripts/alog_to_fixture.py first", flush=True)
        print("\n".join(missing[:5]), flush=True)
        return 1

    print(
        f"PASS 1 — roster A ({len(ROSTER_CHEAP)} models) x {len(ALL_ROASTS)} roasts, v2",
        flush=True,
    )
    avail_cheap, cells_cheap = await run_replay_bakeoff(
        ROSTER_CHEAP, ALL_ROASTS, ["v2"], None, CADENCE_SECONDS
    )
    print(f"PASS 2 — Opus spot-check x {len(SPOT_ROASTS)} roasts, v2", flush=True)
    avail_opus, cells_opus = await run_replay_bakeoff(
        ROSTER_OPUS, SPOT_ROASTS, ["v2"], None, CADENCE_SECONDS
    )

    report = (
        render_replay_report(cells_cheap, ALL_ROASTS)
        + "\n\n---\n\n## Opus 3-roast spot-check (DTR-spanning subset)\n\n"
        + f"Subset: {', '.join(SPOT_LABELS)}\n\n"
        + render_replay_report(cells_opus, SPOT_ROASTS)
    )
    print("\n" + report, flush=True)

    OUT_JSON.write_text(
        json.dumps(
            {
                "mode": "replay",
                "test_set": "artisan-drop-lt-198",
                "prompt_versions": ["v2"],
                "cadence_seconds": CADENCE_SECONDS,
                "passes": [
                    {
                        "name": "roster-a-all",
                        "roasts": [p.parent.name for p in ALL_ROASTS],
                        "availability": [dataclasses.asdict(a) for a in avail_cheap],
                        "cells": replay_cells_to_json(cells_cheap),
                    },
                    {
                        "name": "opus-spot-check",
                        "roasts": list(SPOT_LABELS),
                        "availability": [dataclasses.asdict(a) for a in avail_opus],
                        "cells": replay_cells_to_json(cells_opus),
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    OUT_MD.write_text(report, encoding="utf-8")
    print(f"\nwrote -> {OUT_JSON}\nwrote -> {OUT_MD}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
