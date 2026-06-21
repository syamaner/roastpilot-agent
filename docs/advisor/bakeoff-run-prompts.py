"""Prompt-tuning advisor bake-off run (#194, 14 Jun 2026).

The advisor MODEL is pinned (D33: ``google/gemini-3.1-flash-lite`` + prompt
``v2``). This run tunes the PROMPT for that pinned model: gemini is the only
model that reliably calls the drop, but on the 28-roast Artisan-expanded set its
drop **recall is ~0.64** — it misses the drop on ~10/28 roasts, leaning to
develop LATER than the operator did. Root cause is v2's drop language ("guide
not a hard stop … fine to develop modestly PAST it … don't rush the drop").

This runner overrides the roster to the SINGLE pinned candidate and replays all
28 quality-filtered Artisan roasts (drop < 198 °C indicated) across the prompt
versions ``["v2", "v4", "v5", "v6", "v7", "v8"]`` (v2 = baseline), at 30 s
cadence — the same replay pipeline / scoring as ``bakeoff-run-artisan.py``.

v4-v8 keep ALL of v2's heat/fan control guidance intact (the anticipatory
thermal-lag cut + fan-as-convective-transfer-mode that scored 0.88
heat-direction) and change ONLY the drop-decision guidance, each a distinct
strategy (see ``advisor._PROMPTS`` comments):

- v4 — profile-target anchor (drop at/near ``target_drop_temp_c``, floor met).
- v5 — profile development-target as the indicator.
- v6 — full two-sided window: floor / ≤196 °C indicated ceiling / flick guard.
- v7 — FC-detector-lag-aware (development clock is a lower bound).
- v8 — concise rule-forward synthesis.

The fixtures are anonymized (``artisan-NN``, no roast dates) and gitignored;
this runner + the emitted scorecard are committed. Re-run with the key:
``OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-prompts.py``
(regenerate the fixtures first with ``scripts/alog_to_fixture.py``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from advisor_bakeoff import (  # noqa: E402
    Candidate,
    ReplayCell,
    Tier,
    render_replay_report,
    replay_cells_to_json,
    run_replay_bakeoff,
)

from roastpilot_agent.models import RoastPhase  # noqa: E402

DEV = (RoastPhase.DEVELOPMENT,)

# The single PINNED candidate — this run tunes its PROMPT, not the model.
ROSTER: tuple[Candidate, ...] = (
    Candidate("google/gemini-3.1-flash-lite", Tier.CONTROL_CANDIDATE, DEV),
)

PROMPT_VERSIONS = ["v2", "v4", "v5", "v6", "v7", "v8"]
CADENCE_SECONDS = 30.0

FIXTURES = REPO_ROOT / ".artisan-fixtures"
ALL_ROASTS: tuple[Path, ...] = tuple(sorted((FIXTURES).glob("artisan-*/roast.jsonl")))
# The quality-filtered Artisan set is exactly 28 roasts (drop < 198 °C). The dir
# is gitignored, so a partial/stale regeneration would silently spend credits and
# compute "mean across 28" over the wrong population (#199 / Codex #196-#3).
EXPECTED_ROAST_COUNT = 28

OUT_JSON = REPO_ROOT / "docs" / "advisor" / "bakeoff-results-prompts-2026-06-14.json"
OUT_MD = REPO_ROOT / "docs" / "advisor" / "bakeoff-results-prompts-2026-06-14.md"


def _mean(values: list[float]) -> float | None:
    """Mean of the present values, or ``None`` if empty."""
    return round(statistics.mean(values), 3) if values else None


def aggregate(cell: ReplayCell) -> dict[str, Any]:
    """Aggregate one prompt's per-roast scores across all 28 roasts.

    Mirrors the report metrics the operator cares about for the recall gap: mean
    drop F1 / precision / recall, the count of roasts the drop was called on, the
    false-positive count, drop-timing error, and heat/fan directional agreement.

    Args:
        cell: One scored prompt cell (all 28 roasts).

    Returns:
        A flat dict of the aggregated metrics for the scorecard.
    """
    n = len(cell.scores)
    f1s = [s.drop.f1 for s in cell.scores]
    precisions = [s.drop.precision for s in cell.scores]
    recalls = [s.drop.recall for s in cell.scores]
    # A roast where the model called the drop at least once = a true positive
    # somewhere in the roast (recall > 0 on this single-drop-positive label).
    called = [s for s in cell.scores if s.drop.true_positives > 0]
    false_positive_total = sum(s.drop.false_positives for s in cell.scores)
    roasts_with_fp = [s for s in cell.scores if s.drop.false_positives > 0]
    timing_s = [
        s.drop.timing_error_seconds for s in cell.scores if s.drop.timing_error_seconds is not None
    ]
    timing_c = [s.drop.timing_error_c for s in cell.scores if s.drop.timing_error_c is not None]
    heat_dir = [
        s.heat.directional_agreement
        for s in cell.scores
        if s.heat.directional_agreement is not None
    ]
    fan_dir = [
        s.fan.directional_agreement for s in cell.scores if s.fan.directional_agreement is not None
    ]
    heat_mae = [s.heat.mae for s in cell.scores]
    fan_mae = [s.fan.mae for s in cell.scores]
    return {
        "prompt_version": cell.prompt_version,
        "roasts": n,
        "drop_f1_mean": _mean(f1s),
        "drop_precision_mean": _mean(precisions),
        "drop_recall_mean": _mean(recalls),
        "called_drop_on": len(called),
        "false_positive_roasts": len(roasts_with_fp),
        "false_positive_ticks_total": false_positive_total,
        "drop_timing_s_mean": _mean(timing_s),
        "drop_timing_c_mean": _mean(timing_c),
        "heat_direction_mean": _mean(heat_dir),
        "heat_mae_mean": _mean(heat_mae),
        "fan_direction_mean": _mean(fan_dir),
        "fan_mae_mean": _mean(fan_mae),
    }


def render_summary_table(aggregates: list[dict[str, Any]]) -> str:
    """Render the per-prompt comparison table (markdown) for the scorecard."""
    header = (
        "| prompt | drop F1 | precision | recall | called drop on | FP roasts "
        "| dropΔ (s/°C) | heat-dir | heat MAE | fan-dir |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    rows = [header]
    for a in aggregates:
        baseline = " (baseline)" if a["prompt_version"] == "v2" else ""
        rows.append(
            f"| **{a['prompt_version']}**{baseline} | {a['drop_f1_mean']} | "
            f"{a['drop_precision_mean']} | {a['drop_recall_mean']} | "
            f"{a['called_drop_on']}/{a['roasts']} | {a['false_positive_roasts']} | "
            f"{a['drop_timing_s_mean']}/{a['drop_timing_c_mean']} | "
            f"{a['heat_direction_mean']} | {a['heat_mae_mean']} | "
            f"{a['fan_direction_mean']} |"
        )
    return "\n".join(rows)


async def main() -> int:
    """Run the prompt sweep on the pinned model and write the scorecard."""
    if not ALL_ROASTS:
        print("fixtures missing — run scripts/alog_to_fixture.py first", flush=True)
        return 1
    if len(ALL_ROASTS) != EXPECTED_ROAST_COUNT:
        print(
            f"fixture count mismatch: found {len(ALL_ROASTS)} roasts in {FIXTURES}, "
            f"expected exactly {EXPECTED_ROAST_COUNT} (drop < 198 °C). A partial or "
            f"stale .artisan-fixtures would skew the 'mean across 28' result and spend "
            f"credits on the wrong population — regenerate via scripts/alog_to_fixture.py.",
            flush=True,
        )
        return 1

    print(
        f"prompt sweep — {ROSTER[0].slug} x {len(ALL_ROASTS)} roasts "
        f"x prompts {PROMPT_VERSIONS} @ {CADENCE_SECONDS}s",
        flush=True,
    )
    availability, cells = await run_replay_bakeoff(
        ROSTER, ALL_ROASTS, PROMPT_VERSIONS, None, CADENCE_SECONDS
    )

    # Aggregate per prompt, preserving the PROMPT_VERSIONS order.
    by_pv = {c.prompt_version: c for c in cells}
    aggregates = [aggregate(by_pv[pv]) for pv in PROMPT_VERSIONS if pv in by_pv]
    summary_table = render_summary_table(aggregates)

    report = (
        "# Advisor bake-off — prompt-tuning sweep (#194, drop-recall gap)\n\n"
        "> Pinned model `google/gemini-3.1-flash-lite` (D33). This run tunes the "
        "PROMPT, not the model: v2 = baseline; v4-v8 keep v2's heat/fan control "
        "intact and change only the drop-decision guidance to close the "
        "~0.64-recall gap (gemini misses the drop on ~10/28 roasts, leaning "
        "late). Agreement-with-the-operator is the target here, NOT proven "
        "optimality; precision matters as much as recall (an over-eager prompt "
        "racks up false positives = under-developed roasts). The variants are "
        "hand-authored against this same 28-roast set, so read for mild "
        "train-on-test bias.\n\n"
        "## Per-prompt comparison (mean across 28 roasts)\n\n"
        f"{summary_table}\n\n"
        "---\n\n" + render_replay_report(cells, ALL_ROASTS)
    )
    print("\n" + summary_table, flush=True)

    OUT_JSON.write_text(
        json.dumps(
            {
                "mode": "replay",
                "test_set": "artisan-drop-lt-198",
                "pinned_model": ROSTER[0].slug,
                "prompt_versions": PROMPT_VERSIONS,
                "cadence_seconds": CADENCE_SECONDS,
                "roasts": [p.parent.name for p in ALL_ROASTS],
                "availability": [dataclasses.asdict(a) for a in availability],
                "aggregates": aggregates,
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
