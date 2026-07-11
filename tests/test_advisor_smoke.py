"""Tests for the advisor smoke/bake-off context builder (#497).

``advisor_smoke.py`` is a manual/local harness (never run in CI, makes live
network calls when driven end-to-end) and lives outside the
``[tool.coverage] source = ["roastpilot_agent"]`` gate, so it carries no
pre-existing test module. This one targets only the pure, network-free,
importable piece — :func:`advisor_smoke.build_context` — which reconstructs an
:class:`~roastpilot_agent.advisor.AdvisorContext` from a recorded roast fixture
and must populate the #497 actuated-lever fields the same way the live
controller and the bake-off replay harness do.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from advisor_smoke import DEFAULT_FIXTURE, build_context  # noqa: E402


def test_build_context_carries_the_real_actuated_heat_fan() -> None:
    """#497: the context's actuated fields equal the source row's real levers —
    never null — and ``post_fc_loop_active`` stays False (this fixture predates
    the deterministic post-FC RoR-taper loop, #405/D88, still flag-off in
    production, so the recorded levers are advisor-driven, not taper-actuated)."""
    context, row = build_context(DEFAULT_FIXTURE, row_offset_seconds=60.0)
    assert context.current_heat_percent == int(row["heat_level_percent"])
    assert context.current_fan_percent == int(row["fan_level_percent"])
    assert context.post_fc_loop_active is False
