"""Target-sensitivity addendum to the held-out validation (#194).

NOT the conclusion — a "what-if" verification. The stringent held-out run
(`bakeoff-holdout-prompts.py`) feeds the advisor each over-dark roast's *actual*
drop as `target_drop_temp_c` (e.g. 200 °C), testing whether v4's ≤196 ceiling
overrides a too-high target. This addendum runs the same 19 unseen roasts but
feeds the operator's *intended* profile target (≈195 °C / 15 % DTR) instead —
isolating how much the recommended drop follows the target vs the prompt's own
ceiling logic. It is a target-SENSITIVITY / counterfactual analysis (vary one
input, hold model+prompt fixed), not an ablation (no component is removed).

Comparing a prompt's recommended drop temperature under target=actual-over-dark
vs target=195 on the *same* roast measures its robustness to a mis-set target.
Scoring is still against the real (over-dark) drop, so the operator win
criterion — recommend a drop **lower than the actual AND ≥ 193 °C** — applies to
both runs.

Run: `OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-holdout-addendum.py`
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from bakeoff_replay import build_ticks, replay_roast, score_roast, score_to_json  # noqa: E402

from roastpilot_agent.advisor import PydanticAIAdvisor  # noqa: E402
from roastpilot_agent.config import AdvisorConfig  # noqa: E402

PINNED_MODEL = "google/gemini-3.1-flash-lite"
PROMPT_VERSIONS = ["v2", "v4", "v5"]
CADENCE_SECONDS = 30.0
HELD_OUT_MIN_DROP_C = 198.0
# The operator's INTENDED profile target (the "what-if" input), decoupled from
# the roasts' actual over-dark drops.
TARGET_DROP_C = 195.0
TARGET_DEVELOPMENT_PERCENT = 15.0

FIXTURES = REPO_ROOT / ".artisan-holdout"
OUT_JSON = REPO_ROOT / "docs" / "advisor" / "bakeoff-holdout-addendum-2026-06-14.json"


def _held_out_roasts() -> list[Path]:
    """The unseen fixtures: those whose summary.json drop ≥ 198 °C."""
    roasts: list[Path] = []
    for summary in sorted(FIXTURES.glob("artisan-*/summary.json")):
        if float(json.loads(summary.read_text())["drop_temp_c"]) >= HELD_OUT_MIN_DROP_C:
            roasts.append(summary.parent / "roast.jsonl")
    return roasts


async def main() -> int:
    """Replay the 19 unseen roasts with the intended target; write the scorecard."""
    roasts = _held_out_roasts()
    if len(roasts) < 10:
        print(f"only {len(roasts)} held-out roasts — regenerate .artisan-holdout first")
        return 1
    print(
        f"target-sensitivity addendum: {len(roasts)} unseen roasts, "
        f"intended target {TARGET_DROP_C:.0f} °C / {TARGET_DEVELOPMENT_PERCENT:.0f}% DTR",
        flush=True,
    )
    results: dict[str, list[dict[str, object]]] = {}
    for pv in PROMPT_VERSIONS:
        advisor = PydanticAIAdvisor(AdvisorConfig(model_slug=PINNED_MODEL, prompt_version=pv))
        cells: list[dict[str, object]] = []
        for fixture in roasts:
            ticks, ground = build_ticks(
                fixture,
                cadence_seconds=CADENCE_SECONDS,
                target_drop_c_override=TARGET_DROP_C,
                target_development_percent_override=TARGET_DEVELOPMENT_PERCENT,
            )
            outcomes = await replay_roast(
                ticks, advisor.get_recommendation, clock=time.perf_counter
            )
            name = fixture.parent.name
            score = score_to_json(score_roast(outcomes, ground, name))
            score["human_drop_temp_c"] = round(ground.drop_temp_c, 1)
            cells.append(score)
            print(f"  {pv} {name}: drop F1={score['drop']['f1']}", flush=True)  # type: ignore[index]
        results[pv] = cells

    OUT_JSON.write_text(
        json.dumps(
            {
                "mode": "replay-holdout-addendum",
                "kind": "target-sensitivity",
                "pinned_model": PINNED_MODEL,
                "intended_target_drop_c": TARGET_DROP_C,
                "intended_target_development_percent": TARGET_DEVELOPMENT_PERCENT,
                "roasts": [p.parent.name for p in roasts],
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote -> {OUT_JSON}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
