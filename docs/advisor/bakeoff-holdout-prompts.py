"""Held-out validation of the winning drop-recall prompts (#194).

The v4–v8 prompts were hand-authored from the operator's roast PROFILE, which is
the aggregate of the 28 quality-filtered Artisan roasts (drop < 198 °C) used in
the prompt bake-off — a population-level train-on-test risk. This run validates
the winner (v4) and runner-up (v5) against the **19 UNSEEN roasts** the prompt
work never touched: the over-dark logs the operator excluded (drop ≥ 198 °C).

These are a harder, differently-shaped test, read on two axes:
- **Generalization (clean):** does the prompt reliably RECOGNIZE the drop window
  on roasts it never informed (recall > 0)?
- **Ceiling behavior:** on roasts the operator dropped at 198–202 °C, does the
  prompt recommend dropping EARLIER (≤ ~196 °C indicated), i.e. would it have
  caught the over-roast? Caveat: the harness feeds the advisor
  ``target_drop_temp_c = the roast's actual (over-dark) drop``, so this measures
  whether v4's ≤196 ceiling overrides an over-dark profile target.

Agreement-with-the-human is NOT "correctness" here — the human over-roasted, so
a lower-agreement EARLIER drop is the better outcome. Read recall + the drop-temp
distribution, not F1 alone. v2 is included as the baseline reference.
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

from advisor_bakeoff import (  # noqa: E402
    Candidate,
    Tier,
    render_replay_report,
    replay_cells_to_json,
    run_replay_bakeoff,
)

from roastpilot_agent.models import RoastPhase  # noqa: E402

DEV = (RoastPhase.DEVELOPMENT,)
ROSTER: tuple[Candidate, ...] = (Candidate("google/gemini-3.1-flash-lite", Tier.ULTRA_FLASH, DEV),)
PROMPT_VERSIONS = ["v2", "v4", "v5"]  # baseline + winner + runner-up
CADENCE_SECONDS = 30.0
HELD_OUT_MIN_DROP_C = 198.0  # the operator's excluded over-dark roasts = unseen

FIXTURES = REPO_ROOT / ".artisan-holdout"
OUT_JSON = REPO_ROOT / "docs" / "advisor" / "bakeoff-holdout-2026-06-14.json"
OUT_MD = REPO_ROOT / "docs" / "advisor" / "bakeoff-holdout-2026-06-14.md"


def _held_out_roasts() -> tuple[Path, ...]:
    """The unseen fixtures: those whose summary.json drop ≥ 198 °C."""
    roasts: list[Path] = []
    for summary in sorted(FIXTURES.glob("artisan-*/summary.json")):
        drop = float(json.loads(summary.read_text())["drop_temp_c"])
        if drop >= HELD_OUT_MIN_DROP_C:
            roasts.append(summary.parent / "roast.jsonl")
    return tuple(roasts)


async def main() -> int:
    """Run v2/v4/v5 over the unseen over-dark roasts and write the scorecard."""
    roasts = _held_out_roasts()
    if len(roasts) < 10:
        print(f"only {len(roasts)} held-out roasts found — regenerate .artisan-holdout first")
        return 1
    print(
        f"held-out: {len(roasts)} unseen roasts (drop ≥ {HELD_OUT_MIN_DROP_C:.0f} °C)", flush=True
    )
    availability, cells = await run_replay_bakeoff(
        ROSTER, roasts, PROMPT_VERSIONS, None, CADENCE_SECONDS
    )
    report = render_replay_report(cells, roasts)
    print("\n" + report, flush=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "mode": "replay-holdout",
                "test_set": "artisan-drop-ge-198-unseen",
                "pinned_model": ROSTER[0].slug,
                "prompt_versions": PROMPT_VERSIONS,
                "cadence_seconds": CADENCE_SECONDS,
                "roasts": [p.parent.name for p in roasts],
                "availability": [dataclasses.asdict(a) for a in availability],
                "cells": replay_cells_to_json(cells),
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
