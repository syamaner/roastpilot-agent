"""Per-phase advisor bake-off driver (E8-S4 → #172/#173; plan §11.1 → D20).

Extends the D20 bake-off methodology — *operator-judged advice quality under a
hard latency gate*, no auto-pick — to the per-phase prompt + model selection
that #172 (the ``v3`` per-stage prompt) and #173 (``model_slug_by_phase``)
introduce. It runs the real :class:`~roastpilot_agent.advisor.PydanticAIAdvisor`
(D5 + D18) over a candidate roster across three grounded roast moments — a
**preheat**, a **charge / pre-first-crack**, and a **first-crack** context — and
emits a paste-able decision table the operator reads to pick (a) the default
prompt version and (b) the per-phase model for the ``model_slug_by_phase`` slot.

Pipeline (a run, in order):

1. **Availability sweep** — every roster slug is probed for reachability on
   OpenRouter (the #168 ``healthcheck`` mechanism). A slug that resolves is
   kept; a 404 / provider error is **dropped and reported** so the comparison
   contains no phantom winners. Several roster slugs look next-gen and may not
   exist yet.
2. **Per-phase sampling** — each surviving (model, phase) pair runs
   ``get_recommendation`` N=3 (D20) against the phase-appropriate grounded
   context, under the prompt version(s) requested (``--prompt-version v2 v3``
   compares both, since #172 is part of this bake-off).
3. **Measure + report** — latency (min / median / max), pass/fail the hard
   latency gate (≤ 10 s, and a tighter first-crack threshold — the #171
   unthrottled-cadence target), and the full advice text per cell, so the
   operator judges quality. The first-crack slot's ranking is **latency-
   weighted**. NO model is auto-selected — D20 is operator-judged.

**Manual / local only — spends real OpenRouter credits.** Reads
``OPENROUTER_API_KEY`` from the environment at run; the key never enters config
or the repo. Nothing here changes production config defaults: pinning the
winning prompt / per-phase model into ``config.py`` is a *separate* post-bake-off
PR with its own D-number.

Exact operator run command::

    OPENROUTER_API_KEY=sk-or-... \\
    python scripts/advisor_bakeoff.py --iterations 3 \\
        --prompt-version v2 v3 --out /tmp/bakeoff.json

(``--iterations 3`` is the D20 N=3; ``--prompt-version v2 v3`` compares the
#172 candidate prompt against today's default. Drop to a single version to
isolate one.)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))  # advisor_smoke
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from advisor_smoke import DEFAULT_FIXTURE, build_context  # noqa: E402

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
    AdvisorError,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import AdvisorConfig  # noqa: E402
from roastpilot_agent.models import AdvisorHealthStatus, RoastPhase  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1"

# The hard latency gate (D20): the controller's tick-aligned advisory budget.
GATE_SECONDS = 10.0
# The tighter first-crack threshold the operator weights the FC slot against.
# #171 makes the development (FC-onward) consult cadence *unthrottled* — the
# practical floor is advisor latency, serial. A model that takes ~10 s there
# means the agent re-advises only every ~10 s through the narrow FC→drop
# window; this bake-off treats <= 2.5 s as the FC-slot target (fast enough to
# re-advise several times across the window) and surfaces anything slower.
FC_GATE_SECONDS = 2.5
# Generous bound so over-budget advice is still captured and shown (the operator
# wants to see slow-but-good advice, not a bare timeout).
MEASURE_TIMEOUT = 90.0

ReasoningEffort = Literal["off", "minimal", "low", "medium", "high"]


class Tier(enum.Enum):
    """Candidate tier (#173 roster). Plain ``Enum`` per the repo's D15 idiom.

    The tier records *which phase* a candidate is primarily a candidate for and
    its latency profile — it informs the report layout, not an auto-pick. Every
    surviving candidate is still measured in every phase.
    """

    ULTRA_FLASH = "ultra-flash"
    SPEED_AND_POWER = "speed-and-power"
    FAST_REASONING = "fast-reasoning"
    INCUMBENT = "incumbent"


# Agent phases sampled, in roast order. The first-crack slot is the one #171
# leaves unthrottled, so the latency-weighted FC ranking keys off it.
PHASE_ORDER: tuple[RoastPhase, ...] = (
    RoastPhase.PREHEATING,
    RoastPhase.ROASTING_PRE_FIRST_CRACK,
    RoastPhase.DEVELOPMENT,
)


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One bake-off candidate model (#173 roster encoded as data).

    Attributes:
        slug: The OpenRouter model slug probed and run.
        tier: Which :class:`Tier` the candidate belongs to (informs the report
            and the phase it is primarily a candidate for).
        primary_phases: The roast phase(s) the candidate is primarily a
            candidate for. All survivors are measured in every phase regardless;
            this only flags the operator's main interest.
        latency_risk: ``True`` for the fast-reasoning tier — a brief ``<think>``
            before output adds latency, a poor fit for the FC slot's hard gate.
    """

    slug: str
    tier: Tier
    primary_phases: tuple[RoastPhase, ...]
    latency_risk: bool = False


# The #173 candidate roster (operator, 13 Jun), encoded as data. Ultra-Flash →
# FC/development; Speed & Power → charge/pre-FC; Fast-Reasoning → pre-FC, flagged
# latency-risk; plus the incumbent D20 winner as the comparison anchor. The
# availability sweep drops any slug that does not resolve on OpenRouter.
ROSTER: tuple[Candidate, ...] = (
    # Ultra-Flash tier (sub-500 ms TTFT focus → FC/development slot).
    Candidate("google/gemini-3.1-flash-lite", Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    Candidate("google/gemini-3.5-flash", Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    Candidate("deepseek/deepseek-v4-flash", Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    Candidate("openai/gpt-4.1-mini", Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    Candidate("openai/gpt-5.4-nano", Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    # Speed & Power tier (high logic, 500 ms-1 s → charge / pre-FC slot).
    Candidate(
        "meta-llama/llama-3.3-70b-instruct",
        Tier.SPEED_AND_POWER,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
    ),
    Candidate(
        "qwen/qwen3.5-35b-instruct",
        Tier.SPEED_AND_POWER,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
    ),
    Candidate(
        "qwen/qwen3.5-coder",
        Tier.SPEED_AND_POWER,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
    ),
    Candidate(
        "nvidia/nemotron-3-ultra-550b-a55b",
        Tier.SPEED_AND_POWER,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
    ),
    # Fast-Reasoning tier (brief <think> before output → pre-FC, latency-risk).
    Candidate(
        "deepseek/deepseek-r1-distill-qwen-32b",
        Tier.FAST_REASONING,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
        latency_risk=True,
    ),
    Candidate(
        "microsoft/phi-4",
        Tier.FAST_REASONING,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
        latency_risk=True,
    ),
    # Incumbent baseline (D20 winner, current default in the #173 slot).
    Candidate(
        "anthropic/claude-opus-4.8",
        Tier.INCUMBENT,
        PHASE_ORDER,
    ),
)


def _make_config(
    slug: str, prompt_version: str, reasoning: ReasoningEffort | None
) -> AdvisorConfig:
    """Build an OpenRouter-backed :class:`AdvisorConfig` for one candidate slug.

    The provider is always ``openai_compatible`` against OpenRouter (D18); only
    the model slug, prompt version, and reasoning effort vary per cell. This
    only *drives* the production advisor — it never mutates production defaults.

    Args:
        slug: The candidate model slug.
        prompt_version: The advisor prompt version (``v2`` / ``v3``).
        reasoning: Reasoning effort for the OpenAI-compatible path, or ``None``
            for the provider default.

    Returns:
        A configuration pinning every phase to ``slug`` (so the phase the
        context carries does not re-route the model under test).
    """
    return AdvisorConfig(
        provider="openai_compatible",
        provider_base_url=OPENROUTER,
        api_key_env="OPENROUTER_API_KEY",
        model_slug=slug,
        model_slug_by_phase={phase: slug for phase in PHASE_ORDER},
        prompt_version=prompt_version,
        reasoning_effort=reasoning,
    )


# --- Per-phase grounded contexts -------------------------------------------


def build_phase_context(fixture: Path, phase: RoastPhase) -> tuple[AdvisorContext, float]:
    """Build a grounded :class:`AdvisorContext` for ``phase`` from a real roast.

    Reuses :func:`advisor_smoke.build_context` (the development/FC builder) and
    derives the preheat and charge / pre-first-crack contexts from the same live
    roast by re-stamping the phase and choosing realistic temps / RoR /
    development for each window. All temperatures are Celsius (invariant).

    - ``PREHEATING``: a drum warming toward the charge band, no beans, no FC.
    - ``ROASTING_PRE_FIRST_CRACK``: mid-roast (a point between charge and FC),
      RoR still positive, FC not yet detected, ``should_drop`` not in play.
    - ``DEVELOPMENT``: the existing grounded FC/development moment (~45 s after
      first crack), FC detected.

    Args:
        fixture: The live-roast ``roast.jsonl`` to ground the context in.
        phase: The roast phase to build a context for.

    Returns:
        The context plus the source-row monotonic timestamp (for the report).
    """
    if phase is RoastPhase.DEVELOPMENT:
        context, source_row = build_context(fixture, row_offset_seconds=45.0)
        return context, float(source_row["monotonic_seconds"])
    if phase is RoastPhase.ROASTING_PRE_FIRST_CRACK:
        return _pre_fc_context(fixture)
    if phase is RoastPhase.PREHEATING:
        return _preheat_context(fixture)
    raise ValueError(f"no bake-off context defined for phase {phase!r}")  # pragma: no cover


def _pre_fc_context(fixture: Path) -> tuple[AdvisorContext, float]:
    """Build a charge / pre-first-crack context from the live roast.

    Targets a row roughly midway between charge (``beans_added``) and first
    crack — RoR still declining toward FC, beans charged, FC not yet detected.

    Args:
        fixture: The live-roast ``roast.jsonl`` to ground the context in.

    Returns:
        The context plus the source-row monotonic timestamp.
    """
    telemetry, events = _load(fixture)
    t0 = events["beans_added"]
    fc = events["first_crack_detected"]
    drop = events["beans_dropped"]
    target = t0 + (fc - t0) * 0.5
    pre_rows = [
        (i, r) for i, r in enumerate(telemetry) if t0 <= float(r["monotonic_seconds"]) <= fc
    ]
    if not pre_rows:
        raise ValueError(
            f"fixture {fixture} has no telemetry rows between charge (T0={t0:.1f}s) "
            f"and first crack ({fc:.1f}s) — pre-first-crack context unavailable"
        )
    index, row = min(pre_rows, key=lambda ir: abs(float(ir[1]["monotonic_seconds"]) - target))
    mono = float(row["monotonic_seconds"])
    drop_row = min(telemetry, key=lambda r: abs(float(r["monotonic_seconds"]) - drop))
    context = AdvisorContext(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        roast_elapsed_seconds=round(mono - t0, 3),
        development_elapsed_seconds=None,
        current_bean_temp_c=float(row["bean_temp_c"]),
        current_env_temp_c=float(row["env_temp_c"]),
        bean_ror_c_per_min=_ror(telemetry, index, "bean_temp_c"),
        env_ror_c_per_min=_ror(telemetry, index, "env_temp_c"),
        target_drop_temp_c=float(drop_row["bean_temp_c"]),
        target_development_percent=20.0,
        profile_name=f"{fixture.parent.parent.name}/{fixture.parent.name}",
        recent_telemetry_samples=_recent_samples(telemetry, index),
        first_crack_detected=False,
        first_crack_timestamp_seconds=None,
    )
    return context, mono


def _preheat_context(fixture: Path) -> tuple[AdvisorContext, float]:
    """Build a preheat context from the live roast's earliest warming rows.

    No beans charged, the drum warming toward the charge band; FC not detected,
    ``should_drop`` not in play. The charge guidance band is supplied so the
    ``v3`` preheat section has its explicit target.

    Args:
        fixture: The live-roast ``roast.jsonl`` to ground the context in.

    Returns:
        The context plus the source-row monotonic timestamp.
    """
    telemetry, events = _load(fixture)
    t0 = events["beans_added"]
    pre_rows = [(i, r) for i, r in enumerate(telemetry) if float(r["monotonic_seconds"]) < t0]
    if not pre_rows:
        raise ValueError(
            f"fixture {fixture} has no telemetry rows before charge (T0={t0:.1f}s) — "
            f"preheat context unavailable"
        )
    # A row a little before charge: the drum has warmed and is approaching the
    # band, which is the realistic moment the advisor is consulted in preheat.
    index, row = pre_rows[len(pre_rows) // 2]
    mono = float(row["monotonic_seconds"])
    context = AdvisorContext(
        phase=RoastPhase.PREHEATING,
        roast_elapsed_seconds=round(mono, 3),
        development_elapsed_seconds=None,
        current_bean_temp_c=float(row["bean_temp_c"]),
        current_env_temp_c=float(row["env_temp_c"]),
        bean_ror_c_per_min=_ror(telemetry, index, "bean_temp_c"),
        env_ror_c_per_min=_ror(telemetry, index, "env_temp_c"),
        target_drop_temp_c=210.0,
        charge_guidance_min_c=180.0,
        charge_guidance_max_c=200.0,
        profile_name=f"{fixture.parent.parent.name}/{fixture.parent.name}",
        recent_telemetry_samples=_recent_samples(telemetry, index),
        first_crack_detected=False,
        first_crack_timestamp_seconds=None,
    )
    return context, mono


def _load(fixture: Path) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Return (telemetry rows, {event kind -> monotonic_seconds}) for a roast."""
    telemetry: list[dict[str, Any]] = []
    events: dict[str, float] = {}
    for line in fixture.read_text().splitlines():
        if not line.strip():
            continue
        row = cast("dict[str, Any]", json.loads(line))
        if row.get("type") == "telemetry":
            telemetry.append(row)
        elif row.get("type") == "event":
            events[str(row["kind"])] = float(row["monotonic_seconds"])
    return telemetry, events


_ROR_WINDOW_SECONDS = 60.0
_RECENT_SAMPLES = 6


def _ror(rows: list[dict[str, Any]], index: int, field: str) -> float | None:
    """Estimate °C/min for ``field`` at ``rows[index]`` over the prior ~60 s."""
    now = rows[index]
    now_t = float(now["monotonic_seconds"])
    for past in reversed(rows[:index]):
        dt = now_t - float(past["monotonic_seconds"])
        if dt >= _ROR_WINDOW_SECONDS:
            return round((float(now[field]) - float(past[field])) / dt * 60.0, 3)
    return None


def _recent_samples(rows: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """Return the recent telemetry window the advisor sees as context."""
    recent = rows[max(0, index - _RECENT_SAMPLES + 1) : index + 1]
    return [
        {
            "monotonic_seconds": float(r["monotonic_seconds"]),
            "bean_temp_c": float(r["bean_temp_c"]),
            "env_temp_c": float(r["env_temp_c"]),
            "heat_level_percent": int(r["heat_level_percent"]),
            "fan_level_percent": int(r["fan_level_percent"]),
        }
        for r in recent
    ]


# --- Availability sweep -----------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AvailabilityResult:
    """Outcome of probing one candidate slug for reachability.

    Attributes:
        slug: The probed model slug.
        available: ``True`` if the slug resolved (kept), ``False`` if it was
            dropped (404 / provider error).
        error: The captured error message when dropped, else ``None``.
    """

    slug: str
    available: bool
    error: str | None = None


async def probe_slug(
    slug: str, prompt_version: str, reasoning: ReasoningEffort | None
) -> AvailabilityResult:
    """Probe one slug's reachability on OpenRouter via the #168 healthcheck.

    Reuses :meth:`PydanticAIAdvisor.healthcheck` — a cheap, bounded, never-
    raising completion that decides reachability by the transport (a 404 model,
    a 401/402 key, an unreachable endpoint), not the content. A ``REACHABLE``
    result keeps the slug; ``UNREACHABLE`` drops it and carries the error.

    Args:
        slug: The candidate model slug to probe.
        prompt_version: The prompt version (so the probe builds the same agent
            shape the run uses; immaterial to reachability).
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        The :class:`AvailabilityResult` for ``slug``.
    """
    advisor = PydanticAIAdvisor(_make_config(slug, prompt_version, reasoning))
    health = await advisor.healthcheck()
    if health.status is AdvisorHealthStatus.REACHABLE:
        return AvailabilityResult(slug=slug, available=True)
    return AvailabilityResult(slug=slug, available=False, error=health.error)


async def availability_sweep(
    roster: tuple[Candidate, ...], prompt_version: str, reasoning: ReasoningEffort | None
) -> tuple[list[Candidate], list[AvailabilityResult]]:
    """Probe every roster slug; return survivors plus all probe results.

    The first step of a run. A slug that resolves is kept; a 404 / error is
    dropped (no phantom winners). The dropped slugs and their errors are
    returned so the caller can report them.

    Args:
        roster: The candidate roster to sweep.
        prompt_version: The prompt version used to build probe agents.
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        ``(survivors, results)`` — the kept candidates in roster order, and the
        per-slug :class:`AvailabilityResult` list (kept and dropped).
    """
    # Probe concurrently — each healthcheck is an independent network round trip,
    # so a serial loop would make the operator block ~1-3 s per slug before any
    # sampling starts. ``gather`` preserves roster order in its return value.
    results = list(
        await asyncio.gather(*(probe_slug(cand.slug, prompt_version, reasoning) for cand in roster))
    )
    by_slug = {r.slug: r for r in results}
    survivors = [c for c in roster if by_slug[c.slug].available]
    return survivors, results


# --- Per-phase measurement --------------------------------------------------


@dataclasses.dataclass
class CellResult:
    """Measured result for one (model, phase, prompt) cell.

    Attributes:
        slug: The model slug measured.
        tier: The candidate's tier value (for the report).
        phase: The roast phase value the cell measured.
        prompt_version: The prompt version used.
        latency_risk: Whether the candidate is flagged latency-risk.
        ok_count: How many of the N iterations returned a valid decision.
        latency_min/median/max: Latency in seconds over the ok iterations
            (``None`` if none succeeded).
        passes_gate: Whether the median latency is within the 10 s hard gate.
        passes_fc_gate: Whether the median latency is within the tighter FC
            threshold (the metric the FC slot is ranked on).
        decision: The first valid :class:`RoastDecision` (the advice the
            operator judges), or ``None`` if every iteration failed.
        error: The first iteration error message when no decision was produced.
    """

    slug: str
    tier: str
    phase: str
    prompt_version: str
    latency_risk: bool
    ok_count: int
    latency_min: float | None
    latency_median: float | None
    latency_max: float | None
    passes_gate: bool
    passes_fc_gate: bool
    decision: RoastDecision | None
    error: str | None = None


async def run_cell(
    cand: Candidate,
    phase: RoastPhase,
    context: AdvisorContext,
    iters: int,
    prompt_version: str,
    reasoning: ReasoningEffort | None,
) -> CellResult:
    """Run one (model, phase, prompt) cell N times and summarize it.

    Calls the real :meth:`PydanticAIAdvisor.get_recommendation` ``iters`` times
    against ``context`` and records latency and the returned advice. The advisor
    pins every phase to ``cand.slug``, so the cell measures that one model.

    Args:
        cand: The candidate model.
        phase: The roast phase the context represents.
        context: The grounded context to advise on.
        iters: Iterations per cell (D20 N=3).
        prompt_version: The prompt version under test.
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        The :class:`CellResult` summary.
    """
    advisor = PydanticAIAdvisor(_make_config(cand.slug, prompt_version, reasoning))
    latencies: list[float] = []
    decision: RoastDecision | None = None
    error: str | None = None
    for _ in range(iters):
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                advisor.get_recommendation(context), timeout=MEASURE_TIMEOUT
            )
        except (AdvisorError, TimeoutError) as exc:
            if error is None:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        latencies.append(time.perf_counter() - started)
        if decision is None:
            decision = result
    return summarize_cell(cand, phase, prompt_version, latencies, decision, error)


def summarize_cell(
    cand: Candidate,
    phase: RoastPhase,
    prompt_version: str,
    latencies: list[float],
    decision: RoastDecision | None,
    error: str | None,
) -> CellResult:
    """Reduce raw per-iteration latencies + advice into a :class:`CellResult`.

    Pure (no I/O) so the report layer is testable from synthetic inputs.

    Args:
        cand: The candidate model.
        phase: The roast phase measured.
        prompt_version: The prompt version under test.
        latencies: Per-iteration latencies (seconds) for the ok iterations.
        decision: The first valid decision, or ``None``.
        error: The first error message when no decision was produced.

    Returns:
        The summarized :class:`CellResult`.
    """
    median = round(statistics.median(latencies), 2) if latencies else None
    return CellResult(
        slug=cand.slug,
        tier=cand.tier.value,
        phase=phase.value,
        prompt_version=prompt_version,
        latency_risk=cand.latency_risk,
        ok_count=len(latencies),
        latency_min=round(min(latencies), 2) if latencies else None,
        latency_median=median,
        latency_max=round(max(latencies), 2) if latencies else None,
        passes_gate=bool(median is not None and median <= GATE_SECONDS),
        passes_fc_gate=bool(median is not None and median <= FC_GATE_SECONDS),
        decision=decision,
        error=error,
    )


# --- Reporting (operator-judged; NO auto-pick) ------------------------------


def render_availability(results: list[AvailabilityResult]) -> str:
    """Render the availability sweep as a paste-able section.

    Args:
        results: All per-slug probe results (kept and dropped).

    Returns:
        A text block listing kept slugs and, separately, the dropped slugs with
        their errors (so the operator sees exactly what was excluded and why).
    """
    kept = [r.slug for r in results if r.available]
    dropped = [r for r in results if not r.available]
    lines = ["## Availability sweep (OpenRouter reachability)", ""]
    lines.append(f"kept ({len(kept)}): " + (", ".join(kept) if kept else "(none)"))
    if dropped:
        lines.append("")
        lines.append(f"DROPPED ({len(dropped)}) — excluded from the comparison:")
        for r in dropped:
            lines.append(f"  - {r.slug}: {r.error or 'unavailable'}")
    else:
        lines.append("dropped (0): all roster slugs resolved")
    return "\n".join(lines)


def _gate_label(cell: CellResult) -> str:
    """Return the gate verdict for a cell, FC-aware.

    Development cells are judged against the tighter FC threshold (and the
    10 s gate); other phases against the 10 s gate only.
    """
    if cell.latency_median is None:
        return "FAILED"
    if cell.phase == RoastPhase.DEVELOPMENT.value:
        if cell.passes_fc_gate:
            return f"FC-PASS (<={FC_GATE_SECONDS:g}s)"
        return "over-FC-gate" if cell.passes_gate else "OVER-10s"
    return "PASS" if cell.passes_gate else "OVER-10s"


def render_decision_table(cells: list[CellResult]) -> str:
    """Render the paste-able operator decision table (NO auto-pick — D20).

    Groups by prompt version then phase so the operator can read off (a) the
    prompt default (v2 vs v3, same phase, side by side) and (b) the per-phase
    model for the ``model_slug_by_phase`` slot. The first-crack (development)
    section is sorted latency-ascending — the FC slot's ranking is latency-
    weighted — and every cell carries the full advice text for quality
    judgement. No row is marked "winner": the operator picks.

    Args:
        cells: All measured cells across models, phases, and prompt versions.

    Returns:
        The full report text.
    """
    out: list[str] = []
    out.append("# Advisor bake-off — per-phase decision table (operator-judged, D20)")
    out.append("")
    out.append(
        f"Hard latency gate: median <= {GATE_SECONDS:g}s (all phases). "
        f"First-crack (development) target: median <= {FC_GATE_SECONDS:g}s "
        "(#171 unthrottled cadence — the FC slot is latency-weighted)."
    )
    out.append("NO model is auto-selected. Pick (a) the prompt default and (b) the per-phase")
    out.append("model from the latency + advice columns below.")

    prompt_versions = sorted({c.prompt_version for c in cells})
    for pv in prompt_versions:
        out.append("")
        out.append(f"## prompt_version = {pv}")
        for phase in PHASE_ORDER:
            phase_cells = [c for c in cells if c.prompt_version == pv and c.phase == phase.value]
            if not phase_cells:
                continue
            out.append("")
            label = phase.value
            if phase is RoastPhase.DEVELOPMENT:
                label += "  (FIRST CRACK / development — latency-weighted)"
            out.append(f"### phase = {label}")
            # FC slot ranks by latency ascending; other phases keep roster order
            # (capability-first) so the operator weighs quality, not just speed.
            ordered = (
                sorted(phase_cells, key=_latency_sort_key)
                if phase is RoastPhase.DEVELOPMENT
                else phase_cells
            )
            for c in ordered:
                out.append(_render_cell_row(c))
    return "\n".join(out)


def _latency_sort_key(cell: CellResult) -> float:
    """Sort key putting fastest cells first; failed cells (no median) last."""
    return cell.latency_median if cell.latency_median is not None else float("inf")


def _render_cell_row(cell: CellResult) -> str:
    """Render one cell as two lines: the metrics row, then the advice text."""
    risk = " [latency-risk]" if cell.latency_risk else ""
    lat = (
        f"min/med/max={cell.latency_min}/{cell.latency_median}/{cell.latency_max}s"
        if cell.latency_median is not None
        else "no successful call"
    )
    head = f"- {cell.slug} ({cell.tier}){risk}: {_gate_label(cell)} {lat} ok={cell.ok_count}"
    if cell.decision is not None:
        d = cell.decision
        advice = (
            f"    advice: heat={d.target_heat}% fan={d.target_fan}% "
            f"drop={d.should_drop} confidence={d.confidence} "
            f"rationale={d.rationale!r}"
        )
    else:
        advice = f"    advice: (none) error={cell.error}"
    return head + "\n" + advice


def cells_to_json(cells: list[CellResult]) -> list[dict[str, Any]]:
    """Serialize cells for the ``--out`` JSON artifact."""
    rows: list[dict[str, Any]] = []
    for c in cells:
        # Build the row by field rather than ``dataclasses.asdict`` — the latter
        # deepcopies the ``RoastDecision`` Pydantic field only to discard it; we
        # serialize the decision explicitly via ``model_dump``.
        row = {f.name: getattr(c, f.name) for f in dataclasses.fields(c) if f.name != "decision"}
        row["decision"] = c.decision.model_dump() if c.decision is not None else None
        rows.append(row)
    return rows


# --- Run loop ---------------------------------------------------------------


async def run_bakeoff(
    roster: tuple[Candidate, ...],
    fixture: Path,
    iters: int,
    prompt_versions: list[str],
    reasoning: ReasoningEffort | None,
) -> tuple[list[AvailabilityResult], list[CellResult]]:
    """Run the full pipeline: availability sweep, then per-phase sampling.

    The availability sweep runs once (with the first prompt version — probes are
    prompt-independent); only the survivors are sampled across every phase and
    prompt version.

    Args:
        roster: The candidate roster.
        fixture: The grounding live-roast fixture.
        iters: Iterations per cell (D20 N=3).
        prompt_versions: Prompt versions to compare (e.g. ``["v2", "v3"]``).
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        ``(availability_results, cells)``.
    """
    probe_pv = prompt_versions[0]
    survivors, availability = await availability_sweep(roster, probe_pv, reasoning)
    print(render_availability(availability), flush=True)
    print("", flush=True)

    contexts = {phase: build_phase_context(fixture, phase)[0] for phase in PHASE_ORDER}
    cells: list[CellResult] = []
    for pv in prompt_versions:
        for cand in survivors:
            for phase in PHASE_ORDER:
                print(f"running {cand.slug} @ {phase.value} (prompt {pv})…", flush=True)
                cell = await run_cell(cand, phase, contexts[phase], iters, pv, reasoning)
                cells.append(cell)
                print(f"  {_gate_label(cell)} median={cell.latency_median}s", flush=True)
    return availability, cells


async def main() -> int:
    """CLI entrypoint — runs the bake-off and prints the decision table.

    Returns:
        ``0`` on a completed run (the report is operator-judged; a completed run
        is success regardless of which models passed the gate).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--iterations", type=int, default=3, help="iterations per cell (D20 N=3)")
    parser.add_argument(
        "--prompt-version",
        nargs="+",
        default=["v2", "v3"],
        help="prompt version(s) to compare (default: v2 v3 — #172)",
    )
    parser.add_argument(
        "--reasoning",
        default="default",
        choices=["default", "off", "minimal", "low", "medium", "high"],
        help="reasoning effort for the OpenAI-compatible path (default: provider default)",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--out", type=Path, default=Path("/tmp/bakeoff.json"))
    args = parser.parse_args()

    reasoning: ReasoningEffort | None = (
        None if args.reasoning == "default" else cast("ReasoningEffort", args.reasoning)
    )
    prompt_versions = cast("list[str]", args.prompt_version)
    availability, cells = await run_bakeoff(
        ROSTER, args.fixture, args.iterations, prompt_versions, reasoning
    )

    report = render_decision_table(cells)
    print("\n" + report, flush=True)
    artifact = {
        "availability": [dataclasses.asdict(a) for a in availability],
        "cells": cells_to_json(cells),
    }
    args.out.write_text(json.dumps(artifact, indent=2))
    print(f"\nwrote artifact -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
