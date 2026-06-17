"""Per-phase advisor bake-off driver (E8-S4 → #172/#173; plan §11.1 → D20).

Extends the D20 bake-off methodology — *operator-judged advice quality under a
hard latency gate*, no auto-pick — to the per-phase prompt + model selection
that #172 (the ``v3`` per-stage prompt) and #173 (``model_slug_by_phase``)
introduce. It runs the real :class:`~roastpilot_agent.advisor.PydanticAIAdvisor`
(D5 + D18) over a candidate roster and emits a paste-able report the operator
reads to pick (a) the default prompt version and (b) the per-phase model for the
``model_slug_by_phase`` slot. The default ``replay`` mode scores recommendations
quantitatively against the two known-good 7-Jun roasts; the ``per-phase`` mode is
a lighter latency/advice table over three grounded synthetic moments.

Two modes; both start with the availability sweep and never auto-pick (D20):

1. **Availability sweep** (always first) — every roster slug is probed for
   reachability on OpenRouter (the #168 ``healthcheck`` mechanism). A slug that
   resolves is kept; a 404 / provider error is **dropped and reported** so the
   comparison contains no phantom winners. Several roster slugs look next-gen and
   may not exist yet.
2. **``--mode replay`` (default) — quantitative real-roast scoring.** Replay each
   of the two known-good 7-Jun Hottop roasts tick-by-tick, reconstruct the
   ``AdvisorContext`` at each decision tick, run each surviving (model, prompt)
   over it, and **score the recommendations against what the real good roast
   did**: drop F1 / precision / recall + drop-timing error (s and °C), heat/fan
   MAE + directional agreement, and per-phase latency. See ``bakeoff_replay`` for
   the honest-framing caveat — these measure *agreement with a known-good roast*,
   NOT correctness.
3. **``--mode per-phase`` — synthetic-moment latency/advice table.** The lighter
   pass: each surviving (model, phase) runs ``get_recommendation`` N=3 (D20)
   against one grounded preheat / pre-FC / first-crack moment under each prompt,
   reporting latency (min/median/max) vs the hard gate (≤ 10 s, tighter at FC)
   and the advice text. The FC slot is latency-weighted.

Both compare prompt **v2 vs v3** by default (#172 is part of this bake-off) and
emit a paste-able operator report + a JSON artifact.

**Manual / local only — spends real OpenRouter credits.** Reads
``OPENROUTER_API_KEY`` from the environment at run; the key never enters config
or the repo. Nothing here changes production config defaults: pinning the
winning prompt / per-phase model into ``config.py`` is a *separate* post-bake-off
PR with its own D-number.

Exact operator run commands::

    # Quantitative scorecard vs the two known-good roasts (default mode):
    OPENROUTER_API_KEY=sk-or-... \\
    python scripts/advisor_bakeoff.py --prompt-version v2 v3 \\
        --out /tmp/bakeoff.json --report-md /tmp/bakeoff.md

    # Lighter per-phase latency/advice table:
    OPENROUTER_API_KEY=sk-or-... \\
    python scripts/advisor_bakeoff.py --mode per-phase --iterations 3 \\
        --prompt-version v2 v3 --out /tmp/bakeoff-perphase.json

**Long-run observability + recovery (replay mode, #280).** A replay run is
expensive and slow (a model call per tick, per model, per prompt, per roast), so
it prints a per-cell progress line as each ``(model, prompt, roast)`` cell
completes plus a periodic cumulative-cost heartbeat, persists every completed
cell to a ``<out>.cells.jsonl`` sidecar **immediately**, and on re-run RESUMES by
skipping cells already on disk (``--no-resume`` to force a clean run). An optional
``--max-spend`` budget stops the run GRACEFULLY before it would breach the cap —
flushing partials, rendering the partial scorecard, and exiting cleanly. Spend is
estimated as ``calls x --cost-per-call`` (pydantic_ai exposes token usage, not a
billed dollar amount). A kill / cap / crash therefore loses at most the in-flight
cell, and the final scorecard always renders from the accumulated cells::

    OPENROUTER_API_KEY=sk-or-... \\
    python scripts/advisor_bakeoff.py --out /tmp/bakeoff.json \\
        --max-spend 20 --report-md /tmp/bakeoff.md
    # killed / capped? just re-run the SAME command — it resumes.

The replay machinery (replay + metrics + report + checkpoint + cost guard) is
testable WITHOUT a key via a canned recommender; only the real-candidate run
needs ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import json
import random
import re
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))  # advisor_smoke
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from advisor_smoke import DEFAULT_FIXTURE, build_context  # noqa: E402
from bakeoff_replay import (  # noqa: E402
    GroundTruth,
    PhaseLatency,
    ReplayTick,
    RoastScore,
    TickOutcome,
    build_ticks,
    render_drop_confusion_md,
    render_heat_direction_confusion_md,
    replay_roast,
    score_roast,
    score_to_json,
)
from trajectory_scorer import (  # noqa: E402
    TrajectorySanity,
    render_trajectory_report,
    score_trajectory,
    trajectory_to_json,
)

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
    AdvisorError,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import AdvisorConfig  # noqa: E402
from roastpilot_agent.models import AdvisorHealthStatus, RoastPhase  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1"

# The two known-good 7-Jun Hottop roasts used as the replay test set. Both are
# GOOD roasts (operator ground truth), NOT provably optimal — the scoring
# measures agreement with a known-good roast, not absolute correctness.
REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_ROASTS: tuple[Path, ...] = (
    REPO_ROOT / "tests" / "fixtures" / "live-roast-2026-06-07" / "session-1" / "roast.jsonl",
    REPO_ROOT / "tests" / "fixtures" / "live-roast-2026-06-07" / "session-2" / "roast.jsonl",
)
# Roast-time spacing between scored decision ticks in a replay (a real run
# spends a model call per tick per model per prompt per roast).
DEFAULT_CADENCE_SECONDS = 30.0

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
    PRIOR_FRONTIER = "prior-frontier"


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
    # Frontier-fast option: frontier quality at flash-ish speed — a prime FC-slot
    # candidate (operator-approved bonus).
    Candidate("anthropic/claude-opus-4.8-fast", Tier.ULTRA_FLASH, (RoastPhase.DEVELOPMENT,)),
    # Speed & Power tier (high logic, 500 ms-1 s → charge / pre-FC slot).
    Candidate(
        "meta-llama/llama-3.3-70b-instruct",
        Tier.SPEED_AND_POWER,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
    ),
    Candidate(
        "qwen/qwen3.5-35b-a3b",
        Tier.SPEED_AND_POWER,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
    ),
    Candidate(
        "qwen/qwen3-coder",
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
        "deepseek/deepseek-r1",
        Tier.FAST_REASONING,
        (RoastPhase.ROASTING_PRE_FIRST_CRACK,),
        latency_risk=True,
    ),
    Candidate(
        "openai/o4-mini",
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
    # Prior-frontier baselines — the rest of the D20 slate, measured in every
    # phase (``PHASE_ORDER``) like the incumbent. These are the quality floor a
    # new ultra-flash candidate must match or beat: if a sub-500 ms model cannot
    # roast at least as well as these established frontier models did, the speed
    # is not worth the quality regression.
    Candidate(
        "anthropic/claude-sonnet-4.6",
        Tier.PRIOR_FRONTIER,
        PHASE_ORDER,
    ),
    Candidate(
        "openai/gpt-5.5",
        Tier.PRIOR_FRONTIER,
        PHASE_ORDER,
    ),
    Candidate(
        "anthropic/claude-haiku-4.5",
        Tier.PRIOR_FRONTIER,
        PHASE_ORDER,
    ),
    # gpt-5-mini reasons before answering (~12-16 s) — included deliberately as
    # the "quality competes but the latency fails the FC gate" case, so the
    # operator can see a strong-advice / over-gate trade-off explicitly.
    Candidate(
        "openai/gpt-5-mini",
        Tier.PRIOR_FRONTIER,
        PHASE_ORDER,
        latency_risk=True,
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


# Probe retry policy. A keyed re-probe showed the sweep had *transiently*
# false-dropped a valid model (``meta-llama/llama-3.3-70b-instruct`` came back
# reachable on a clean re-run). A single network blip must not exclude a real
# candidate, so the sweep retries an UNREACHABLE probe a couple of times with a
# short backoff before declaring the slug unavailable. A genuine 400/404 simply
# fails again on each attempt — cheap and idempotent — and is still dropped.
_PROBE_ATTEMPTS = 2
_PROBE_BACKOFF_SECONDS = 1.0


# Drop-reason classes for the report. The healthcheck error message already
# distinguishes "does not exist" (400 invalid model ID) from "exists but lacks
# the structured-output / tool-use the advisor requires" (404 no tool-use
# endpoints) — the latter is a real candidate filter, separate from latency,
# and it knocks out the whole fast-reasoning tier. ``transient`` is reserved for
# a probe that failed every retry without a recognised provider verdict.
DROP_REASON_INVALID_MODEL = "invalid-model-id"
DROP_REASON_NO_TOOL_USE = "no-tool-use-endpoint"
DROP_REASON_AUTH = "auth"
DROP_REASON_OTHER = "other-or-transient"


def classify_drop_reason(error: str | None) -> str:
    """Classify a dropped slug's healthcheck error into a report reason.

    Reads the provider error text the #168 healthcheck captured — the transport
    verdict, not the slug — so the operator sees *why* a candidate was excluded:
    a non-existent model (400 invalid ID), a model that exists but lacks the
    tool-use / structured-output the advisor requires (404 no endpoints), an
    auth problem, or an unrecognised / transient failure.

    Args:
        error: The captured healthcheck error message, or ``None``.

    Returns:
        One of the ``DROP_REASON_*`` constants.
    """
    text = (error or "").lower()
    if "tool use" in text or "tool-use" in text or "no endpoints" in text:
        return DROP_REASON_NO_TOOL_USE
    if "not a valid model" in text or "invalid model" in text:
        return DROP_REASON_INVALID_MODEL
    if "400" in text and "model" in text:
        return DROP_REASON_INVALID_MODEL
    if "401" in text or "402" in text or "auth" in text or "api key" in text:
        return DROP_REASON_AUTH
    return DROP_REASON_OTHER


@dataclasses.dataclass(frozen=True)
class AvailabilityResult:
    """Outcome of probing one candidate slug for reachability.

    Attributes:
        slug: The probed model slug.
        available: ``True`` if the slug resolved (kept), ``False`` if it was
            dropped (400/404 / provider error after all retries).
        error: The captured error message when dropped, else ``None``.
        attempts: How many probe attempts were made (>= 2 only when an earlier
            attempt failed and was retried — surfaces a recovered transient).
        reason: The :func:`classify_drop_reason` class when dropped, else
            ``None``; tells the operator WHY the slug was excluded.
    """

    slug: str
    available: bool
    error: str | None = None
    attempts: int = 1
    reason: str | None = None


async def probe_slug(
    slug: str,
    prompt_version: str,
    reasoning: ReasoningEffort | None,
    *,
    attempts: int = _PROBE_ATTEMPTS,
    backoff_seconds: float = _PROBE_BACKOFF_SECONDS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> AvailabilityResult:
    """Probe one slug's reachability on OpenRouter via the #168 healthcheck.

    Reuses :meth:`PydanticAIAdvisor.healthcheck` — a cheap, bounded, never-
    raising completion that decides reachability by the transport (a 404 model,
    a 401/402 key, an unreachable endpoint), not the content. A ``REACHABLE``
    result keeps the slug; an ``UNREACHABLE`` result is **retried** up to
    ``attempts`` times with ``backoff_seconds`` between tries, so a transient
    blip does not false-drop a valid candidate (a keyed re-probe caught exactly
    that). Only a slug that fails every attempt is dropped, carrying the last
    error and a classified :func:`classify_drop_reason`.

    Args:
        slug: The candidate model slug to probe.
        prompt_version: The prompt version (so the probe builds the same agent
            shape the run uses; immaterial to reachability).
        reasoning: Reasoning effort, or ``None`` for the provider default.
        attempts: Maximum probe attempts before declaring the slug unavailable.
        backoff_seconds: Delay between attempts.
        sleep: Async sleep used between attempts; defaults to
            :func:`asyncio.sleep` (injectable so tests stay instant).

    Returns:
        The :class:`AvailabilityResult` for ``slug``.
    """
    do_sleep = sleep if sleep is not None else asyncio.sleep
    advisor = PydanticAIAdvisor(_make_config(slug, prompt_version, reasoning))
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        health = await advisor.healthcheck()
        if health.status is AdvisorHealthStatus.REACHABLE:
            return AvailabilityResult(slug=slug, available=True, attempts=attempt)
        last_error = health.error
        if attempt < attempts:
            await do_sleep(backoff_seconds)
    return AvailabilityResult(
        slug=slug,
        available=False,
        error=last_error,
        attempts=attempts,
        reason=classify_drop_reason(last_error),
    )


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
        A text block listing kept slugs (flagging any that needed a retry) and,
        separately, the dropped slugs with their classified drop reason + error
        (so the operator sees exactly what was excluded and WHY).
    """
    kept = [r for r in results if r.available]
    dropped = [r for r in results if not r.available]
    lines = ["## Availability sweep (OpenRouter reachability)", ""]
    kept_slugs = ", ".join(r.slug for r in kept) if kept else "(none)"
    lines.append(f"kept ({len(kept)}): {kept_slugs}")
    # Surface a recovered transient: a slug that only resolved after a retry.
    recovered = [r for r in kept if r.attempts > 1]
    for r in recovered:
        lines.append(
            f"  - NOTE {r.slug} resolved only on attempt {r.attempts} "
            "(transient first failure — the retry saved a valid candidate)"
        )
    if dropped:
        lines.append("")
        lines.append(f"DROPPED ({len(dropped)}) — excluded from the comparison, with reason:")
        for r in dropped:
            # The DROP_REASON_* constants are themselves the human-readable
            # labels, so the reason is shown directly (no label lookup needed).
            label = r.reason or DROP_REASON_OTHER
            lines.append(
                f"  - {r.slug} [{label}] (after {r.attempts} attempt"
                f"{'s' if r.attempts != 1 else ''}): {r.error or 'unavailable'}"
            )
        if any(r.reason == DROP_REASON_NO_TOOL_USE for r in dropped):
            lines.append("")
            lines.append(
                "  Tool-use requirement: a model tagged [no-tool-use-endpoint] EXISTS but "
                "lacks the structured-output / tool-use the advisor requires — a real "
                "candidate filter distinct from latency, and it knocks out the whole "
                "fast-reasoning tier here."
            )
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


# --- Real-roast replay scoring (the quantitative layer) ---------------------

# Roast moments to surface advice samples for in the report — the operator reads
# the model's actual recommendation at each, since the metrics are agreement-
# only (see the honest-framing note). Labels map to a fraction between the
# anchoring events; the nearest scored tick is shown.
_SAMPLE_MOMENTS: tuple[str, ...] = ("charge", "maillard", "first-crack", "development")


@dataclasses.dataclass
class ReplayCell:
    """One (model, prompt) candidate scored across all replay roasts.

    Attributes:
        slug: The model slug.
        tier: The candidate's tier value.
        prompt_version: The prompt version under test.
        latency_risk: Whether the candidate is flagged latency-risk.
        scores: Per-roast scorecards.
        samples: Per-roast advice samples at the key moments (for operator
            quality judgement, since the metrics are agreement-only).
        trajectories: Per-roast control-trajectory sanity scorecards (#277) —
            the agreement-FREE command-signal coherence view (change/reversal
            counts, control-signal entropy, momentum cuts). Ordered to match
            ``scores`` (one per replay roast). Computed alongside the agreement
            metrics; reported behind ``--trajectory``.
    """

    slug: str
    tier: str
    prompt_version: str
    latency_risk: bool
    scores: list[RoastScore]
    samples: dict[str, list[tuple[str, TickOutcome]]]
    # Defaulted so existing constructors (and the report-only tests that build a
    # cell by hand) stay valid; the replay path always populates it. The typed
    # factory keeps pyright strict from inferring ``list[Unknown]``.
    trajectories: list[TrajectorySanity] = dataclasses.field(
        default_factory=lambda: cast("list[TrajectorySanity]", [])
    )


def _sample_outcomes(
    outcomes: list[TickOutcome], ground: GroundTruth
) -> list[tuple[str, TickOutcome]]:
    """Pick the advice-sample ticks nearest each key roast moment."""
    targets = {
        "charge": ground.t0_seconds + 5.0,
        "maillard": ground.t0_seconds + (ground.first_crack_seconds - ground.t0_seconds) * 0.6,
        "first-crack": ground.first_crack_seconds + 5.0,
        "development": ground.first_crack_seconds
        + (ground.drop_seconds - ground.first_crack_seconds) * 0.6,
    }
    ok = [o for o in outcomes if o.decision is not None]
    picks: list[tuple[str, TickOutcome]] = []
    for label in _SAMPLE_MOMENTS:
        target = targets[label]
        pool = ok or outcomes
        nearest = min(pool, key=lambda o: abs(o.tick.monotonic_seconds - target))
        picks.append((label, nearest))
    return picks


async def run_replay_cell(
    cand: Candidate,
    prompt_version: str,
    reasoning: ReasoningEffort | None,
    roasts: tuple[Path, ...],
    cadence_seconds: float,
) -> ReplayCell:
    """Replay every roast through ``cand`` under ``prompt_version`` and score it.

    Builds one advisor for the cell and runs its ``get_recommendation`` over
    each roast's reconstructed ticks, scoring drop / heat / fan / latency
    against the known-good roast and capturing advice samples at the key
    moments.

    Args:
        cand: The candidate model.
        prompt_version: The prompt version under test (``v2`` / ``v3``).
        reasoning: Reasoning effort, or ``None`` for the provider default.
        roasts: The replay roast fixtures.
        cadence_seconds: Roast-time spacing between scored ticks.

    Returns:
        The :class:`ReplayCell`.
    """
    advisor = PydanticAIAdvisor(_make_config(cand.slug, prompt_version, reasoning))

    async def recommend(context: AdvisorContext) -> RoastDecision:
        return await asyncio.wait_for(advisor.get_recommendation(context), timeout=MEASURE_TIMEOUT)

    return await score_candidate(cand, prompt_version, recommend, roasts, cadence_seconds)


def roast_id_for(fixture: Path) -> str:
    """Return the stable ``roast_id`` used to key a (model, prompt, roast) cell.

    The id is the ``<session-parent>/<session>`` name the scorecard already uses
    as the roast name, so the checkpoint key matches the reported roast.

    Args:
        fixture: A replay roast ``roast.jsonl`` fixture path.

    Returns:
        The roast id (e.g. ``live-roast-2026-06-07/session-1``).
    """
    return f"{fixture.parent.parent.name}/{fixture.parent.name}"


@dataclasses.dataclass
class RoastReplay:
    """One scored ``(model_slug, prompt_version, roast_id)`` replay cell.

    The atomic checkpoint unit (issue #280): the raw per-tick recommender
    outcomes for a single roast plus the identity that keys them. Scores,
    samples, and the trajectory view are derived from ``outcomes`` by the
    existing pure scorers — never stored — so a reload recomputes them
    identically and the scoring math is untouched.

    Attributes:
        slug: The model slug measured.
        prompt_version: The prompt version under test.
        roast_id: The roast the outcomes are for (see :func:`roast_id_for`).
        outcomes: The per-tick recommender outcomes (decision + latency), in
            tick order — the only model-call output worth persisting.
        ground: The roast's ground truth (rebuilt from the fixture; not stored).
        score: The derived :class:`RoastScore` for the cell.
        samples: The advice samples at the key roast moments.
        trajectory: The control-trajectory sanity scorecard (#277).
        call_count: Number of recommender calls this cell consumed (= ticks);
            the basis for the cost estimate when no provider cost is exposed.
    """

    slug: str
    prompt_version: str
    roast_id: str
    outcomes: list[TickOutcome]
    ground: GroundTruth
    score: RoastScore
    samples: list[tuple[str, TickOutcome]]
    trajectory: TrajectorySanity
    call_count: int


def build_roast_replay(
    slug: str,
    prompt_version: str,
    roast_id: str,
    outcomes: list[TickOutcome],
    ground: GroundTruth,
) -> RoastReplay:
    """Build a :class:`RoastReplay` by running the existing pure scorers.

    The single place the scorers are invoked, so a fresh run and a checkpoint
    reload produce identical derived metrics from the same outcomes.

    Args:
        slug: The model slug.
        prompt_version: The prompt version under test.
        roast_id: The roast id the outcomes are for.
        outcomes: The per-tick recommender outcomes (fresh or reloaded).
        ground: The roast's ground truth.

    Returns:
        The fully-derived :class:`RoastReplay`.
    """
    return RoastReplay(
        slug=slug,
        prompt_version=prompt_version,
        roast_id=roast_id,
        outcomes=outcomes,
        ground=ground,
        score=score_roast(outcomes, ground, roast_id),
        samples=_sample_outcomes(outcomes, ground),
        # Control-trajectory sanity (#277): orthogonal, agreement-free; scored
        # from the SAME outcomes, so it adds no model calls and never touches the
        # drop / heat metrics.
        trajectory=score_trajectory(outcomes, roast_id),
        # One recommender call per tick — the basis for the cost estimate.
        call_count=len(outcomes),
    )


def _replays_to_cell(
    cand: Candidate, prompt_version: str, replays: list[RoastReplay]
) -> ReplayCell:
    """Assemble the per-roast replays for one candidate into a :class:`ReplayCell`.

    Pure: the report layer is unchanged — it still consumes :class:`ReplayCell`.
    ``scores`` and ``trajectories`` are emitted in the order of ``replays``, so
    the caller MUST pass them in a deterministic roast order (not completion
    order) — under ``--concurrency > 1`` cells complete out of order, so the
    orchestrator slots them by roast index and sorts before calling this (#281).

    Args:
        cand: The candidate model.
        prompt_version: The prompt version under test.
        replays: The per-roast replays for this (slug, prompt) cell, in roast
            order (the caller's responsibility — see above).

    Returns:
        The assembled :class:`ReplayCell`.
    """
    return ReplayCell(
        slug=cand.slug,
        tier=cand.tier.value,
        prompt_version=prompt_version,
        latency_risk=cand.latency_risk,
        scores=[r.score for r in replays],
        samples={r.roast_id: r.samples for r in replays},
        trajectories=[r.trajectory for r in replays],
    )


async def score_candidate(
    cand: Candidate,
    prompt_version: str,
    recommend: Callable[[AdvisorContext], Awaitable[RoastDecision]],
    roasts: tuple[Path, ...],
    cadence_seconds: float,
    *,
    clock: Callable[[], float] | None = None,
) -> ReplayCell:
    """Score one candidate's recommender over the replay roasts (key-free seam).

    Separated from :func:`run_replay_cell` so the scoring path can be driven by
    any async recommender — the real advisor (a key) or a canned callable (the
    test path) — with an injectable clock for deterministic latency in tests.

    Args:
        cand: The candidate model.
        prompt_version: The prompt version under test.
        recommend: The async recommender to score (real or canned).
        roasts: The replay roast fixtures.
        cadence_seconds: Roast-time spacing between scored ticks.
        clock: Monotonic clock; defaults to ``time.perf_counter``.

    Returns:
        The :class:`ReplayCell`.
    """
    tick_clock = clock if clock is not None else time.perf_counter
    replays: list[RoastReplay] = []
    for fixture in roasts:
        ticks, ground = build_ticks(fixture, cadence_seconds=cadence_seconds)
        outcomes = await replay_roast(ticks, recommend, clock=tick_clock)
        replays.append(
            build_roast_replay(cand.slug, prompt_version, roast_id_for(fixture), outcomes, ground)
        )
    return _replays_to_cell(cand, prompt_version, replays)


_HONEST_FRAMING = (
    "> **Read first — what these numbers mean.** The ground truth is a "
    "known-GOOD roast, *not* a provably optimal one. Every metric below measures "
    "**agreement with a known-good roast**, NOT absolute correctness: a capable "
    "model may legitimately differ from what the human did and still roast well, "
    "and high agreement is not proof of quality. Drop F1 = 1.0 means *matched "
    "this one good roast*, not *correct*. Use these as a quantitative aid to the "
    "operator's judgement (the advice samples + the latency gate), never a "
    "replacement for it."
)


def _phase_latency_str(phase_latency: list[PhaseLatency]) -> str:
    """Render per-phase latency compactly for a table cell."""
    parts: list[str] = []
    for pl in phase_latency:
        if pl.median_seconds is None:
            continue
        short = {"preheating": "pre", "roasting_pre_first_crack": "preFC", "development": "FC"}.get(
            pl.phase, pl.phase
        )
        parts.append(f"{short}={pl.median_seconds}s")
    return " ".join(parts) if parts else "—"


def render_replay_report(
    cells: list[ReplayCell],
    roasts: tuple[Path, ...],
    *,
    trajectory: bool = False,
) -> str:
    """Render the quantitative replay report (markdown) — agreement, NOT truth.

    Per (model, prompt): the drop F1 / precision / recall / timing, heat & fan
    MAE + directional agreement, and per-phase latency for each roast, followed
    by the advice samples at charge / Maillard / first-crack / development. The
    honest-framing caveat heads the report; no model is auto-selected (D20).

    Args:
        cells: The scored replay cells.
        roasts: The replay roast fixtures (for the header).
        trajectory: When ``True``, append the control-trajectory sanity section
            (#277) — the agreement-FREE command-signal coherence view. Off by
            default so the existing drop-decision report is byte-for-byte
            unchanged unless the operator opts in.

    Returns:
        The markdown report.
    """
    out: list[str] = []
    out.append("# Advisor bake-off — real-roast replay scorecard (#172/#173, D20)")
    out.append("")
    out.append(_HONEST_FRAMING)
    out.append("")
    roast_names = ", ".join(f"{p.parent.parent.name}/{p.parent.name}" for p in roasts)
    out.append(f"Test set (known-good 7-Jun Hottop roasts): {roast_names}")
    out.append(
        "Drop = should_drop agreement over ticks (F1/precision/recall) + first-drop "
        "timing error (s and °C vs the real drop). Heat/Fan = MAE (percentage "
        "points) + directional agreement (did the model move the lever the way the "
        "human did). Latency = median per phase, FC tightest. NO auto-pick."
    )
    out.append("")
    out.append(
        "Confusion matrices below are derived purely from the per-tick replay data "
        "(no extra calls). The 2×2 drop matrix is consistent with the F1/P/R above "
        "but is heavily class-imbalanced — almost every tick is no-drop, so TN "
        "dominates; read it WITH the drop-timing error, never alone. The 3×3 "
        "heat-direction matrix (cut/hold/raise) is the more informative view of "
        "control behaviour and anticipatory-cut agreement."
    )

    prompt_versions = sorted({c.prompt_version for c in cells})
    for pv in prompt_versions:
        out.append("")
        out.append(f"## prompt_version = {pv}")
        for cell in [c for c in cells if c.prompt_version == pv]:
            out.append("")
            risk = " [latency-risk]" if cell.latency_risk else ""
            out.append(f"### {cell.slug} ({cell.tier}){risk}")
            for score in cell.scores:
                out.append(_render_score_line(score))
                out.extend(render_drop_confusion_md(score.drop_confusion))
                out.extend(render_heat_direction_confusion_md(score.heat_direction_confusion))
            out.append("")
            out.append("  advice samples (operator judges quality — agreement ≠ correct):")
            for roast_name, picks in cell.samples.items():
                for label, outcome in picks:
                    out.append(_render_sample_line(roast_name, label, outcome))

    if trajectory:
        out.append("")
        out.append("---")
        out.append("")
        scored = [
            (
                f"{cell.slug} ({cell.tier}) prompt={cell.prompt_version}",
                cell.trajectories,
            )
            for cell in cells
        ]
        out.append(render_trajectory_report(scored))
    return "\n".join(out)


def _render_score_line(score: RoastScore) -> str:
    """Render one roast's metric line for a cell."""
    d = score.drop
    timing = (
        f"timing={d.timing_error_seconds:+}s/{d.timing_error_c:+}°C"
        if d.timing_error_seconds is not None
        else "timing=never-dropped"
    )
    heat_dir = (
        "—" if score.heat.directional_agreement is None else f"{score.heat.directional_agreement}"
    )
    fan_dir = (
        "—" if score.fan.directional_agreement is None else f"{score.fan.directional_agreement}"
    )
    return (
        f"- {score.roast_name} (truth DTR {score.development_time_ratio_truth}%, "
        f"{score.ok_count}/{score.tick_count} ok): "
        f"drop F1={d.f1} P={d.precision} R={d.recall} {timing}; "
        f"heat MAE={score.heat.mae} dir={heat_dir}; "
        f"fan MAE={score.fan.mae} dir={fan_dir}; "
        f"latency {_phase_latency_str(score.phase_latency)}"
    )


def _render_sample_line(roast_name: str, label: str, outcome: TickOutcome) -> str:
    """Render one advice-sample line for the report."""
    ctx = outcome.tick.context
    where = (
        f"{roast_name} {label} @ {outcome.tick.monotonic_seconds:.0f}s "
        f"bean={ctx.current_bean_temp_c}°C ror={ctx.bean_ror_c_per_min} "
        f"real(heat/fan)={outcome.tick.real_heat_percent}/{outcome.tick.real_fan_percent}"
    )
    if outcome.decision is None:
        return f"    - {where}: (no advice) error={outcome.error}"
    dec = outcome.decision
    return (
        f"    - {where}: model heat={dec.target_heat}% fan={dec.target_fan}% "
        f"drop={dec.should_drop} conf={dec.confidence} — {dec.rationale!r}"
    )


def replay_cells_to_json(cells: list[ReplayCell]) -> list[dict[str, Any]]:
    """Serialize replay cells (scores + samples) for the ``--out`` JSON."""
    rows: list[dict[str, Any]] = []
    for cell in cells:
        samples_json: dict[str, list[dict[str, Any]]] = {}
        for roast_name, picks in cell.samples.items():
            samples_json[roast_name] = [
                {
                    "moment": label,
                    "monotonic_seconds": o.tick.monotonic_seconds,
                    "bean_temp_c": o.tick.context.current_bean_temp_c,
                    "real_heat_percent": o.tick.real_heat_percent,
                    "real_fan_percent": o.tick.real_fan_percent,
                    "decision": o.decision.model_dump() if o.decision is not None else None,
                    "latency_seconds": o.latency_seconds,
                }
                for label, o in picks
            ]
        rows.append(
            {
                "slug": cell.slug,
                "tier": cell.tier,
                "prompt_version": cell.prompt_version,
                "latency_risk": cell.latency_risk,
                "scores": [score_to_json(s) for s in cell.scores],
                "samples": samples_json,
                "trajectories": [trajectory_to_json(t) for t in cell.trajectories],
            }
        )
    return rows


# --- "Most-interesting cells" surfacing from the capture (#284) -------------

# How many calls to surface per interest category by default.
DEFAULT_INTEREST_TOP_N = 5
# A heat move at/above this magnitude (percentage points) vs the previous real
# setpoint counts as a deliberate direction; smaller wobble is "hold". Matches
# the scorer's ``_DIRECTION_DEADBAND`` so the surfacing agrees with the metric.
_INTEREST_DEADBAND = 1.0


class InterestKind(enum.Enum):
    """Why a captured call was surfaced as "interesting" (#284). Plain ``Enum``.

    Drives the report grouping, not an auto-pick. A call may qualify under more
    than one kind; each kind's top-N is selected independently.
    """

    HEAT_DIRECTION_DISAGREEMENT = "heat-direction-disagreement"
    PRE_FC_INTERVENTION = "pre-fc-intervention"
    FAILURE = "failure"


@dataclasses.dataclass(frozen=True)
class InterestingCall:
    """A surfaced capture record plus why it is interesting (#284).

    Attributes:
        kind: The :class:`InterestKind` this call was surfaced under.
        score: The interest magnitude (kind-specific; larger = more interesting),
            used to rank within a kind.
        reason: A short human-readable explanation for the report.
        call: The underlying captured call (carries prompt + rationale +
            reasoning for the inline lookup).
    """

    kind: InterestKind
    score: float
    reason: str
    call: CapturedCall


def _heat_direction(prev: int | None, now: int) -> int | None:
    """Return -1 / 0 / +1 for a heat move, or ``None`` without a baseline."""
    if prev is None:
        return None
    delta = now - prev
    if delta >= _INTEREST_DEADBAND:
        return 1
    if delta <= -_INTEREST_DEADBAND:
        return -1
    return 0


_PRE_FC_PHASES: frozenset[str] = frozenset(
    {RoastPhase.PREHEATING.value, RoastPhase.ROASTING_PRE_FIRST_CRACK.value}
)


def select_interesting_calls(
    calls: list[CapturedCall], *, top_n: int = DEFAULT_INTEREST_TOP_N
) -> dict[InterestKind, list[InterestingCall]]:
    """Pick the most-interesting captured calls per category (#284).

    Surfaces, from the raw capture, the cells whose reasoning is worth reading
    without a re-run:

    - **heat-direction disagreement** — the model moved heat the OPPOSITE way the
      known-good human did (e.g. cut while the human raised/held), ranked by the
      magnitude of the swing. This is the lens that catches the 16 Jun pre-FC
      heat-cut.
    - **pre-FC intervention** — a pre-first-crack call that cuts heat or raises
      fan while the human held, ranked by the combined move size. This is the
      lens that catches the fan-into-the-crack case explicitly.
    - **failure** — a call that produced no decision (provider/timeout/parse).

    Ties break by larger latency then earlier tick so the order is deterministic.

    Args:
        calls: Every captured call to consider (a run's full capture).
        top_n: How many to keep per category.

    Returns:
        A mapping of each :class:`InterestKind` to its ranked top-N (possibly
        empty when nothing qualified).
    """
    heat_disagreements: list[InterestingCall] = []
    pre_fc: list[InterestingCall] = []
    failures: list[InterestingCall] = []

    for call in calls:
        if call.decision is None:
            failures.append(
                InterestingCall(
                    kind=InterestKind.FAILURE,
                    score=1.0,
                    reason=f"no decision ({call.error or 'unknown error'})",
                    call=call,
                )
            )
            continue
        dec = call.decision
        real_dir = _heat_direction(call.prev_real_heat_percent, call.real_heat_percent)
        model_dir = _heat_direction(call.prev_real_heat_percent, dec.target_heat)
        if real_dir is not None and model_dir is not None and real_dir != model_dir:
            swing = abs(dec.target_heat - call.real_heat_percent)
            heat_disagreements.append(
                InterestingCall(
                    kind=InterestKind.HEAT_DIRECTION_DISAGREEMENT,
                    score=float(swing),
                    reason=(
                        f"model moved heat {_dir_word(model_dir)} "
                        f"(→{dec.target_heat}%) while the roast moved it "
                        f"{_dir_word(real_dir)} (→{call.real_heat_percent}%)"
                    ),
                    call=call,
                )
            )
        # Pre-FC intervention: a heat cut or fan raise vs the previous real
        # setpoints, in a pre-first-crack phase — the #218 bake behaviour.
        if call.phase in _PRE_FC_PHASES and call.prev_real_heat_percent is not None:
            heat_cut = call.real_heat_percent - dec.target_heat
            fan_raise = dec.target_fan - call.real_fan_percent
            if heat_cut >= _INTEREST_DEADBAND or fan_raise >= _INTEREST_DEADBAND:
                pre_fc.append(
                    InterestingCall(
                        kind=InterestKind.PRE_FC_INTERVENTION,
                        score=float(max(heat_cut, 0) + max(fan_raise, 0)),
                        reason=(
                            f"pre-FC ({call.phase}): heat {call.real_heat_percent}→"
                            f"{dec.target_heat}% fan {call.real_fan_percent}→"
                            f"{dec.target_fan}% (human held — momentum/fan-into-crack risk)"
                        ),
                        call=call,
                    )
                )

    return {
        InterestKind.HEAT_DIRECTION_DISAGREEMENT: _rank(heat_disagreements, top_n),
        InterestKind.PRE_FC_INTERVENTION: _rank(pre_fc, top_n),
        InterestKind.FAILURE: _rank(failures, top_n),
    }


def _dir_word(direction: int) -> str:
    """Render a -1 / 0 / +1 heat direction as cut / hold / raise."""
    return {-1: "cut", 0: "hold", 1: "raise"}[direction]


def _rank(items: list[InterestingCall], top_n: int) -> list[InterestingCall]:
    """Rank interesting calls by score desc, then latency desc, then tick asc."""
    return sorted(
        items,
        key=lambda i: (
            -i.score,
            -(i.call.latency_seconds or 0.0),
            i.call.tick_index,
        ),
    )[:top_n]


def render_interesting_calls(
    selected: dict[InterestKind, list[InterestingCall]], *, top_n: int = DEFAULT_INTEREST_TOP_N
) -> str:
    """Render the "most-interesting cells" report section (#284).

    Each surfaced call is shown WITH its prompt context, the structured advice +
    rationale, and (when present) the reasoning trace — so "why did model X do Y"
    is a lookup, not a re-run. The reasoning trace is truncated for readability;
    the full text always lives in the (gitignored) capture file.

    Args:
        selected: The per-kind ranked calls from :func:`select_interesting_calls`.
        top_n: The top-N the selection used (for the heading).

    Returns:
        The markdown section.
    """
    out: list[str] = []
    out.append("# Most-interesting cells (auditable from the per-call capture, #284)")
    out.append("")
    out.append(
        "Surfaced from the full per-call capture so the worst / most-divergent "
        "advice is a lookup, not a re-run. Each entry carries its prompt context, "
        "the structured advice + rationale, and the reasoning trace WHERE THE "
        "PROVIDER EXPOSED IT (absent for non-reasoning models). The complete "
        "prompt + reasoning live in the gitignored capture file."
    )
    headings = {
        InterestKind.HEAT_DIRECTION_DISAGREEMENT: (
            f"## Largest heat-direction disagreements (top {top_n})"
        ),
        InterestKind.PRE_FC_INTERVENTION: (
            f"## Pre-first-crack interventions — heat cut / fan-into-crack (top {top_n})"
        ),
        InterestKind.FAILURE: f"## Failures — calls with no decision (top {top_n})",
    }
    for kind in (
        InterestKind.PRE_FC_INTERVENTION,
        InterestKind.HEAT_DIRECTION_DISAGREEMENT,
        InterestKind.FAILURE,
    ):
        out.append("")
        out.append(headings[kind])
        items = selected.get(kind, [])
        if not items:
            out.append("")
            out.append("  (none)")
            continue
        for item in items:
            out.append("")
            out.extend(_render_interesting_call(item))
    return "\n".join(out)


# How many characters of a reasoning trace to inline in the report (the full
# trace is always in the gitignored capture file).
_REASONING_PREVIEW_CHARS = 600


def _render_interesting_call(item: InterestingCall) -> list[str]:
    """Render one surfaced call with prompt + rationale + reasoning."""
    call = item.call
    ctx = call.context
    lines = [
        f"- {call.model_slug} | prompt={call.prompt_version} | {call.roast_id} "
        f"tick={call.tick_index} @ {call.monotonic_seconds:.0f}s ({call.phase})",
        f"    why: {item.reason}",
        f"    prompt: bean={ctx.current_bean_temp_c}°C env={ctx.current_env_temp_c}°C "
        f"bean_ror={ctx.bean_ror_c_per_min} dev_elapsed={ctx.development_elapsed_seconds} "
        f"fc_detected={ctx.first_crack_detected} "
        f"real(heat/fan)={call.real_heat_percent}/{call.real_fan_percent}",
    ]
    if call.decision is not None:
        dec = call.decision
        lines.append(
            f"    advice: heat={dec.target_heat}% fan={dec.target_fan}% "
            f"drop={dec.should_drop} conf={dec.confidence} rationale={dec.rationale!r}"
        )
    else:
        lines.append(f"    advice: (none) error={call.error}")
    if call.reasoning_available and call.reasoning is not None:
        preview = call.reasoning.replace("\n", " ").strip()
        if len(preview) > _REASONING_PREVIEW_CHARS:
            preview = preview[:_REASONING_PREVIEW_CHARS] + " […]"
        lines.append(f"    reasoning: {preview}")
    else:
        lines.append("    reasoning: (provider exposed none)")
    return lines


def interesting_cells_to_json(
    selected: dict[InterestKind, list[InterestingCall]],
) -> dict[str, list[dict[str, Any]]]:
    """Serialize the surfaced interesting calls for the ``--out`` JSON (#284).

    Each entry carries its identity, the reason, the structured advice, and the
    full reasoning trace + ``reasoning_available`` flag so the JSON artifact is a
    self-contained derived summary (the raw capture stays gitignored).

    Args:
        selected: The per-kind ranked calls from :func:`select_interesting_calls`.

    Returns:
        A mapping of the kind value to its list of JSON-ready entries.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for kind, items in selected.items():
        out[kind.value] = [
            {
                "score": item.score,
                "reason": item.reason,
                "model_slug": item.call.model_slug,
                "prompt_version": item.call.prompt_version,
                "roast_id": item.call.roast_id,
                "tick_index": item.call.tick_index,
                "monotonic_seconds": item.call.monotonic_seconds,
                "phase": item.call.phase,
                "decision": _decision_to_json(item.call.decision),
                "reasoning": item.call.reasoning,
                "reasoning_available": item.call.reasoning_available,
            }
            for item in items
        ]
    return out


async def run_replay_bakeoff(
    roster: tuple[Candidate, ...],
    roasts: tuple[Path, ...],
    prompt_versions: list[str],
    reasoning: ReasoningEffort | None,
    cadence_seconds: float,
) -> tuple[list[AvailabilityResult], list[ReplayCell]]:
    """Run the replay pipeline: availability sweep, then score survivors.

    Args:
        roster: The candidate roster.
        roasts: The replay roast fixtures (the known-good test set).
        prompt_versions: Prompt versions to compare (e.g. ``["v2", "v3"]``).
        reasoning: Reasoning effort, or ``None`` for the provider default.
        cadence_seconds: Roast-time spacing between scored ticks.

    Returns:
        ``(availability_results, replay_cells)``.
    """
    survivors, availability = await availability_sweep(roster, prompt_versions[0], reasoning)
    print(render_availability(availability), flush=True)
    print("", flush=True)
    cells: list[ReplayCell] = []
    for pv in prompt_versions:
        # Candidates are independent (each calls a different model), so run them
        # concurrently — same rationale as the concurrent availability sweep.
        # Within a single roast the ticks must stay serial (per-tick latency
        # measurement), so the concurrency is at the (candidate) level only.
        # ``gather`` preserves ``survivors`` order; progress is printed after
        # each cell resolves so the buffered output stays readable.
        print(
            f"replaying {len(survivors)} survivors (prompt {pv}) over {len(roasts)} roasts…",
            flush=True,
        )
        cells_for_pv = list(
            await asyncio.gather(
                *(
                    run_replay_cell(cand, pv, reasoning, roasts, cadence_seconds)
                    for cand in survivors
                )
            )
        )
        for cell in cells_for_pv:
            for score in cell.scores:
                print(f"  {cell.slug}: {_render_score_line(score)[2:]}", flush=True)
        cells.extend(cells_for_pv)
    return availability, cells


# --- Observability + incremental checkpoint + cost guard (#280) -------------

# The default per-cell cost estimate (USD) used when no provider/usage dollar
# cost is exposed. pydantic_ai surfaces *token* usage (``AdvisorUsage``) but not
# a billed dollar amount, so the cost guard estimates spend as
# ``call_count * PER_CALL_COST_USD``. This is a deliberately rough, documented
# upper-ish guard rail — overrideable with ``--cost-per-call`` — not an invoice.
# Anchored to the 16 Jun run: ~$32 across a full roster sweep, dominated by the
# frontier models; per get_recommendation call this lands in the low-cents range.
DEFAULT_COST_PER_CALL_USD = 0.02

# How often the heartbeat line is emitted, in wall-clock seconds.
DEFAULT_HEARTBEAT_SECONDS = 30.0


# --- Concurrency + retry/backoff (#281) -------------------------------------

# Default in-flight cell cap. Bounded so the harness never blasts the provider
# (operator, 16 Jun): the independent ``(model, prompt, roast)`` cells run
# concurrently behind a bounded worker pool (a fixed set of workers pulling from
# a shared queue — equivalent to a semaphore), so the fan-out is always capped.
DEFAULT_CONCURRENCY = 8
# Hard ceiling on the configurable cap — a sanity bound so a fat ``--concurrency``
# typo cannot turn the bounded fan-out into a blast.
MAX_CONCURRENCY = 32

# Backoff policy for a transient provider failure (429 / 5xx / network blip).
# Exponential with full jitter, honouring an explicit ``Retry-After`` when the
# provider sends one. Builds on the availability-sweep retry idea but adds the
# rate-limit-aware classification + delay the bake-off run needs under fan-out.
DEFAULT_RETRY_ATTEMPTS = 4
DEFAULT_RETRY_BASE_SECONDS = 0.5
DEFAULT_RETRY_MAX_SECONDS = 30.0

# HTTP status codes (5xx) and the rate-limit code that warrant a retry. Matched
# textually against the provider error message: pydantic_ai wraps the transport
# error in ``AdvisorProviderError(str(exc))``, whose text carries the status
# (e.g. ``status_code: 429``), so the classifier reads the message, not a typed
# field the advisor does not expose.
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
# Network-level transient phrases (no HTTP status) that also warrant a retry.
_RETRYABLE_PHRASES = (
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
    "service unavailable",
    "too many requests",
    "rate limit",
    "overloaded",
)
_RETRY_AFTER_RE = re.compile(
    r"retry[-\s]?after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", re.IGNORECASE
)
_STATUS_RE = re.compile(r"(?:status[_\s]?code|http)['\"]?\s*[:=]?\s*['\"]?(\d{3})", re.IGNORECASE)


def is_retryable_error(message: str | None) -> bool:
    """Classify a provider error message as a transient, retryable failure.

    Reads the textual error pydantic_ai surfaces (``AdvisorProviderError`` wraps
    the transport error's ``str``) — the advisor exposes no typed status, so the
    bake-off matches the message. A ``429`` / ``5xx`` status or a recognised
    network-transient phrase is retryable; a ``4xx`` other than ``429`` (a bad
    request, an auth failure, a missing model) is NOT — retrying it just wastes
    the budget.

    Args:
        message: The error message to classify, or ``None``.

    Returns:
        ``True`` if the failure looks transient and is worth retrying.
    """
    if not message:
        return False
    text = message.lower()
    for match in _STATUS_RE.finditer(message):
        if int(match.group(1)) in _RETRYABLE_STATUS:
            return True
    return any(phrase in text for phrase in _RETRYABLE_PHRASES)


def parse_retry_after_seconds(message: str | None) -> float | None:
    """Extract a ``Retry-After`` delay (seconds) from a provider error message.

    Honours an explicit provider-supplied retry delay over the computed
    exponential backoff (the operator requirement). Only the numeric-seconds form
    is parsed (the form OpenRouter / OpenAI-compatible providers surface); an
    HTTP-date ``Retry-After`` is ignored (returns ``None``, so the caller falls
    back to exponential backoff).

    Args:
        message: The error message to scan, or ``None``.

    Returns:
        The retry delay in seconds, or ``None`` when none is present.
    """
    if not message:
        return None
    match = _RETRY_AFTER_RE.search(message)
    if match is None:
        return None
    return float(match.group(1))


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    """Exponential-backoff-with-jitter retry policy for transient failures (#281).

    Bounded and honouring ``Retry-After``: the delay before attempt ``n`` is the
    provider's ``Retry-After`` when supplied, else a full-jitter exponential
    ``base * 2**(n-1)`` capped at ``max_seconds``. A non-retryable error (a 4xx
    that is not 429, a malformed-output parse error) is re-raised immediately so
    the budget is not wasted on a request that will never succeed.

    Attributes:
        attempts: Total attempts (1 try + ``attempts - 1`` retries).
        base_seconds: The exponential base delay.
        max_seconds: The per-delay ceiling (before jitter).
        rng: Jitter source in ``[0, 1)`` (injectable so tests are deterministic).
    """

    attempts: int = DEFAULT_RETRY_ATTEMPTS
    base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    max_seconds: float = DEFAULT_RETRY_MAX_SECONDS
    rng: Callable[[], float] = random.random

    def delay_for(self, attempt: int, retry_after: float | None) -> float:
        """Compute the delay before the given (1-based) retry attempt.

        Args:
            attempt: The upcoming attempt number (>= 1; the first retry is 1).
            retry_after: A provider-supplied ``Retry-After`` (seconds), or
                ``None`` to use exponential backoff.

        Returns:
            The delay in seconds (provider ``Retry-After`` wins; else full-jitter
            exponential, capped).
        """
        if retry_after is not None:
            return max(0.0, retry_after)
        ceiling = min(self.max_seconds, self.base_seconds * (2 ** max(0, attempt - 1)))
        return self.rng() * ceiling


def with_retry(
    recommender: ReasoningRecommender,
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_attempt: Callable[[], None] | None = None,
) -> ReasoningRecommender:
    """Wrap a reasoning-aware recommender with bounded retry + backoff (#281).

    On a transient failure (:func:`is_retryable_error`) the call is retried up to
    ``policy.attempts`` times, sleeping the policy's delay (honouring a
    ``Retry-After`` in the error) between tries; a non-retryable error or the
    exhaustion of all attempts re-raises the last error so the per-tick capture
    records it exactly as the serial path would. Per-call latency is measured by
    the caller AROUND this wrapper, so a retried call's latency includes its
    backoff — which is the real cost the FC gate must see.

    Every provider REQUEST (the first try and each retry) invokes ``on_attempt``
    before it is issued, so the cost guard can account *actual* provider requests
    — not one-per-tick — and keep the budget honest when calls are retried.

    Args:
        recommender: The reasoning-aware recommender to wrap.
        policy: The retry/backoff policy.
        sleep: Async sleep between attempts; defaults to :func:`asyncio.sleep`
            (injectable so tests stay instant).
        on_attempt: Called once per provider request attempt (incl. retries),
            just before the request is issued — the hook the cost guard uses to
            count real requests. ``None`` to count nothing.

    Returns:
        A recommender with the same signature that retries transient failures.
    """
    do_sleep = sleep if sleep is not None else asyncio.sleep

    async def retrying(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
        last_exc: Exception | None = None
        for attempt in range(1, policy.attempts + 1):
            if on_attempt is not None:
                on_attempt()
            try:
                return await recommender(context)
            except Exception as exc:  # noqa: BLE001 — classify, then retry or re-raise
                message = str(exc)
                last_exc = exc
                if attempt >= policy.attempts or not is_retryable_error(message):
                    raise
                await do_sleep(policy.delay_for(attempt, parse_retry_after_seconds(message)))
        # Unreachable: the loop either returns, re-raises inside, or re-raises on
        # the final attempt. Re-raise defensively so the type checker is satisfied.
        raise last_exc  # type: ignore[misc]  # pragma: no cover

    return retrying


class _ConcurrentBudget:
    """Atomic, concurrency-safe call-budget reservation for the cost guard (#281).

    Under concurrent cells the serial "check then add" of :class:`CostGuard` is a
    race: two cells could each see headroom and both schedule, overshooting the
    cap. This guards the same accounting with an :class:`asyncio.Lock` and a
    single ``try_reserve`` that atomically checks AND commits the projected calls,
    so once the budget is reached no further cell is scheduled.

    **Reservation vs actual requests.** ``try_reserve`` pre-pays a cell's
    *minimum* cost — one provider request per tick. But ``with_retry`` may issue
    several requests for a single tick (a retried 429 / 5xx), so the cost guard
    counts ACTUAL provider requests, not one-per-tick: every attempt increments
    the guard via :meth:`account_attempt`, and the pre-paid reservation is then
    backed out via :meth:`release_reserved` so each request is counted exactly
    once. The honest overshoot bound is therefore: the running total can exceed
    ``--max-spend`` only by the (retry-inflated) request counts of the cells that
    were already in flight when the cap was reached — those reserved while there
    was still headroom.

    The underlying :class:`CostGuard` stays the single accounting source (calls
    + spend); this only makes its mutation concurrency-safe and retry-aware.

    Attributes:
        guard: The wrapped :class:`CostGuard` (the accounting source of truth).
    """

    def __init__(self, guard: CostGuard) -> None:
        """Wrap ``guard`` with a lock for concurrency-safe reservation.

        Args:
            guard: The cost guard whose accounting is made concurrency-safe.
        """
        self.guard = guard
        self._lock = asyncio.Lock()
        self._stopped = False

    async def try_reserve(self, upcoming_calls: int) -> bool:
        """Atomically reserve ``upcoming_calls`` (the cell's minimum cost).

        The reservation is the per-tick floor; per-attempt accounting + the
        matching release reconcile it to the actual request count afterwards.

        Args:
            upcoming_calls: The minimum number of requests the cell would cost
                (one per tick, before any retries).

        Returns:
            ``True`` if the calls were reserved (the cell may run); ``False`` if
            running them would breach ``--max-spend`` (the cell is skipped and the
            run is marked stopped so no further cell is scheduled).
        """
        async with self._lock:
            if self._stopped or self.guard.would_exceed(upcoming_calls):
                self._stopped = True
                return False
            self.guard.add_calls(upcoming_calls)
            return True

    def account_attempt(self) -> None:
        """Count one ACTUAL provider request (the first try or a retry).

        Wired as ``with_retry``'s ``on_attempt`` hook so the guard's call count
        — and thus the spend estimate — tracks real requests including retries,
        not one-per-tick. Thread-safe without the lock: ``+= 1`` on the guard's
        counter is atomic under the single-threaded event loop, and there is no
        check-then-act here (unlike :meth:`try_reserve`).
        """
        self.guard.add_calls(1)

    def release_reserved(self, reserved_calls: int) -> None:
        """Back out a cell's pre-paid reservation after its attempts are counted.

        Each tick's first request is counted twice otherwise — once by the
        ``try_reserve`` floor, once by :meth:`account_attempt`. Releasing the
        reservation leaves exactly the actual request count accounted.

        Args:
            reserved_calls: The per-tick floor previously reserved for the cell.
        """
        self.guard.add_calls(-reserved_calls)

    @property
    def stopped(self) -> bool:
        """Whether the budget has tripped (no further cells should schedule)."""
        return self._stopped


def _decision_to_json(decision: RoastDecision | None) -> dict[str, Any] | None:
    """Serialize a decision for the sidecar, or ``None`` for a failed tick."""
    return decision.model_dump() if decision is not None else None


def _decision_from_json(raw: dict[str, Any] | None) -> RoastDecision | None:
    """Rebuild a decision from a sidecar row, or ``None`` for a failed tick."""
    return RoastDecision.model_validate(raw) if raw is not None else None


def roast_replay_to_record(replay: RoastReplay) -> dict[str, Any]:
    """Serialize a :class:`RoastReplay` to a sidecar JSONL record.

    Persists only the cell identity and the raw per-tick recommender outcomes
    (decision + latency + error). The derived score / samples / trajectory are
    NOT stored — they are recomputed by the pure scorers on reload, so the
    scoring math is the single source of truth and never drifts.

    Args:
        replay: The completed per-roast replay to persist.

    Returns:
        A JSON-ready record keyed by ``(model_slug, prompt_version, roast_id)``.
    """
    return {
        "model_slug": replay.slug,
        "prompt_version": replay.prompt_version,
        "roast_id": replay.roast_id,
        "call_count": replay.call_count,
        "outcomes": [
            {
                "decision": _decision_to_json(o.decision),
                "latency_seconds": o.latency_seconds,
                "error": o.error,
            }
            for o in replay.outcomes
        ],
    }


def roast_replay_from_record(
    record: dict[str, Any], ticks: list[ReplayTick], ground: GroundTruth
) -> RoastReplay:
    """Rebuild a :class:`RoastReplay` from a sidecar record + rebuilt ticks.

    The ticks are reconstructed deterministically from the fixture (no model
    calls); each saved per-tick recommender outcome is reattached to its tick in
    order, then the existing pure scorers derive the metrics — identical to a
    fresh run. The recorded outcome count must match the rebuilt tick count, or
    the fixture/cadence changed since the checkpoint and the record is rejected.

    Args:
        record: A sidecar record from :func:`roast_replay_to_record`.
        ticks: The ticks rebuilt from the fixture for this roast.
        ground: The roast's ground truth.

    Returns:
        The reconstructed :class:`RoastReplay`.

    Raises:
        ValueError: If the recorded outcome count does not match ``ticks``.
    """
    raw_outcomes = cast("list[dict[str, Any]]", record["outcomes"])
    if len(raw_outcomes) != len(ticks):
        raise ValueError(
            f"checkpoint for {record['model_slug']}/{record['prompt_version']}/"
            f"{record['roast_id']} has {len(raw_outcomes)} outcomes but the fixture "
            f"rebuilds {len(ticks)} ticks (fixture or cadence changed) — re-run from scratch"
        )
    outcomes = [
        TickOutcome(
            tick=tick,
            decision=_decision_from_json(raw.get("decision")),
            latency_seconds=raw.get("latency_seconds"),
            error=raw.get("error"),
        )
        for tick, raw in zip(ticks, raw_outcomes, strict=True)
    ]
    return build_roast_replay(
        str(record["model_slug"]),
        str(record["prompt_version"]),
        str(record["roast_id"]),
        outcomes,
        ground,
    )


def sidecar_path(out: Path) -> Path:
    """Return the sidecar JSONL path next to the ``--out`` JSON path."""
    return out.with_name(out.name + ".cells.jsonl")


CellKey = tuple[str, str, str]


def cell_key(slug: str, prompt_version: str, roast_id: str) -> CellKey:
    """The ``(model_slug, prompt_version, roast_id)`` checkpoint key."""
    return (slug, prompt_version, roast_id)


class Checkpoint:
    """Append-only sidecar of completed ``(slug, prompt, roast)`` cells (#280).

    Each completed per-roast replay is appended to a JSONL sidecar immediately,
    so a kill / cap-hit / crash leaves every finished cell on disk. On start the
    sidecar is loaded and already-complete cells are skipped, so a re-run never
    re-pays for finished work (resume); the in-flight cell is the most a kill can
    lose. The final scorecard renders from the accumulated cells either way.

    Attributes:
        path: The sidecar JSONL path.
        resume: When ``True`` (default), pre-existing records are loaded and
            their cells skipped; when ``False``, the sidecar is truncated on open
            so the run starts clean.
    """

    def __init__(self, path: Path, *, resume: bool = True) -> None:
        """Open (and optionally load) the sidecar at ``path``.

        Args:
            path: The sidecar JSONL path.
            resume: Load + skip existing cells when ``True``; truncate when
                ``False``.
        """
        self.path = path
        self.resume = resume
        self._records: dict[CellKey, dict[str, Any]] = {}
        if resume and path.exists():
            self._load()
        elif not resume and path.exists():
            path.unlink()

    def _load(self) -> None:
        """Load existing sidecar records, keyed by the cell triple.

        A later record for the same key wins (a resumed re-run that re-did a
        cell), so loading is last-write-wins per key.
        """
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            record = cast("dict[str, Any]", json.loads(line))
            key = cell_key(
                str(record["model_slug"]),
                str(record["prompt_version"]),
                str(record["roast_id"]),
            )
            self._records[key] = record

    def has(self, key: CellKey) -> bool:
        """Return whether ``key``'s cell is already complete on disk."""
        return key in self._records

    def record(self, key: CellKey) -> dict[str, Any]:
        """Return the stored record for an already-complete ``key``."""
        return self._records[key]

    def append(self, replay: RoastReplay) -> None:
        """Persist a completed replay to disk immediately and remember it.

        Appends one JSONL line and flushes to the OS, so an abrupt kill right
        after this call still leaves the cell recoverable.

        Args:
            replay: The completed per-roast replay to persist.
        """
        record = roast_replay_to_record(replay)
        key = cell_key(replay.slug, replay.prompt_version, replay.roast_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
        self._records[key] = record

    def completed_count(self) -> int:
        """How many cells are already complete on disk."""
        return len(self._records)


# --- Full per-call capture (prompt + response + reasoning) (#284) ------------

# A reasoning-aware recommender: given a tick context, return the recommendation
# AND the provider's reasoning trace (``None`` when the provider exposed none).
# The real advisor's ``get_recommendation_with_reasoning`` matches this; a canned
# callable matches it too, which is how the capture is tested without a key.
ReasoningRecommender = Callable[[AdvisorContext], Awaitable[tuple[RoastDecision, str | None]]]


@dataclasses.dataclass
class CapturedCall:
    """The full audit record of one scored advisor call (#284).

    Persisted so re-analysis (e.g. finding the pre-FC heat-cut / fan-into-crack
    or the lever-unit confusion) needs **no re-run** — the prompt, the structured
    response, and the reasoning trace are all on disk. The score-relevant fields
    are duplicated alongside the raw response so a reader can rank "most
    interesting" without re-deriving them.

    Attributes:
        model_slug: The model that produced the call.
        prompt_version: The prompt version under test.
        roast_id: The roast the tick belongs to (see :func:`roast_id_for`).
        tick_index: The 0-based tick position within the roast.
        monotonic_seconds: The tick's roast timestamp.
        roast_elapsed_seconds: Elapsed roast time at the tick (from the context).
        phase: The agent phase value at the tick.
        context: The full :class:`AdvisorContext` sent (the prompt).
        decision: The raw structured :class:`RoastDecision` (incl. ``rationale``),
            or ``None`` when the call failed.
        reasoning: The provider's reasoning / thinking trace when exposed, else
            ``None``.
        reasoning_available: Whether a reasoning trace was captured for the call.
        latency_seconds: The measured call latency, or ``None`` on failure.
        error: The failure message when the call produced no decision.
        cost_estimate_usd: The per-call cost estimate (calls x cost-per-call).
        real_heat_percent: The known-good roast's heat setpoint at the tick.
        real_fan_percent: The known-good roast's fan setpoint at the tick.
        prev_real_heat_percent: The previous real heat setpoint (direction base).
        real_should_drop: Whether the real roast had dropped by the tick.
    """

    model_slug: str
    prompt_version: str
    roast_id: str
    tick_index: int
    monotonic_seconds: float
    roast_elapsed_seconds: float
    phase: str
    context: AdvisorContext
    decision: RoastDecision | None
    reasoning: str | None
    reasoning_available: bool
    latency_seconds: float | None
    error: str | None
    cost_estimate_usd: float
    real_heat_percent: int
    real_fan_percent: int
    prev_real_heat_percent: int | None
    real_should_drop: bool


def captured_call_to_json(call: CapturedCall) -> dict[str, Any]:
    """Serialize a :class:`CapturedCall` to a JSON-ready record.

    The context and decision are dumped via their Pydantic ``model_dump`` so the
    full prompt + structured response round-trip; everything else is scalar.

    Args:
        call: The captured call.

    Returns:
        A JSON-ready dict keyed for lookup by the cell triple + tick index.
    """
    return {
        "model_slug": call.model_slug,
        "prompt_version": call.prompt_version,
        "roast_id": call.roast_id,
        "tick_index": call.tick_index,
        "monotonic_seconds": call.monotonic_seconds,
        "roast_elapsed_seconds": call.roast_elapsed_seconds,
        "phase": call.phase,
        "context": call.context.model_dump(mode="json"),
        "decision": _decision_to_json(call.decision),
        "reasoning": call.reasoning,
        "reasoning_available": call.reasoning_available,
        "latency_seconds": call.latency_seconds,
        "error": call.error,
        "cost_estimate_usd": call.cost_estimate_usd,
        "real_heat_percent": call.real_heat_percent,
        "real_fan_percent": call.real_fan_percent,
        "prev_real_heat_percent": call.prev_real_heat_percent,
        "real_should_drop": call.real_should_drop,
    }


def captured_call_from_json(record: dict[str, Any]) -> CapturedCall:
    """Rebuild a :class:`CapturedCall` from a persisted capture record.

    The inverse of :func:`captured_call_to_json` — the context and decision are
    re-validated through their Pydantic models so the reload is field-for-field
    identical to capture time.

    Args:
        record: A capture record from :func:`captured_call_to_json`.

    Returns:
        The reconstructed :class:`CapturedCall`.
    """
    return CapturedCall(
        model_slug=str(record["model_slug"]),
        prompt_version=str(record["prompt_version"]),
        roast_id=str(record["roast_id"]),
        tick_index=int(record["tick_index"]),
        monotonic_seconds=float(record["monotonic_seconds"]),
        roast_elapsed_seconds=float(record["roast_elapsed_seconds"]),
        phase=str(record["phase"]),
        context=AdvisorContext.model_validate(record["context"]),
        decision=_decision_from_json(record.get("decision")),
        reasoning=cast("str | None", record.get("reasoning")),
        reasoning_available=bool(record.get("reasoning_available", False)),
        latency_seconds=cast("float | None", record.get("latency_seconds")),
        error=cast("str | None", record.get("error")),
        cost_estimate_usd=float(record.get("cost_estimate_usd", 0.0)),
        real_heat_percent=int(record["real_heat_percent"]),
        real_fan_percent=int(record["real_fan_percent"]),
        prev_real_heat_percent=cast("int | None", record.get("prev_real_heat_percent")),
        real_should_drop=bool(record["real_should_drop"]),
    )


def build_captured_calls(
    slug: str,
    prompt_version: str,
    roast_id: str,
    ticks: list[ReplayTick],
    outcomes: list[TickOutcome],
    reasonings: list[str | None],
    cost_per_call: float,
) -> list[CapturedCall]:
    """Assemble the per-tick capture records for one completed roast cell.

    Pure: pairs each tick with its outcome (decision + latency + error) and the
    reasoning trace captured for it. Used for both a fresh run and reconstruction
    in tests; no model calls.

    Args:
        slug: The model slug.
        prompt_version: The prompt version under test.
        roast_id: The roast id.
        ticks: The reconstructed ticks for the roast.
        outcomes: The per-tick outcomes (same length / order as ``ticks``).
        reasonings: The per-tick reasoning traces (same length / order).
        cost_per_call: The run's per-call cost estimate.

    Returns:
        One :class:`CapturedCall` per tick, in tick order.
    """
    calls: list[CapturedCall] = []
    for index, (tick, outcome, reasoning) in enumerate(
        zip(ticks, outcomes, reasonings, strict=True)
    ):
        calls.append(
            CapturedCall(
                model_slug=slug,
                prompt_version=prompt_version,
                roast_id=roast_id,
                tick_index=index,
                monotonic_seconds=tick.monotonic_seconds,
                roast_elapsed_seconds=tick.context.roast_elapsed_seconds,
                phase=tick.context.phase.value,
                context=tick.context,
                decision=outcome.decision,
                reasoning=reasoning,
                reasoning_available=reasoning is not None,
                latency_seconds=outcome.latency_seconds,
                error=outcome.error,
                cost_estimate_usd=round(cost_per_call, 6),
                real_heat_percent=tick.real_heat_percent,
                real_fan_percent=tick.real_fan_percent,
                prev_real_heat_percent=tick.prev_real_heat_percent,
                real_should_drop=tick.real_should_drop,
            )
        )
    return calls


async def replay_roast_with_capture(
    ticks: list[ReplayTick],
    recommender: ReasoningRecommender,
    *,
    clock: Callable[[], float],
) -> tuple[list[TickOutcome], list[str | None]]:
    """Run a reasoning-aware recommender over every tick, capturing reasoning.

    The capture counterpart to :func:`replay_roast`: it produces the SAME
    :class:`TickOutcome` list the scorers consume (so the scoring math is
    untouched) plus a parallel per-tick reasoning list. A failed call yields a
    ``None`` decision outcome and a ``None`` reasoning, exactly as the scoring
    path expects — reasoning absence never errors.

    Args:
        ticks: The reconstructed ticks (from :func:`build_ticks`).
        recommender: A reasoning-aware async recommender returning
            ``(decision, reasoning)``.
        clock: A monotonic clock returning seconds.

    Returns:
        ``(outcomes, reasonings)`` — aligned to ``ticks`` by index.
    """
    outcomes: list[TickOutcome] = []
    reasonings: list[str | None] = []
    for tick in ticks:
        started = clock()
        try:
            decision, reasoning = await recommender(tick.context)
        except Exception as exc:  # noqa: BLE001 — capture, score the rest of the roast
            outcomes.append(
                TickOutcome(
                    tick=tick,
                    decision=None,
                    latency_seconds=round(clock() - started, 3),
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
            )
            reasonings.append(None)
            continue
        outcomes.append(
            TickOutcome(tick=tick, decision=decision, latency_seconds=round(clock() - started, 3))
        )
        reasonings.append(reasoning)
    return outcomes, reasonings


def capture_path(out: Path) -> Path:
    """Return the per-call capture JSONL path next to the ``--out`` JSON path.

    Distinct from the :func:`sidecar_path` checkpoint: the capture is large and
    contains the full prompts / reasoning, so it lives at its own gitignored path
    (``<out>.capture.jsonl``) and is never committed (#284).
    """
    return out.with_name(out.name + ".capture.jsonl")


class CaptureWriter:
    """Append-only, resumable sidecar of full per-call audit records (#284).

    Mirrors :class:`Checkpoint`'s incremental-flush + resume discipline so the
    capture is also kill-safe and resumable: each completed cell's per-tick
    records are appended immediately and flushed, and on a resumed run cells
    already present are NOT re-captured (the checkpoint already skips re-running
    them). Records are keyed by ``(model_slug, prompt_version, roast_id)``; a
    later write for a key supersedes an earlier one (last-write-wins), matching
    the checkpoint.

    The capture file is gitignored (large + contains full prompts / reasoning);
    only the derived summary / report is committed.

    Attributes:
        path: The capture JSONL path.
        resume: When ``True`` (default), pre-existing records are loaded and
            their cells are not re-captured; when ``False``, the file is
            truncated on open.
    """

    def __init__(self, path: Path, *, resume: bool = True) -> None:
        """Open (and optionally load) the capture file at ``path``.

        Args:
            path: The capture JSONL path.
            resume: Load existing records when ``True``; truncate when ``False``.
        """
        self.path = path
        self.resume = resume
        self._by_cell: dict[CellKey, list[CapturedCall]] = {}
        if resume and path.exists():
            self._load()
        elif not resume and path.exists():
            path.unlink()

    def _load(self) -> None:
        """Load existing capture records, grouped by the cell triple.

        Records are grouped per ``(slug, prompt, roast)`` cell in file order; a
        later cell's records replace an earlier same-key group (last-write-wins),
        matching the checkpoint's resume semantics.
        """
        ordered: dict[CellKey, list[CapturedCall]] = {}
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            record = cast("dict[str, Any]", json.loads(line))
            call = captured_call_from_json(record)
            key = cell_key(call.model_slug, call.prompt_version, call.roast_id)
            # A new group for a key supersedes a previously-loaded one — the
            # records arrive cell-contiguous, so the first tick (index 0) of a
            # key starts a fresh group.
            if key not in ordered or call.tick_index == 0:
                ordered[key] = []
            ordered[key].append(call)
        self._by_cell = ordered

    def has(self, key: CellKey) -> bool:
        """Whether a cell's capture is already on disk."""
        return key in self._by_cell

    def calls_for(self, key: CellKey) -> list[CapturedCall]:
        """Return the captured calls for a cell (empty if absent)."""
        return list(self._by_cell.get(key, []))

    def append(self, calls: list[CapturedCall]) -> None:
        """Persist one cell's per-tick capture records immediately.

        Writes every tick's record as its own JSONL line and flushes, so an
        abrupt kill after this call leaves the cell's capture recoverable.

        Args:
            calls: The per-tick capture records for one completed roast cell.
        """
        if not calls:
            return
        key = cell_key(calls[0].model_slug, calls[0].prompt_version, calls[0].roast_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for call in calls:
                handle.write(json.dumps(captured_call_to_json(call)) + "\n")
            handle.flush()
        self._by_cell[key] = list(calls)

    def all_calls(self) -> list[CapturedCall]:
        """Every captured call across all cells, in load / append order."""
        flat: list[CapturedCall] = []
        for calls in self._by_cell.values():
            flat.extend(calls)
        return flat


class CostGuard:
    """Tracks cumulative spend and trips a graceful stop before a budget (#280).

    Spend is estimated as ``calls * cost_per_call`` (pydantic_ai exposes token
    usage, not a billed dollar amount — see :data:`DEFAULT_COST_PER_CALL_USD`).
    ``would_exceed`` lets the orchestrator decide BEFORE paying for the next
    cell, so a budget stop flushes partials and exits cleanly rather than raising
    mid-call. The guard never makes a call itself — it only accounts.

    Attributes:
        cost_per_call: Estimated USD per recommender call.
        max_spend: The budget ceiling in USD, or ``None`` for unlimited.
    """

    def __init__(self, cost_per_call: float, max_spend: float | None) -> None:
        """Initialise the guard.

        Args:
            cost_per_call: Estimated USD per recommender call.
            max_spend: The budget ceiling in USD, or ``None`` for no cap.
        """
        self.cost_per_call = cost_per_call
        self.max_spend = max_spend
        self._calls = 0

    @property
    def calls(self) -> int:
        """Total recommender calls accounted so far."""
        return self._calls

    @property
    def spend(self) -> float:
        """Estimated cumulative spend in USD."""
        return round(self._calls * self.cost_per_call, 4)

    def add_calls(self, n: int) -> None:
        """Account ``n`` completed recommender calls."""
        self._calls += n

    def would_exceed(self, upcoming_calls: int) -> bool:
        """Whether running ``upcoming_calls`` more would breach the budget.

        Args:
            upcoming_calls: The number of calls the next cell would cost.

        Returns:
            ``True`` if a budget is set and the projected spend would exceed it.
        """
        if self.max_spend is None:
            return False
        projected = (self._calls + upcoming_calls) * self.cost_per_call
        return projected > self.max_spend


@dataclasses.dataclass
class Heartbeat:
    """Periodic liveness line for a long run (#280) — no silent multi-hour runs.

    Attributes:
        total_cells: Total cells the run will attempt.
        interval_seconds: Minimum wall-clock seconds between heartbeats.
        clock: Wall-clock source (injectable for tests).
        emit: Sink for the heartbeat line (defaults to a flushed ``print``).
    """

    total_cells: int
    interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    clock: Callable[[], float] = time.monotonic
    emit: Callable[[str], None] = lambda line: print(line, flush=True)
    _started: float = dataclasses.field(default=0.0, init=False)
    _last: float = dataclasses.field(default=0.0, init=False)

    def __post_init__(self) -> None:
        """Stamp the start time so elapsed is measured from construction."""
        self._started = self.clock()
        self._last = self._started

    def maybe_beat(self, done: int, guard: CostGuard, *, force: bool = False) -> None:
        """Emit a heartbeat if the interval elapsed (or ``force``).

        Args:
            done: Cells completed so far (incl. resumed).
            guard: The cost guard, for cumulative spend + call count.
            force: Emit regardless of the interval (run start / end).
        """
        now = self.clock()
        if not force and now - self._last < self.interval_seconds:
            return
        self._last = now
        elapsed = now - self._started
        self.emit(
            f"[heartbeat] elapsed={elapsed:.0f}s cells={done}/{self.total_cells} "
            f"calls={guard.calls} spend=${guard.spend:.2f}"
            + (f"/{guard.max_spend:.2f}" if guard.max_spend is not None else "")
        )


def cell_progress_line(
    cand: Candidate, replay: RoastReplay, n: int, total: int, cost_per_call: float
) -> str:
    """Render the per-cell progress line emitted as each cell completes.

    Args:
        cand: The candidate model.
        replay: The completed per-roast replay.
        n: This cell's 1-based position in the run.
        total: Total cells the run will attempt.
        cost_per_call: The run's configured USD-per-call rate — the SAME rate the
            :class:`CostGuard` accounts spend with, so the displayed per-cell cost
            never contradicts the budget math.

    Returns:
        The progress line.
    """
    score = replay.score
    f1 = score.drop.f1
    median = _phase_latency_str(score.phase_latency)
    cell_cost = round(replay.call_count * cost_per_call, 4)
    return (
        f"  [cell] {cand.slug} | {replay.roast_id} {n}/{total} | "
        f"drop F1={f1} | latency {median} | ~${cell_cost:.2f}"
    )


@dataclasses.dataclass
class ObservableRunResult:
    """Outcome of an observable, checkpointed replay run (#280, #284).

    Attributes:
        availability: The availability-sweep results.
        cells: The assembled :class:`ReplayCell` list (resumed + fresh).
        stopped_for_budget: ``True`` if the run stopped early on ``--max-spend``;
            the cells are then a valid PARTIAL scorecard.
        resumed_cells: How many per-roast cells were skipped (loaded from disk).
        fresh_cells: How many per-roast cells were computed this run.
        accounted_calls: The cost guard's final ACTUAL provider-request count —
            resumed cells' recorded calls plus every fresh request including
            ``with_retry`` retries (not one-per-tick), the basis for the spend
            estimate (#281).
        captured_calls: Every full per-call audit record from this run (#284) —
            resumed cells' capture is reloaded from the capture file, fresh
            cells' is captured live, so the set is complete across a resume.
    """

    availability: list[AvailabilityResult]
    cells: list[ReplayCell]
    stopped_for_budget: bool
    resumed_cells: int
    fresh_cells: int
    accounted_calls: int = 0
    captured_calls: list[CapturedCall] = dataclasses.field(
        default_factory=lambda: cast("list[CapturedCall]", [])
    )


async def _run_cells_bounded(
    pending: list[_PendingCell],
    run_one: Callable[[_PendingCell], Awaitable[None]],
    concurrency: int,
    is_stopped: Callable[[], bool],
) -> None:
    """Run pending cells concurrently behind a bounded worker pool (#281).

    A fixed pool of ``min(concurrency, MAX_CONCURRENCY, len(pending))`` workers
    pulls cells from a shared index — never more than ``concurrency`` cells are
    in flight, so the fan-out is always bounded (no unbounded ``gather`` over the
    whole grid). Once the budget trips (``is_stopped`` returns ``True``) the
    workers stop pulling new cells, so a budget stop schedules no further work.

    Args:
        pending: The fresh cells to run, in scheduling order.
        run_one: The per-cell coroutine (budget-gated, persists + reports).
        concurrency: The requested in-flight cap (clamped to ``MAX_CONCURRENCY``).
        is_stopped: Predicate that returns ``True`` once the budget has tripped.
    """
    cap = min(max(1, concurrency), MAX_CONCURRENCY)
    queue: asyncio.Queue[_PendingCell] = asyncio.Queue()
    for cell in pending:
        queue.put_nowait(cell)

    async def worker() -> None:
        while not is_stopped():
            try:
                cell = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await run_one(cell)

    await asyncio.gather(*(worker() for _ in range(min(cap, len(pending) or 1))))


@dataclasses.dataclass(frozen=True)
class _PendingCell:
    """A fresh ``(candidate, prompt, roast)`` cell scheduled for this run.

    Carries everything a worker needs so the scheduling order (the serial grid)
    is decided once, up front, and the workers stay identical whether they run
    serially or concurrently.

    Attributes:
        cand: The candidate model.
        prompt_version: The prompt version under test.
        rid: The roast id.
        key: The checkpoint key triple.
        ticks: The reconstructed ticks for the roast.
        ground: The roast's ground truth.
    """

    cand: Candidate
    prompt_version: str
    rid: str
    key: CellKey
    ticks: list[ReplayTick]
    ground: GroundTruth


async def run_replay_bakeoff_observable(  # noqa: PLR0915 — one orchestration unit
    roster: tuple[Candidate, ...],
    roasts: tuple[Path, ...],
    prompt_versions: list[str],
    reasoning: ReasoningEffort | None,
    cadence_seconds: float,
    *,
    out: Path,
    resume: bool = True,
    cost_per_call: float = DEFAULT_COST_PER_CALL_USD,
    max_spend: float | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    concurrency: int = 1,
    retry_policy: RetryPolicy | None = None,
    retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    recommender_factory: Callable[
        [Candidate, str], Callable[[AdvisorContext], Awaitable[RoastDecision]]
    ]
    | None = None,
    reasoning_recommender_factory: Callable[[Candidate, str], ReasoningRecommender] | None = None,
    clock: Callable[[], float] | None = None,
    heartbeat_clock: Callable[[], float] | None = None,
) -> ObservableRunResult:
    """Run the replay bake-off with observability, checkpointing, and a cost guard.

    The observable counterpart to :func:`run_replay_bakeoff`. Each
    ``(model_slug, prompt_version, roast_id)`` cell is appended to the sidecar
    immediately on completion, a progress line is printed, and a periodic
    cumulative-cost heartbeat is emitted. On start it loads the sidecar and SKIPS
    already-complete cells (resume). The scoring math is unchanged — cells are
    derived by the same pure scorers, fresh or reloaded.

    **Concurrency (#281).** With ``concurrency == 1`` (the default) the fresh
    cells run strictly serially, byte-identical to the original behaviour. With
    ``concurrency > 1`` the independent cells run concurrently behind a bounded
    worker pool (never an unbounded fan-out); the budget check is made atomic via
    :class:`_ConcurrentBudget`, which counts ACTUAL provider requests (including
    ``with_retry`` retries, not one-per-tick), so the running total overshoots
    ``--max-spend`` only by the request counts of the cells already in flight
    when the cap was hit. Each cell's per-roast replays are assembled in roast
    order (slotted by roast index), so concurrent completion order never reorders
    the scorecard. Within a single cell the ticks stay serial, so per-call
    latency is still measured per request; provider-side queueing under high
    concurrency can inflate that latency, so the latency gate is authoritative at
    low concurrency (see #281).

    **Backoff (#281).** When ``retry_policy`` is given each recommender call is
    wrapped with :func:`with_retry`, retrying transient ``429`` / ``5xx`` /
    network failures with exponential backoff honouring ``Retry-After``; a
    non-retryable error still records a failed outcome exactly as before.

    Alongside the scoring checkpoint it ALSO persists a full per-call capture
    (#284) to its own gitignored capture file, with the same incremental-flush +
    resume discipline; a resumed cell's capture is reloaded from disk.

    Args:
        roster: The candidate roster.
        roasts: The replay roast fixtures (the known-good test set).
        prompt_versions: Prompt versions to compare (e.g. ``["v2", "v3"]``).
        reasoning: Reasoning effort, or ``None`` for the provider default.
        cadence_seconds: Roast-time spacing between scored ticks.
        out: The ``--out`` JSON path; the sidecar + capture are derived from it.
        resume: Load + skip already-complete cells when ``True`` (default).
        cost_per_call: Estimated USD per recommender call (cost guard basis).
        max_spend: Optional USD budget; the run stops gracefully before breaching
            it, flushing partials.
        heartbeat_seconds: Minimum wall-clock seconds between heartbeats.
        concurrency: Maximum cells in flight (>= 1). ``1`` is the serial default;
            higher values fan out behind a bounded semaphore.
        retry_policy: When given, wrap each recommender with bounded retry +
            backoff for transient failures; ``None`` disables retry (the
            historical default the existing tests rely on).
        retry_sleep: Async sleep used by the retry backoff (injectable so tests
            stay instant); defaults to :func:`asyncio.sleep`.
        recommender_factory: Builds a plain ``RoastDecision`` recommender for a
            (candidate, prompt) cell. Back-compat seam: when given (and no
            reasoning factory is), its decisions are captured with ``None``
            reasoning. Tests inject a canned recommender (key-free seam).
        reasoning_recommender_factory: Builds a reasoning-aware recommender
            (returns ``(decision, reasoning)``) for a cell. Takes precedence over
            ``recommender_factory``; defaults to the real
            :class:`PydanticAIAdvisor` reasoning path when neither is given.
        clock: Monotonic clock for per-tick latency; defaults to
            ``time.perf_counter``.
        heartbeat_clock: Wall-clock for the heartbeat; defaults to
            ``time.monotonic``.

    Returns:
        The :class:`ObservableRunResult` (availability + assembled cells + the
        budget-stop / resume accounting + the full per-call capture).
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    survivors, availability = await availability_sweep(roster, prompt_versions[0], reasoning)
    print(render_availability(availability), flush=True)
    print("", flush=True)

    capture_factory = _resolve_capture_factory(
        reasoning_recommender_factory, recommender_factory, reasoning
    )
    checkpoint = Checkpoint(sidecar_path(out), resume=resume)
    capture = CaptureWriter(capture_path(out), resume=resume)
    guard = CostGuard(cost_per_call, max_spend)
    budget = _ConcurrentBudget(guard)
    total_cells = len(prompt_versions) * len(survivors) * len(roasts)
    hb_clock = heartbeat_clock if heartbeat_clock is not None else time.monotonic
    heartbeat = Heartbeat(
        total_cells=total_cells, interval_seconds=heartbeat_seconds, clock=hb_clock
    )
    tick_clock = clock if clock is not None else time.perf_counter

    # Pre-build ticks + ground once per roast (deterministic, no model calls);
    # both fresh runs and reloads reuse them, so resume needs no model access.
    built = {roast_id_for(f): build_ticks(f, cadence_seconds=cadence_seconds) for f in roasts}

    # One recommender per (candidate, prompt), built once and reused across that
    # cell's roasts — and wrapped with retry/backoff when a policy is configured.
    recommenders: dict[tuple[str, str], ReasoningRecommender] = {}

    def recommender_for(cand: Candidate, pv: str) -> ReasoningRecommender:
        cache_key = (cand.slug, pv)
        cached = recommenders.get(cache_key)
        if cached is not None:
            return cached
        base = capture_factory(cand, pv)
        # When retrying, every provider request (incl. retries) is counted via the
        # budget's per-attempt hook, and the cell's per-tick reservation is backed
        # out after the run, so the cost guard tracks ACTUAL requests not ticks.
        wrapped = (
            base
            if retry_policy is None
            else with_retry(
                base, retry_policy, sleep=retry_sleep, on_attempt=budget.account_attempt
            )
        )
        recommenders[cache_key] = wrapped
        return wrapped

    # The roast's stable grid index, so each cell's per-roast replays can be
    # assembled in ROAST ORDER regardless of the order they COMPLETE in under
    # concurrency (completions interleave; the scorecard must not).
    roast_index = {roast_id_for(f): i for i, f in enumerate(roasts)}

    # Walk the grid once IN ORDER: resumed cells are reloaded + accounted here
    # (so the resume count + cost is scoped to this run's grid, unchanged), and
    # the remaining fresh cells are collected as a schedule the workers consume.
    # Each cell's replays are stored in a roast-index-keyed slot map, never an
    # append list, so concurrent completion order cannot reorder them (#281).
    resumed = 0
    pending: list[_PendingCell] = []
    replays_by_cell: dict[tuple[str, str], dict[int, RoastReplay]] = {}
    for pv in prompt_versions:
        for cand in survivors:
            for fixture in roasts:
                rid = roast_id_for(fixture)
                key = cell_key(cand.slug, pv, rid)
                ticks, ground = built[rid]
                if checkpoint.has(key):
                    replay = roast_replay_from_record(checkpoint.record(key), ticks, ground)
                    replays_by_cell.setdefault((cand.slug, pv), {})[roast_index[rid]] = replay
                    resumed += 1
                    guard.add_calls(replay.call_count)
                    continue
                pending.append(_PendingCell(cand, pv, rid, key, ticks, ground))

    if resumed:
        print(f"resume: {resumed}/{total_cells} cells already on disk — skipping them", flush=True)

    done = resumed
    fresh = 0
    stopped = False
    write_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    heartbeat.maybe_beat(done=resumed, guard=guard, force=True)

    async def run_one(pending_cell: _PendingCell) -> None:
        """Run, persist, and report one fresh cell (budget-gated, kill-safe)."""
        nonlocal done, fresh, stopped
        cand, pv, rid = pending_cell.cand, pending_cell.prompt_version, pending_cell.rid
        ticks, ground = pending_cell.ticks, pending_cell.ground

        # Atomic budget reservation BEFORE paying for the cell (the per-tick
        # floor): once the budget is reached no further cell is scheduled. Actual
        # provider requests — incl. retries — are counted per attempt during the
        # run, so the running total overshoots only by the (retry-inflated)
        # request counts of the cells already in flight when the cap was hit.
        if not await budget.try_reserve(len(ticks)):
            if not stopped:
                stopped = True
                async with progress_lock:
                    print(
                        f"[budget] stopping gracefully before {cand.slug}/{pv}/{rid}: "
                        f"running it (~{len(ticks)} calls) would exceed --max-spend "
                        f"${max_spend:.2f} (spent ~${guard.spend:.2f} over {guard.calls} "
                        f"calls). {done} cells complete and flushed to {checkpoint.path}.",
                        flush=True,
                    )
            return

        try:
            outcomes, reasonings = await replay_roast_with_capture(
                ticks, recommender_for(cand, pv), clock=tick_clock
            )
        finally:
            # With retry counting on, every request (incl. retries) was counted
            # by ``account_attempt``; back out the per-tick reservation so each
            # request is counted exactly once. Without a policy there is no
            # per-attempt counting, so the reservation IS the accounting — leave
            # it. In a ``finally`` so a mid-cell raise cannot leak the reservation.
            if retry_policy is not None:
                budget.release_reserved(len(ticks))
        replay = build_roast_replay(cand.slug, pv, rid, outcomes, ground)
        # Persist immediately — kill-safe. The shared sidecar + capture files are
        # written under one lock so concurrent cells never interleave a line.
        async with write_lock:
            checkpoint.append(replay)
            capture.append(
                build_captured_calls(cand.slug, pv, rid, ticks, outcomes, reasonings, cost_per_call)
            )
            # Slot by roast index — completion order is irrelevant to assembly.
            replays_by_cell.setdefault((cand.slug, pv), {})[roast_index[rid]] = replay
        async with progress_lock:
            done += 1
            fresh += 1
            print(
                cell_progress_line(cand, replay, done, total_cells, guard.cost_per_call),
                flush=True,
            )
            heartbeat.maybe_beat(done=done, guard=guard)

    if concurrency == 1:
        # Strict serial path — byte-identical scheduling to the original loop, so
        # the default run stays behaviour-compatible.
        for pending_cell in pending:
            if stopped:
                break
            await run_one(pending_cell)
    else:
        await _run_cells_bounded(pending, run_one, concurrency, lambda: stopped)

    heartbeat.maybe_beat(done=done, guard=guard, force=True)

    # Assemble cells in (prompt, survivor) order, including partial cells (a cell
    # with only some roasts scored is still a valid, reportable partial). Each
    # cell's per-roast replays are emitted in ROAST-INDEX order (the slot map is
    # sorted by key), so concurrent completion order never reorders the scorecard.
    # The capture is assembled the same way so a resumed run's capture is complete.
    cells: list[ReplayCell] = []
    captured_calls: list[CapturedCall] = []
    for pv in prompt_versions:
        for cand in survivors:
            slots = replays_by_cell.get((cand.slug, pv))
            if slots:
                replays = [slots[i] for i in sorted(slots)]
                cells.append(_replays_to_cell(cand, pv, replays))
            for fixture in roasts:
                key = cell_key(cand.slug, pv, roast_id_for(fixture))
                if capture.has(key):
                    captured_calls.extend(capture.calls_for(key))
    return ObservableRunResult(
        availability=availability,
        cells=cells,
        stopped_for_budget=stopped,
        resumed_cells=resumed,
        fresh_cells=fresh,
        accounted_calls=guard.calls,
        captured_calls=captured_calls,
    )


def _resolve_capture_factory(
    reasoning_recommender_factory: Callable[[Candidate, str], ReasoningRecommender] | None,
    recommender_factory: Callable[
        [Candidate, str], Callable[[AdvisorContext], Awaitable[RoastDecision]]
    ]
    | None,
    reasoning: ReasoningEffort | None,
) -> Callable[[Candidate, str], ReasoningRecommender]:
    """Resolve which reasoning-aware recommender factory the run should use.

    Precedence: an explicit ``reasoning_recommender_factory`` wins; else a plain
    ``recommender_factory`` is adapted to return ``None`` reasoning (back-compat
    seam — its captures simply carry no reasoning); else the real
    :class:`PydanticAIAdvisor` reasoning path (spends credits).

    Args:
        reasoning_recommender_factory: An explicit reasoning-aware factory, or
            ``None``.
        recommender_factory: A plain-decision factory to adapt, or ``None``.
        reasoning: Reasoning effort for the real default factory.

    Returns:
        A factory mapping a (candidate, prompt) to a :data:`ReasoningRecommender`.
    """
    if reasoning_recommender_factory is not None:
        return reasoning_recommender_factory
    if recommender_factory is not None:
        plain = recommender_factory

        def adapted(cand: Candidate, pv: str) -> ReasoningRecommender:
            recommend = plain(cand, pv)

            async def with_reasoning(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
                return await recommend(context), None

            return with_reasoning

        return adapted
    return _real_reasoning_recommender_factory(reasoning)


def _real_reasoning_recommender_factory(
    reasoning: ReasoningEffort | None,
) -> Callable[[Candidate, str], ReasoningRecommender]:
    """Build the default real-OpenRouter reasoning-aware factory (spends credits).

    The default capture factory for :func:`run_replay_bakeoff_observable`. Calls
    :meth:`PydanticAIAdvisor.get_recommendation_with_reasoning` so the capture
    records the provider's reasoning trace where exposed. Tests inject a canned
    factory instead (the key-free seam), so this real path is never exercised by
    the test suite.

    Args:
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        A factory mapping a (candidate, prompt) to a timeout-bounded
        reasoning-aware recommender.
    """

    def factory(cand: Candidate, prompt_version: str) -> ReasoningRecommender:
        advisor = PydanticAIAdvisor(_make_config(cand.slug, prompt_version, reasoning))

        async def recommend(context: AdvisorContext) -> tuple[RoastDecision, str | None]:
            return await asyncio.wait_for(
                advisor.get_recommendation_with_reasoning(context), timeout=MEASURE_TIMEOUT
            )

        return recommend

    return factory


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
    """CLI entrypoint — runs the bake-off and writes the report(s).

    The default ``replay`` mode scores candidates tick-by-tick against the two
    known-good 7-Jun roasts (the quantitative layer); ``per-phase`` runs the
    lighter synthetic-moment latency/advice table. Both write a JSON artifact and
    print the report; neither auto-picks a model (D20).

    Returns:
        ``0`` on a completed run (operator-judged; a completed run is success
        regardless of which models passed the gate).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode",
        choices=["replay", "per-phase"],
        default="replay",
        help="replay = score vs the known-good roasts (default); per-phase = "
        "synthetic-moment latency/advice table",
    )
    parser.add_argument("--iterations", type=int, default=3, help="per-phase cell iters (D20 N=3)")
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
    parser.add_argument(
        "--cadence-seconds",
        type=float,
        default=DEFAULT_CADENCE_SECONDS,
        help="replay mode: roast-time spacing between scored ticks (default: 30)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="per-phase mode: the grounding live-roast fixture",
    )
    parser.add_argument("--out", type=Path, default=Path("/tmp/bakeoff.json"))
    parser.add_argument(
        "--report-md",
        type=Path,
        default=None,
        help="replay mode: also write the markdown scorecard here",
    )
    parser.add_argument(
        "--trajectory",
        action="store_true",
        help="replay mode: append the control-trajectory sanity section (#277) — "
        "command-signal coherence (change/reversal counts, control-signal entropy, "
        "momentum cuts) over development. Agreement-free; the JSON always carries it.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="replay mode: ignore + truncate any existing checkpoint sidecar and "
        "run every cell from scratch (default: resume — skip cells already on disk)",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=None,
        help="replay mode: optional USD budget; the run stops GRACEFULLY before a "
        "cell would breach it, flushing partial results and rendering the partial "
        "scorecard (no exception). Spend is estimated as calls x --cost-per-call.",
    )
    parser.add_argument(
        "--cost-per-call",
        type=float,
        default=DEFAULT_COST_PER_CALL_USD,
        help=f"replay mode: estimated USD per recommender call for the cost guard "
        f"(default: {DEFAULT_COST_PER_CALL_USD}; pydantic_ai exposes tokens, not a "
        f"billed dollar amount, so spend is an estimate)",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help=f"replay mode: minimum wall-clock seconds between cumulative-cost "
        f"heartbeats (default: {DEFAULT_HEARTBEAT_SECONDS:g})",
    )
    parser.add_argument(
        "--interest-top-n",
        type=int,
        default=DEFAULT_INTEREST_TOP_N,
        help=f"replay mode: how many calls to surface per 'most-interesting cells' "
        f"category from the per-call capture (#284) (default: {DEFAULT_INTEREST_TOP_N})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=f"replay mode: max independent (model, prompt, roast) cells in flight, "
        f"capped at {MAX_CONCURRENCY} (#281). 1 (default) = serial, behaviour-"
        f"compatible. Cells fan out behind a bounded semaphore; ticks within a cell "
        f"stay serial so per-call latency is measured per request. NOTE: provider-"
        f"side queueing under high concurrency can inflate latency, so run the "
        f"latency-gate pass at --concurrency 1 (the default) when the gate numbers "
        f"must be authoritative; use a higher value for the scoring pass.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=DEFAULT_RETRY_ATTEMPTS,
        help=f"replay mode: total attempts per call on a transient 429 / 5xx / "
        f"network failure, with exponential backoff honouring Retry-After (#281) "
        f"(default: {DEFAULT_RETRY_ATTEMPTS}; 1 disables retry)",
    )
    args = parser.parse_args()

    reasoning: ReasoningEffort | None = (
        None if args.reasoning == "default" else cast("ReasoningEffort", args.reasoning)
    )
    prompt_versions = cast("list[str]", args.prompt_version)

    if args.mode == "per-phase":
        availability, cells = await run_bakeoff(
            ROSTER, args.fixture, args.iterations, prompt_versions, reasoning
        )
        print("\n" + render_decision_table(cells), flush=True)
        args.out.write_text(
            json.dumps(
                {
                    "mode": "per-phase",
                    "availability": [dataclasses.asdict(a) for a in availability],
                    "cells": cells_to_json(cells),
                },
                indent=2,
            )
        )
        print(f"\nwrote artifact -> {args.out}", flush=True)
        return 0

    concurrency = max(1, min(int(args.concurrency), MAX_CONCURRENCY))
    retry_attempts = max(1, int(args.retry_attempts))
    # A real run always uses backoff; retry is a no-op when attempts == 1.
    retry_policy = RetryPolicy(attempts=retry_attempts)
    result = await run_replay_bakeoff_observable(
        ROSTER,
        REPLAY_ROASTS,
        prompt_versions,
        reasoning,
        args.cadence_seconds,
        out=args.out,
        resume=not bool(args.no_resume),
        cost_per_call=float(args.cost_per_call),
        max_spend=cast("float | None", args.max_spend),
        heartbeat_seconds=float(args.heartbeat_seconds),
        concurrency=concurrency,
        retry_policy=retry_policy,
    )
    availability, replay_cells = result.availability, result.cells
    interest_top_n = int(args.interest_top_n)
    selected = select_interesting_calls(result.captured_calls, top_n=interest_top_n)
    report = render_replay_report(replay_cells, REPLAY_ROASTS, trajectory=bool(args.trajectory))
    # Append the #284 surfacing so "why did model X do Y" is a lookup, not a
    # re-run. The full prompt + reasoning live in the gitignored capture file.
    report = report + "\n\n---\n\n" + render_interesting_calls(selected, top_n=interest_top_n)
    print("\n" + report, flush=True)
    args.out.write_text(
        json.dumps(
            {
                "mode": "replay",
                "stopped_for_budget": result.stopped_for_budget,
                "resumed_cells": result.resumed_cells,
                "fresh_cells": result.fresh_cells,
                "captured_calls": len(result.captured_calls),
                "capture_path": str(capture_path(args.out)),
                "reasoning_available_calls": sum(
                    1 for c in result.captured_calls if c.reasoning_available
                ),
                "availability": [dataclasses.asdict(a) for a in availability],
                "interesting_cells": interesting_cells_to_json(selected),
                "cells": replay_cells_to_json(replay_cells),
            },
            indent=2,
        )
    )
    print(f"\nwrote artifact -> {args.out}", flush=True)
    print(
        f"wrote per-call capture ({len(result.captured_calls)} calls, "
        f"gitignored) -> {capture_path(args.out)}",
        flush=True,
    )
    if args.report_md is not None:
        args.report_md.write_text(report)
        print(f"wrote markdown report -> {args.report_md}", flush=True)
    if result.stopped_for_budget:
        print(
            "NOTE: the run stopped early on --max-spend; the scorecard above is a "
            "PARTIAL over the completed cells. Re-run (resume is on) to finish the rest.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
