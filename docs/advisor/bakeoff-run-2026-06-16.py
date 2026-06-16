"""Model-roster bake-off run (16 Jun 2026, D40.4).

A fresh roster comparison on the operator's Artisan roasts, adding the
n8n-proven control model (``openai/gpt-4o``) the prior runs lacked, plus the
"fast-enough" additions the operator picked (16 Jun). Holds the prompt at the
current drop pin (``v4``, D34) so this isolates the MODEL, scored on the same
real data that drove D33/D34.

Scope (D40.4): this is the DROP-decision refresh — it reuses the existing
drop/heat-direction replay scoring. The CONTROL-loop trajectory eval (fan-change
count, momentum cuts) lands once #273–#275 build the teaching prompt + the
roast-so-far context + the trajectory scorer; the post-FC control-loop model pin
comes from THAT eval, not this drop refresh.

Two passes so each model runs at a fair latency:

- **Pass 1 — non-reasoning roster, provider-default reasoning:** gpt-4o,
  gpt-4o-mini, gemini-3.1-flash-lite (incumbent pin), claude-haiku-4.5, and
  three frontier models within budget — claude-opus-4.8, claude-sonnet-4.6,
  gpt-5.5.
- **Pass 2 — gpt-5-mini at ``reasoning=low``:** it reasons before answering, so
  it is only "fast enough" with reasoning minimised; run it that way and let the
  recorded latency show whether it clears the ~5 s post-FC budget.

The availability sweep silently drops any slug OpenRouter cannot resolve, so a
wrong slug is skipped, not fatal.

Operator run (needs a key; regenerate the fixtures first)::

    python scripts/alog_to_fixture.py "<roasting-logs-dir>" --out-dir .artisan-fixtures
    OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-2026-06-16.py

Fixtures are anonymized (``artisan-NN``) and gitignored; this runner + the
emitted scorecard are committed.
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

# Pass 1 — non-reasoning roster (D40.4). gpt-4o is the n8n-proven control model
# (newly added); gemini-3.1-flash-lite is the incumbent pin (D33) to beat.
# Two frontier models within the post-FC budget (sonnet-4.6, gpt-5.5) alongside
# the opus-4.8 frontier — none carry latency-risk; recorded latency confirms.
ROSTER_MAIN: tuple[Candidate, ...] = (
    Candidate("openai/gpt-4o", Tier.PRIOR_FRONTIER, DEV),
    Candidate("openai/gpt-4o-mini", Tier.ULTRA_FLASH, DEV),
    Candidate("google/gemini-3.1-flash-lite", Tier.ULTRA_FLASH, DEV),
    Candidate("anthropic/claude-haiku-4.5", Tier.ULTRA_FLASH, DEV),
    Candidate("anthropic/claude-opus-4.8", Tier.INCUMBENT, b.PHASE_ORDER),
    Candidate("anthropic/claude-sonnet-4.6", Tier.PRIOR_FRONTIER, DEV),
    Candidate("openai/gpt-5.5", Tier.PRIOR_FRONTIER, DEV),
)

# Pass 2 — reasoning model, run at reasoning=low to stay inside the FC budget.
ROSTER_REASONING: tuple[Candidate, ...] = (
    Candidate("openai/gpt-5-mini", Tier.FAST_REASONING, DEV, latency_risk=True),
)

PROMPT_VERSION = "v4"  # the current drop pin (D34); hold prompt, vary model.
CADENCE_SECONDS = 30.0

FIXTURES = REPO_ROOT / ".artisan-fixtures"
ALL_ROASTS: tuple[Path, ...] = tuple(sorted(FIXTURES.glob("artisan-*/roast.jsonl")))

OUT_JSON = REPO_ROOT / "docs" / "advisor" / "bakeoff-results-2026-06-16.json"
OUT_MD = REPO_ROOT / "docs" / "advisor" / "bakeoff-results-2026-06-16.md"


async def main() -> int:
    """Run both passes on the Artisan set, merge, and write the scorecard."""
    if not ALL_ROASTS:
        print("fixtures missing — run scripts/alog_to_fixture.py first", flush=True)
        return 1

    print(
        f"PASS 1 — main roster ({len(ROSTER_MAIN)} models) x {len(ALL_ROASTS)} roasts, "
        f"{PROMPT_VERSION}, reasoning=default",
        flush=True,
    )
    avail_main, cells_main = await run_replay_bakeoff(
        ROSTER_MAIN, ALL_ROASTS, [PROMPT_VERSION], None, CADENCE_SECONDS
    )
    print(
        f"PASS 2 — gpt-5-mini x {len(ALL_ROASTS)} roasts, {PROMPT_VERSION}, reasoning=low",
        flush=True,
    )
    avail_reason, cells_reason = await run_replay_bakeoff(
        ROSTER_REASONING, ALL_ROASTS, [PROMPT_VERSION], "low", CADENCE_SECONDS
    )

    report = (
        render_replay_report(cells_main, ALL_ROASTS)
        + "\n\n---\n\n## gpt-5-mini (reasoning=low)\n\n"
        + render_replay_report(cells_reason, ALL_ROASTS)
    )
    print("\n" + report, flush=True)

    OUT_JSON.write_text(
        json.dumps(
            {
                "mode": "replay",
                "test_set": "artisan-drop-lt-198",
                "prompt_versions": [PROMPT_VERSION],
                "cadence_seconds": CADENCE_SECONDS,
                "note": "D40.4 drop-decision model refresh; control-loop eval is separate (#277).",
                "passes": [
                    {
                        "name": "main-default-reasoning",
                        "roasts": [p.parent.name for p in ALL_ROASTS],
                        "availability": [dataclasses.asdict(a) for a in avail_main],
                        "cells": replay_cells_to_json(cells_main),
                    },
                    {
                        "name": "gpt-5-mini-reasoning-low",
                        "roasts": [p.parent.name for p in ALL_ROASTS],
                        "availability": [dataclasses.asdict(a) for a in avail_reason],
                        "cells": replay_cells_to_json(cells_reason),
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
