"""One-off runner: full replay bake-off over the 6-model decision set.

The ≤3 s FC-viable set + the incumbent opus-4.8 baseline (operator pick, 14 Jun).
Overrides the module ROSTER (which main() reads as a global) — no edit to the
committed harness.
"""

import asyncio
import sys

sys.path.insert(0, "/Users/sertanyamaner/git/roastpilot-agent/scripts")
sys.path.insert(0, "/Users/sertanyamaner/git/roastpilot-agent/src")

sys.argv = [
    "advisor_bakeoff.py",
    "--mode",
    "replay",
    "--prompt-version",
    "v2",
    "v3",
    "--out",
    "/tmp/bakeoff-final.json",
    "--report-md",
    "/tmp/bakeoff-final.md",
]

import advisor_bakeoff as b  # noqa: E402
from advisor_bakeoff import Candidate, Tier  # noqa: E402

from roastpilot_agent.models import RoastPhase  # noqa: E402

DEV = (RoastPhase.DEVELOPMENT,)
PRE = (RoastPhase.ROASTING_PRE_FIRST_CRACK,)

b.ROSTER = (
    # ≤3 s FC-viable (operator's >3 s exclusion)
    Candidate("google/gemini-3.1-flash-lite", Tier.CONTROL_CANDIDATE, DEV),
    Candidate("openai/gpt-5.4-nano", Tier.CONTROL_CANDIDATE, DEV),
    Candidate("openai/gpt-4.1-mini", Tier.CONTROL_CANDIDATE, DEV),
    Candidate("anthropic/claude-opus-4.8-fast", Tier.CONTROL_CANDIDATE, DEV),
    Candidate("meta-llama/llama-3.3-70b-instruct", Tier.CONTROL_CANDIDATE, PRE),
    # Incumbent quality baseline (kept despite 4.5 s — operator pick)
    Candidate("anthropic/claude-opus-4.8", Tier.BASELINE, b.PHASE_ORDER),
)

print("REPLAY ROSTER:", [c.slug for c in b.ROSTER], flush=True)
raise SystemExit(asyncio.run(b.main()))
