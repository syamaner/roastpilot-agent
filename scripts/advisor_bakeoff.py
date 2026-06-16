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

    Args:
        cand: The candidate model.
        prompt_version: The prompt version under test.
        replays: The per-roast replays for this (slug, prompt) cell, roast order.

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
    """Outcome of an observable, checkpointed replay run (#280).

    Attributes:
        availability: The availability-sweep results.
        cells: The assembled :class:`ReplayCell` list (resumed + fresh).
        stopped_for_budget: ``True`` if the run stopped early on ``--max-spend``;
            the cells are then a valid PARTIAL scorecard.
        resumed_cells: How many per-roast cells were skipped (loaded from disk).
        fresh_cells: How many per-roast cells were computed this run.
    """

    availability: list[AvailabilityResult]
    cells: list[ReplayCell]
    stopped_for_budget: bool
    resumed_cells: int
    fresh_cells: int


async def run_replay_bakeoff_observable(
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
    recommender_factory: Callable[
        [Candidate, str], Callable[[AdvisorContext], Awaitable[RoastDecision]]
    ]
    | None = None,
    clock: Callable[[], float] | None = None,
    heartbeat_clock: Callable[[], float] | None = None,
) -> ObservableRunResult:
    """Run the replay bake-off with observability, checkpointing, and a cost guard.

    The observable counterpart to :func:`run_replay_bakeoff`. It runs each
    ``(model_slug, prompt_version, roast_id)`` cell **serially** so progress,
    incremental persistence, and a graceful budget stop are deterministic; each
    completed cell is appended to the sidecar immediately and a progress line is
    printed, with a periodic cumulative-cost heartbeat. On start it loads the
    sidecar and SKIPS already-complete cells (resume). The scoring math is
    unchanged — cells are derived by the same pure scorers, fresh or reloaded.

    Args:
        roster: The candidate roster.
        roasts: The replay roast fixtures (the known-good test set).
        prompt_versions: Prompt versions to compare (e.g. ``["v2", "v3"]``).
        reasoning: Reasoning effort, or ``None`` for the provider default.
        cadence_seconds: Roast-time spacing between scored ticks.
        out: The ``--out`` JSON path; the sidecar is derived from it.
        resume: Load + skip already-complete cells when ``True`` (default).
        cost_per_call: Estimated USD per recommender call (cost guard basis).
        max_spend: Optional USD budget; the run stops gracefully before breaching
            it, flushing partials.
        heartbeat_seconds: Minimum wall-clock seconds between heartbeats.
        recommender_factory: Builds the async recommender for a (candidate,
            prompt) cell. Defaults to the real :class:`PydanticAIAdvisor`; tests
            inject a canned recommender (key-free seam).
        clock: Monotonic clock for per-tick latency; defaults to
            ``time.perf_counter``.
        heartbeat_clock: Wall-clock for the heartbeat; defaults to
            ``time.monotonic``.

    Returns:
        The :class:`ObservableRunResult` (availability + assembled cells + the
        budget-stop / resume accounting).
    """
    survivors, availability = await availability_sweep(roster, prompt_versions[0], reasoning)
    print(render_availability(availability), flush=True)
    print("", flush=True)

    factory = (
        recommender_factory
        if recommender_factory is not None
        else _real_recommender_factory(reasoning)
    )
    checkpoint = Checkpoint(sidecar_path(out), resume=resume)
    guard = CostGuard(cost_per_call, max_spend)
    total_cells = len(prompt_versions) * len(survivors) * len(roasts)
    hb_clock = heartbeat_clock if heartbeat_clock is not None else time.monotonic
    heartbeat = Heartbeat(
        total_cells=total_cells, interval_seconds=heartbeat_seconds, clock=hb_clock
    )

    # Pre-build ticks + ground once per roast (deterministic, no model calls);
    # both fresh runs and reloads reuse them, so resume needs no model access.
    built = {roast_id_for(f): build_ticks(f, cadence_seconds=cadence_seconds) for f in roasts}

    # Resumed cells already on disk: count + account ONLY the cells that belong
    # to THIS run's (survivors x prompts x roasts) grid. A sidecar carrying
    # entries from a different roster / prompt set must not inflate the resumed
    # count or the cost — only the current run's keys are skipped below, so the
    # count must be scoped the same way to stay consistent with that skip logic.
    resumed = 0
    for pv in prompt_versions:
        for c in survivors:
            for f in roasts:
                key = cell_key(c.slug, pv, roast_id_for(f))
                if not checkpoint.has(key):
                    continue
                resumed += 1
                guard.add_calls(int(checkpoint.record(key)["call_count"]))

    if resumed:
        print(f"resume: {resumed}/{total_cells} cells already on disk — skipping them", flush=True)

    replays_by_cell: dict[tuple[str, str], list[RoastReplay]] = {}
    done = 0
    fresh = 0
    stopped = False
    heartbeat.maybe_beat(done=resumed, guard=guard, force=True)

    for pv in prompt_versions:
        for cand in survivors:
            recommend = factory(cand, pv)
            for fixture in roasts:
                rid = roast_id_for(fixture)
                key = cell_key(cand.slug, pv, rid)
                ticks, ground = built[rid]

                if checkpoint.has(key):
                    replay = roast_replay_from_record(checkpoint.record(key), ticks, ground)
                    replays_by_cell.setdefault((cand.slug, pv), []).append(replay)
                    done += 1
                    continue

                # Cost guard: decide BEFORE paying for the cell so a stop flushes
                # partials and exits cleanly (never mid-call).
                if guard.would_exceed(len(ticks)):
                    print(
                        f"[budget] stopping gracefully before {cand.slug}/{pv}/{rid}: "
                        f"running it (~{len(ticks)} calls) would exceed --max-spend "
                        f"${max_spend:.2f} (spent ~${guard.spend:.2f} over {guard.calls} calls). "
                        f"{done} cells complete and flushed to {checkpoint.path}.",
                        flush=True,
                    )
                    stopped = True
                    break

                outcomes = await replay_roast(
                    ticks, recommend, clock=clock if clock is not None else time.perf_counter
                )
                replay = build_roast_replay(cand.slug, pv, rid, outcomes, ground)
                checkpoint.append(replay)  # persist immediately — kill-safe
                guard.add_calls(replay.call_count)
                replays_by_cell.setdefault((cand.slug, pv), []).append(replay)
                done += 1
                fresh += 1
                print(
                    cell_progress_line(cand, replay, done, total_cells, guard.cost_per_call),
                    flush=True,
                )
                heartbeat.maybe_beat(done=done, guard=guard)
            if stopped:
                break
        if stopped:
            break

    heartbeat.maybe_beat(done=done, guard=guard, force=True)

    # Assemble cells in (prompt, survivor) order, including partial cells (a cell
    # with only some roasts scored is still a valid, reportable partial).
    cells: list[ReplayCell] = []
    for pv in prompt_versions:
        for cand in survivors:
            replays = replays_by_cell.get((cand.slug, pv))
            if replays:
                cells.append(_replays_to_cell(cand, pv, replays))
    return ObservableRunResult(
        availability=availability,
        cells=cells,
        stopped_for_budget=stopped,
        resumed_cells=resumed,
        fresh_cells=fresh,
    )


def _real_recommender_factory(
    reasoning: ReasoningEffort | None,
) -> Callable[[Candidate, str], Callable[[AdvisorContext], Awaitable[RoastDecision]]]:
    """Build the default real-OpenRouter recommender factory (spends credits).

    The default ``recommender_factory`` for
    :func:`run_replay_bakeoff_observable`. Tests inject a canned recommender
    factory instead (the key-free seam), so this real path is never exercised by
    the test suite.

    Args:
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        A factory mapping a (candidate, prompt) to a timeout-bounded recommender.
    """

    def factory(
        cand: Candidate, prompt_version: str
    ) -> Callable[[AdvisorContext], Awaitable[RoastDecision]]:
        advisor = PydanticAIAdvisor(_make_config(cand.slug, prompt_version, reasoning))

        async def recommend(context: AdvisorContext) -> RoastDecision:
            return await asyncio.wait_for(
                advisor.get_recommendation(context), timeout=MEASURE_TIMEOUT
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
    )
    availability, replay_cells = result.availability, result.cells
    report = render_replay_report(replay_cells, REPLAY_ROASTS, trajectory=bool(args.trajectory))
    print("\n" + report, flush=True)
    args.out.write_text(
        json.dumps(
            {
                "mode": "replay",
                "stopped_for_budget": result.stopped_for_budget,
                "resumed_cells": result.resumed_cells,
                "fresh_cells": result.fresh_cells,
                "availability": [dataclasses.asdict(a) for a in availability],
                "cells": replay_cells_to_json(replay_cells),
            },
            indent=2,
        )
    )
    print(f"\nwrote artifact -> {args.out}", flush=True)
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
