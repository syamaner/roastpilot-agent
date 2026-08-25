"""#567 three-arm same-bean reference-curve bake-off (design note §4 / §6.4).

Measures whether a same-bean **reference roast** (a completed, well-rated PAST
roast of the identical bean, #567 Slice A/B) changes the advisor's post-FC
DEVELOPMENT decisions, and whether that change is driven by the raw data being
present or by the c9 prose that TEACHES the model to read it. Three arms per
held-out decision tick, one model (default ``openai/gpt-4o``, ``--model``):

- **Arm 1 — c8, no reference.** ``reference_curve=[]``, ``reference_landmarks=None``
  — the AS-BUILT c8 control teaching prompt with nothing injected.
- **Arm 2 — c8, reference present but UNTAUGHT.** The retrieved reference is
  injected into the context, but the prompt stays c8, whose text never
  mentions ``reference_curve``/``reference_landmarks`` — isolates the RAW
  DATA's marginal effect (arm 2 vs arm 1).
- **Arm 3 — c9, reference present AND taught.** Same injected reference, c9
  (c8 plus the #567 reference-curve teaching section). Isolates the
  TEACHING's marginal effect (arm 3 vs arm 2).

Held-out runs are every completed + rated roast belonging to a same-bean group
of at least ``--min-group-size`` (default 2) qualifying runs in the real
operator store — the design's three verified same-bean groups (Guatemala El
Durazno, Colombia Excelso Huila, Sumatra Mandheling G1) as of 17 Jul 2026. For
each held-out run ``R``, the reference is the best-rated OTHER completed run of
the same bean: :meth:`~roastpilot_agent.store.RoastStore._ranked_reference_run_ids`
is called directly and ``R``'s own id is filtered out of the ranked candidate
list before the first buildable reference
(:meth:`~roastpilot_agent.store.RoastStore._build_reference_roast`) is taken —
mirroring :meth:`~roastpilot_agent.store.RoastStore.load_reference_roast`'s
fallback, but with ``R`` excluded from its own reference pool. ``--run-ids``
overrides discovery with an explicit subset (e.g. to trim a paid run's cost).

Each held-out run is exported to a temporary replay fixture
(:func:`store_to_fixture.convert`, the store→fixture adapter #300 already
proved) and reconstructed into post-FC DEVELOPMENT-only advisor-context ticks
via the SAME machinery ``advisor_bakeoff`` uses for the #277 bake-off
(:func:`bakeoff_replay.build_ticks` + ``advisor_bakeoff.enrich_ticks_with_
control_context`` + ``advisor_bakeoff.development_only`` — the D35 post-FC
advisor scope). Arm 2/3's reference is spliced into each tick's
:class:`~roastpilot_agent.advisor.AdvisorContext` via ``model_copy(update=...)``,
the same seam ``advisor_bakeoff`` already uses for the #273/#275 control
context.

**Manual / local only — spends real OpenRouter credits in the default mode.**
Reads ``OPENROUTER_API_KEY`` from the environment at run (same path as
``advisor_bakeoff.py`` / ``advisor_smoke.py``); the key never enters config,
the repo, or any artifact this script writes. ``--dry-run`` replaces the real
:class:`~roastpilot_agent.advisor.PydanticAIAdvisor` with a deterministic,
network-free :class:`DryRunAdvisor` so the whole three-arm wiring — discovery,
self-exclusion, fixture reconstruction, reference injection, replay, resume,
the cost guard, and the report — is provable at zero spend.

Reads the real operator SQLite store (default ``~/roasts/roastpilot.sqlite3``,
``--store``), but NEVER opens that file itself:
:func:`store_snapshot.snapshot_store_to_temp` (the shared helper every
offline store-reading script uses, #726) backs it up (via SQLite's own online
backup API, against a strictly ``mode=ro`` source connection) to a private
temp copy first, and every :class:`~roastpilot_agent.store.RoastStore` call in
this script — ``list_runs`` / ``read_run`` / the private reference-retrieval
methods — operates on THAT copy. ``RoastStore.initialize()`` opens read-write
and applies WAL/migrations (the normal, safe thing for the live agent to do to
its own store), so this isolation is what keeps the operator's live database
untouched even though this script only ever calls read methods on the
(temp-copy) store.

Exact operator run command (paid, real model calls)::

    OPENROUTER_API_KEY=sk-or-... \\
    python scripts/bakeoff_reference_567.py \\
        --max-spend 5 \\
        --out /tmp/bakeoff-reference-567.json \\
        --report-md /tmp/bakeoff-reference-567.md

Zero-spend wiring proof (no key needed, no network)::

    python scripts/bakeoff_reference_567.py --dry-run \\
        --out /tmp/bakeoff-reference-567-dryrun.json \\
        --report-md /tmp/bakeoff-reference-567-dryrun.md

**Long-run observability + recovery.** Mirrors ``advisor_bakeoff``'s #280
discipline at a smaller scale: each completed ``(run_id, arm)`` cell is
appended to a ``<out>.cells.jsonl`` sidecar immediately, and a re-run with the
SAME ``--out`` RESUMES by skipping cells already on disk (``--no-resume`` to
force a clean run). ``--max-spend`` stops the run gracefully — before a cell
that would breach the budget starts — flushing whatever cells completed and
rendering a partial report; the run has SPENT NOTHING on the cell it declined
to start.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))  # bakeoff_replay, store_to_fixture
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from advisor_bakeoff import development_only, enrich_ticks_with_control_context  # noqa: E402
from bakeoff_replay import (  # noqa: E402
    ReplayTick,
    TickOutcome,
    build_ticks,
    replay_roast,
)
from store_snapshot import snapshot_store_to_temp  # noqa: E402
from store_to_fixture import FixtureConversionError  # noqa: E402
from store_to_fixture import convert as convert_store_run

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
    AdvisorDescriptor,
    PydanticAIAdvisor,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import AdvisorConfig  # noqa: E402
from roastpilot_agent.models import (  # noqa: E402
    AdvisorHealth,
    AdvisorHealthStatus,
    ReferenceRoast,
    RoastPhase,
    recording_origin_slug,
)
from roastpilot_agent.store import RoastStore  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1"

#: Default model under test — the #277 pin (proven n8n baseline, D40.4).
DEFAULT_MODEL = "openai/gpt-4o"

#: Roast-time spacing between scored ticks (mirrors advisor_bakeoff's default).
DEFAULT_CADENCE_SECONDS = 30.0

#: Estimated USD per recommender call for the cost guard (mirrors
#: advisor_bakeoff's ``DEFAULT_COST_PER_CALL_USD`` — pydantic_ai exposes token
#: usage, not a billed dollar amount, so this is an estimate).
DEFAULT_COST_PER_CALL_USD = 0.02

#: Bound on one real provider call (Codex PR #578 round 3 finding: the live
#: controller and ``advisor_bakeoff.py`` both wrap ``get_recommendation`` in
#: an explicit timeout; this script's real path did not, so a hung
#: OpenRouter/model call could hang the whole paid run indefinitely). Mirrors
#: ``advisor_bakeoff.MEASURE_TIMEOUT`` exactly — a generous bound (not the
#: live 10 s tick gate) so slow-but-alive advice is still captured as a
#: recorded tick error rather than genuinely wedging the process.
MEASURE_TIMEOUT_SECONDS = 90.0

#: The design's quality floor for a CANDIDATE reference roast (design note
#: §1.2; matches ``RoastStore.find_reference_run``'s / ``load_reference_roast``'s
#: own default).
DEFAULT_REFERENCE_MIN_RATING = 3

#: The design's charge-weight tolerance band for a candidate reference
#: (matches the store methods' own default).
DEFAULT_WEIGHT_TOLERANCE_FRAC = 0.10

#: Minimum same-bean group size (completed + rated runs sharing a
#: ``recording_origin_slug``) for its runs to be auto-discovered as held-out —
#: a group of 1 has no OTHER run to serve as its own reference, so it can
#: never exercise arm 2/3's injection and is skipped by default.
DEFAULT_MIN_GROUP_SIZE = 2

ReasoningEffort = Literal["off", "minimal", "low", "medium", "high"]


# --- Arm definitions (design note §6.4) --------------------------------------


@dataclasses.dataclass(frozen=True)
class ArmSpec:
    """One of the three #567 bake-off arms.

    Attributes:
        key: Stable identifier used in the checkpoint/report/JSON (never a
            display string — renamed labels must not break resume).
        label: Human-readable label for the report.
        prompt_version: The control teaching prompt version (``c8`` or ``c9``).
        inject_reference: Whether the retrieved same-bean reference is spliced
            into each tick's context for this arm.
    """

    key: str
    label: str
    prompt_version: str
    inject_reference: bool


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        key="arm1_no_reference",
        label="Arm 1 — c8, no reference",
        prompt_version="c8",
        inject_reference=False,
    ),
    ArmSpec(
        key="arm2_reference_untaught",
        label="Arm 2 — c8, reference present (untaught)",
        prompt_version="c8",
        inject_reference=True,
    ),
    ArmSpec(
        key="arm3_reference_taught",
        label="Arm 3 — c9, reference present + taught",
        prompt_version="c9",
        inject_reference=True,
    ),
)


# --- Held-out run discovery + self-excluded reference retrieval --------------


@dataclasses.dataclass(frozen=True)
class HeldOutMeta:
    """One held-out roast to replay through all three arms.

    Attributes:
        run_id: The ``roast_runs.id`` of the held-out roast.
        origin_slug: The :func:`recording_origin_slug` derived from its frozen
            profile — the same-bean identity key.
        bean_label: The frozen profile's ``name`` (report display only).
        operator_rating: The held-out roast's own 1-5 rating.
        charge_grams: The effective charge weight (corrected when present,
            else the frozen default) — the reference retrieval's tolerance
            anchor.
        started_at_utc: The held-out roast's ``roast_runs.started_at_utc`` —
            the look-ahead guard's anchor (Codex PR #578 finding): a reference
            candidate must have COMPLETED strictly before this instant.
    """

    run_id: str
    origin_slug: str
    bean_label: str
    operator_rating: int
    charge_grams: float
    started_at_utc: str


async def _meta_for_run(store: RoastStore, run_id: str) -> HeldOutMeta | None:
    """Resolve one run's held-out metadata, or ``None`` if it cannot qualify.

    A run cannot qualify when it does not exist, was never rated, was not
    completed (``outcome != "completed"`` — the same gate
    :func:`discover_held_out_runs` applies via its own SQL/list filter, now
    also enforced here so an explicit ``--run-ids`` cannot smuggle in an
    aborted/faulted run the auto-discovery path would have rejected), or its
    frozen profile yields no :func:`recording_origin_slug` (no usable bean
    identity text at all — the same guard the store's own reference-retrieval
    query applies).

    Args:
        store: The open store.
        run_id: The candidate ``roast_runs.id``.

    Returns:
        The resolved :class:`HeldOutMeta`, or ``None``.
    """
    detail = await store.read_run(run_id)
    if detail is None or detail.rating is None or detail.outcome != "completed":
        return None
    origin_slug = recording_origin_slug(detail.profile)
    if origin_slug is None:
        return None
    charge_grams = (
        detail.corrected_charge_grams
        if detail.corrected_charge_grams is not None
        else detail.profile.bean_weight_grams
    )
    return HeldOutMeta(
        run_id=detail.id,
        origin_slug=origin_slug,
        bean_label=detail.profile.name,
        operator_rating=detail.rating,
        charge_grams=charge_grams,
        started_at_utc=detail.started_at_utc,
    )


async def discover_held_out_runs(store: RoastStore, *, min_group_size: int) -> list[HeldOutMeta]:
    """Auto-discover held-out runs: every completed+rated run in a same-bean group.

    A "group" is every completed, rated run sharing a
    :func:`recording_origin_slug`; only groups with at least
    ``min_group_size`` members are kept (a singleton bean has no OTHER run to
    serve as its own reference, so #567 cannot be exercised for it).

    Args:
        store: The open store.
        min_group_size: Minimum same-bean group size to include.

    Returns:
        Every qualifying run's :class:`HeldOutMeta`, sorted by
        ``(origin_slug, -operator_rating, run_id)`` for a reproducible report
        order.
    """
    summaries = await store.list_runs()
    candidates = [s for s in summaries if s.outcome == "completed" and s.rating is not None]
    metas: list[HeldOutMeta] = []
    for summary in candidates:
        meta = await _meta_for_run(store, summary.id)
        if meta is not None:
            metas.append(meta)
    groups: dict[str, list[HeldOutMeta]] = {}
    for meta in metas:
        groups.setdefault(meta.origin_slug, []).append(meta)
    selected = [
        meta for members in groups.values() if len(members) >= min_group_size for meta in members
    ]
    selected.sort(key=lambda m: (m.origin_slug, -m.operator_rating, m.run_id))
    return selected


async def resolve_explicit_runs(store: RoastStore, run_ids: list[str]) -> list[HeldOutMeta]:
    """Resolve an explicit ``--run-ids`` subset, bypassing the group-size filter.

    Args:
        store: The open store.
        run_ids: The explicit run ids the operator supplied.

    Returns:
        The resolved :class:`HeldOutMeta` list, in the given order.

    Raises:
        ValueError: If a given id is not a completed, rated run with a
            resolvable bean identity.
    """
    metas: list[HeldOutMeta] = []
    for run_id in run_ids:
        meta = await _meta_for_run(store, run_id)
        if meta is None:
            raise ValueError(
                f"--run-ids: {run_id!r} is not a completed, rated run with a "
                f"resolvable bean identity"
            )
        metas.append(meta)
    return metas


async def find_self_excluded_reference(
    store: RoastStore,
    meta: HeldOutMeta,
    *,
    min_rating: int,
    weight_tolerance_frac: float,
) -> ReferenceRoast | None:
    """The best USABLE, TEMPORALLY-PRIOR reference for ``meta``, self-excluded.

    Calls :meth:`~roastpilot_agent.store.RoastStore._ranked_reference_run_ids`
    directly (best-first) and filters ``meta.run_id`` out of the ranked list
    before taking the first candidate
    :meth:`~roastpilot_agent.store.RoastStore._build_reference_roast` can
    actually build — the same fall-through
    :meth:`~roastpilot_agent.store.RoastStore.load_reference_roast` performs,
    with the held-out run additionally excluded from its own reference pool
    (the public methods have no such exclusion — a held-out run's own frozen
    telemetry would otherwise legitimately outrank every other same-bean run,
    defeating the whole comparison).

    A candidate must also have COMPLETED strictly before ``meta`` STARTED
    (Codex PR #578 finding): excluding only ``meta.run_id`` still lets a
    temporally LATER same-bean roast stand in as "the reference", which the
    live agent could never have retrieved for that earlier roast — a
    look-ahead leak. The candidate's own ``completed_at_utc`` is the anchor
    (falling back to ``started_at_utc`` on the rare pre-completion-timestamp
    row); a candidate whose timestamp cannot be read at all is skipped rather
    than assumed prior.

    Args:
        store: The open store.
        meta: The held-out run to find a reference FOR (excluded from the
            candidate pool, and the temporal cutoff every candidate must
            precede).
        min_rating: The reference candidate quality floor.
        weight_tolerance_frac: The charge-weight tolerance band.

    Returns:
        The best usable, temporally-prior
        :class:`~roastpilot_agent.models.ReferenceRoast`, or ``None`` when no
        other qualifying, buildable, prior run exists.
    """
    ranked = await store._ranked_reference_run_ids(  # pyright: ignore[reportPrivateUsage]
        meta.origin_slug,
        meta.charge_grams,
        min_rating=min_rating,
        weight_tolerance_frac=weight_tolerance_frac,
    )
    held_out_started = datetime.fromisoformat(meta.started_at_utc)
    for run_id in ranked:
        if run_id == meta.run_id:
            continue
        candidate_detail = await store.read_run(run_id)
        if candidate_detail is None:
            continue
        # completed_at_utc is optional (falls back to the always-present
        # started_at_utc); RoastDetail guarantees the fallback is never None.
        candidate_timestamp = candidate_detail.completed_at_utc or candidate_detail.started_at_utc
        if datetime.fromisoformat(candidate_timestamp) >= held_out_started:
            continue
        reference = await store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
            run_id, meta.origin_slug
        )
        if reference is not None:
            return reference
    return None


# --- Fixture reconstruction + reference injection -----------------------------


def build_fixture_for_run(store_path: Path, run_id: str, tmp_dir: Path) -> Path:
    """Export one store run to a replay fixture under ``tmp_dir`` (#300 adapter).

    Args:
        store_path: The store to export from (read-only inside the adapter).
        run_id: The run to export.
        tmp_dir: A scratch directory (created by the caller); the fixture is
            written to ``tmp_dir/<run_id>/roast.jsonl``.

    Returns:
        The written ``roast.jsonl`` path.

    Raises:
        FixtureConversionError: If the run has no telemetry or is missing a
            required charge/first-crack/drop mark.
    """
    entry = convert_store_run(store_path, tmp_dir / run_id, run_id)
    return Path(cast("str", entry["fixture"]))


def build_development_ticks(
    fixture: Path, *, cadence_seconds: float, profile_name: str
) -> list[ReplayTick]:
    """Reconstruct + enrich a fixture's ticks, scoped to post-FC DEVELOPMENT.

    Reuses ``advisor_bakeoff``'s exact #277 machinery: :func:`build_ticks`
    (real-roast reconstruction) → ``enrich_ticks_with_control_context`` (the
    #273/#275 control-loop context every field the live D35 loop populates) →
    ``development_only`` (the D35 advisor scope — the advisor never runs
    pre-first-crack).

    Args:
        fixture: The exported ``roast.jsonl``.
        cadence_seconds: Roast-time spacing between scored ticks.
        profile_name: The held-out roast's REAL bean profile name (Codex PR
            #578 round 2 finding: :func:`build_ticks` defaults
            ``profile_name`` to the fixture's parent directory names —
            ``build_fixture_for_run`` writes fixtures under
            ``tmp_dir/<run_id>/``, so an unset ``profile_name`` stamps every
            advisor context with a meaningless temp-dir label instead of the
            bean's actual name). Pass the SAME identity
            :func:`~roastpilot_agent.models.recording_origin_slug` and the
            reference retrieval already use (``HeldOutMeta.bean_label`` — the
            frozen ``RoastProfile.name``), never re-derived here.

    Returns:
        The post-FC development ticks, enriched, in roast order. May be empty
        for a degenerate fixture (guarded by the caller).
    """
    ticks, ground = build_ticks(fixture, cadence_seconds=cadence_seconds, profile_name=profile_name)
    ticks = enrich_ticks_with_control_context(ticks, ground)
    return development_only(ticks)


def ticks_for_arm(
    ticks: list[ReplayTick], reference: ReferenceRoast | None, *, inject: bool
) -> list[ReplayTick]:
    """Return ``ticks`` with the reference spliced in for arms 2/3 (design §6.4).

    Arm 1 (``inject=False``) and any run with no usable reference return
    ``ticks`` unchanged — ``AdvisorContext.reference_curve`` /
    ``reference_landmarks`` already default to ``[]`` / ``None``, so arm 1's
    contract ("reference_curve=[], reference_landmarks=None") holds without
    any extra work.

    Args:
        ticks: The reconstructed development ticks.
        reference: The retrieved same-bean reference, or ``None``.
        inject: Whether this arm wants the reference spliced in.

    Returns:
        New ticks with ``reference_curve``/``reference_landmarks`` populated
        (arms 2/3 with a reference) or the original ticks (arm 1, or any arm
        when no reference is available).
    """
    if not inject or reference is None:
        return ticks
    injected: list[ReplayTick] = []
    for tick in ticks:
        new_context = tick.context.model_copy(
            update={
                "reference_curve": reference.curve,
                "reference_landmarks": reference.landmarks,
            }
        )
        injected.append(dataclasses.replace(tick, context=new_context))
    return injected


# --- Advisor construction (real + dry-run) ------------------------------------


def make_advisor_config(
    model: str, prompt_version: str, reasoning: ReasoningEffort | None
) -> AdvisorConfig:
    """Build an OpenRouter-backed config pinning every phase to ``model``.

    Every phase is pinned (not just DEVELOPMENT) so the resolved model can
    never silently fall back to ``AdvisorConfig``'s own gpt-4o-everywhere
    default map — mirrors ``advisor_bakeoff._make_config``.

    Args:
        model: The candidate model slug.
        prompt_version: ``c8`` or ``c9``.
        reasoning: Reasoning effort, or ``None`` for the provider default.

    Returns:
        The :class:`~roastpilot_agent.config.AdvisorConfig`.
    """
    return AdvisorConfig(
        provider="openai_compatible",
        provider_base_url=OPENROUTER,
        api_key_env="OPENROUTER_API_KEY",
        model_slug=model,
        model_slug_by_phase={phase: model for phase in RoastPhase},
        prompt_version=prompt_version,
        reasoning_effort=reasoning,
    )


class DryRunAdvisor(RoastAdvisor):
    """Deterministic, network-free advisor for ``--dry-run`` wiring verification.

    Never calls a provider. Its recommendation is a small, PURELY
    context-derived function — never a canned constant — so a dry run can
    still prove that arm 2/3's injected ``reference_curve``/
    ``reference_landmarks`` actually reached the object the recommender sees
    (the rationale states whether the fields were populated), and that the
    development-ratio-vs-target rule can fire a real ``should_drop=True`` at
    the tick the real roast would have dropped. It carries NO model
    intelligence — it is a wiring probe, not a stand-in for the real advisor's
    judgement.
    """

    def __init__(self, model: str, prompt_version: str) -> None:
        """Initialize with the (model, prompt_version) identity to report.

        Args:
            model: The candidate model slug this dry-run stands in for.
            prompt_version: The prompt version (``c8``/``c9``) under test.
        """
        self._model = model
        self._prompt_version = prompt_version

    @property
    def descriptor(self) -> AdvisorDescriptor:
        """Identity for the trace: provider ``dry-run``, the arm's model/prompt."""
        return AdvisorDescriptor(
            provider="dry-run", model=self._model, prompt_version=self._prompt_version
        )

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return a decision derived purely from ``context`` — no network, no cost.

        Holds the actuated heat/fan (falling back to a neutral default when
        unknown), and recommends dropping once the development-time ratio has
        reached the profile's target — the same coarse "is it time" signal a
        real drop-lens advisor reasons over, without any model call.
        """
        heat = context.current_heat_percent if context.current_heat_percent is not None else 65
        fan = context.current_fan_percent if context.current_fan_percent is not None else 40
        dtr = context.development_time_ratio
        # development_time_ratio is a FRACTION (0..1); target_development_percent
        # is a PERCENT (0..100) — the same unit pair the real advisor's own
        # context carries. Compare like-for-like (percent vs percent).
        dtr_percent = None if dtr is None else dtr * 100.0
        target = context.target_development_percent
        should_drop = dtr_percent is not None and target is not None and dtr_percent >= target
        has_reference = bool(context.reference_curve) or context.reference_landmarks is not None
        rationale = (
            f"[dry-run wiring probe] dtr_percent={dtr_percent!r} target={target!r} "
            f"bean_temp_c={context.current_bean_temp_c:.1f} "
            f"reference_injected={has_reference} "
            f"(prompt_version={self._prompt_version!r} is not read by this probe — "
            f"the fake proves the CONTEXT wiring, never model judgement)"
        )
        return RoastDecision(
            target_heat=heat,
            target_fan=fan,
            should_drop=should_drop,
            confidence=0.9 if should_drop else 0.5,
            rationale=rationale,
        )

    async def healthcheck(self) -> AdvisorHealth:
        """Always reachable — no provider to probe."""
        return AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="dry-run",
            model_slug=self._model,
            error=None,
        )


Recommender = Callable[[AdvisorContext], Awaitable[RoastDecision]]


def build_recommender(
    model: str, prompt_version: str, reasoning: ReasoningEffort | None, *, dry_run: bool
) -> Recommender:
    """Build the per-arm recommender: the real advisor, or :class:`DryRunAdvisor`.

    The real path bounds every call with :data:`MEASURE_TIMEOUT_SECONDS`
    (Codex PR #578 round 3 finding — mirrors ``advisor_bakeoff.py``'s own
    ``asyncio.wait_for(advisor.get_recommendation(context), timeout=
    MEASURE_TIMEOUT)`` pattern, and the live controller's own timeout
    discipline): an unbounded await could hang a paid run indefinitely on a
    stuck provider. ``replay_roast`` already catches any exception the
    recommender raises (including the ``TimeoutError`` ``asyncio.wait_for``
    raises on expiry) and records it as that tick's error, so a timeout reads
    exactly like any other provider failure — no special-casing needed here.

    Args:
        model: The candidate model slug.
        prompt_version: ``c8`` or ``c9``.
        reasoning: Reasoning effort for the real path; ignored in dry-run.
        dry_run: When ``True``, never touches the network or a key.

    Returns:
        An async callable matching ``get_recommendation``'s shape.
    """
    if dry_run:
        return DryRunAdvisor(model, prompt_version).get_recommendation
    advisor = PydanticAIAdvisor(make_advisor_config(model, prompt_version, reasoning))

    async def recommend(context: AdvisorContext) -> RoastDecision:
        return await asyncio.wait_for(
            advisor.get_recommendation(context), timeout=MEASURE_TIMEOUT_SECONDS
        )

    return recommend


# --- Per-tick / per-arm / per-run records -------------------------------------


@dataclasses.dataclass(frozen=True)
class TickRecord:
    """One scored decision tick — the compact, JSON-ready shape the report reads.

    Deliberately NOT the raw :class:`~roastpilot_agent.advisor.AdvisorContext`
    (which would embed the whole reference curve on every tick for arms 2/3
    and bloat the artifact); only the fields the report needs.
    """

    tick_index: int
    roast_elapsed_seconds: float
    bean_temp_c: float
    development_time_ratio: float | None
    target_heat: int | None
    target_fan: int | None
    should_drop: bool | None
    confidence: float | None
    rationale: str | None
    latency_seconds: float | None
    error: str | None


def tick_record_from_outcome(index: int, outcome: TickOutcome) -> TickRecord:
    """Project one replay :class:`~bakeoff_replay.TickOutcome` to a :class:`TickRecord`."""
    decision = outcome.decision
    return TickRecord(
        tick_index=index,
        roast_elapsed_seconds=outcome.tick.context.roast_elapsed_seconds,
        bean_temp_c=outcome.tick.context.current_bean_temp_c,
        development_time_ratio=outcome.tick.context.development_time_ratio,
        target_heat=None if decision is None else decision.target_heat,
        target_fan=None if decision is None else decision.target_fan,
        should_drop=None if decision is None else decision.should_drop,
        confidence=None if decision is None else decision.confidence,
        rationale=None if decision is None else decision.rationale,
        latency_seconds=outcome.latency_seconds,
        error=outcome.error,
    )


def tick_record_to_json(record: TickRecord) -> dict[str, Any]:
    """Serialize a :class:`TickRecord`."""
    return dataclasses.asdict(record)


def tick_record_from_json(data: dict[str, Any]) -> TickRecord:
    """Deserialize a :class:`TickRecord` (checkpoint resume)."""
    return TickRecord(
        tick_index=int(data["tick_index"]),
        roast_elapsed_seconds=float(data["roast_elapsed_seconds"]),
        bean_temp_c=float(data["bean_temp_c"]),
        development_time_ratio=cast("float | None", data["development_time_ratio"]),
        target_heat=cast("int | None", data["target_heat"]),
        target_fan=cast("int | None", data["target_fan"]),
        should_drop=cast("bool | None", data["should_drop"]),
        confidence=cast("float | None", data["confidence"]),
        rationale=cast("str | None", data["rationale"]),
        latency_seconds=cast("float | None", data["latency_seconds"]),
        error=cast("str | None", data["error"]),
    )


@dataclasses.dataclass(frozen=True)
class ArmRecord:
    """One arm's full per-tick trace for one held-out run."""

    arm_key: str
    arm_label: str
    prompt_version: str
    reference_injected: bool
    ticks: list[TickRecord]

    def first_drop(self) -> TickRecord | None:
        """The first ``should_drop=True`` tick, or ``None`` if the arm never dropped."""
        return next((t for t in self.ticks if t.should_drop), None)

    def error_count(self) -> int:
        """How many ticks in this arm failed (recommender error)."""
        return sum(1 for t in self.ticks if t.error is not None)


def arm_record_from_outcomes(
    arm: ArmSpec, outcomes: list[TickOutcome], *, reference_injected: bool
) -> ArmRecord:
    """Build an :class:`ArmRecord` from a completed replay's outcomes.

    Args:
        arm: The arm spec (identity/label/prompt only — NOT the injection
            source of truth; see ``reference_injected``).
        outcomes: The per-tick replay outcomes.
        reference_injected: Whether a reference was ACTUALLY spliced into this
            arm's ticks (Codex PR #578 finding: ``arm.inject_reference`` is
            only the REQUEST — a run whose bean has no other buildable
            same-bean reference falls back to empty context even for arm 2/3,
            per :func:`ticks_for_arm`, and the record must say so rather than
            echo the request).

    Returns:
        The :class:`ArmRecord`.
    """
    return ArmRecord(
        arm_key=arm.key,
        arm_label=arm.label,
        prompt_version=arm.prompt_version,
        reference_injected=reference_injected,
        ticks=[tick_record_from_outcome(i, o) for i, o in enumerate(outcomes)],
    )


@dataclasses.dataclass(frozen=True)
class ReferenceInfo:
    """Summary of the reference roast retrieved for a held-out run."""

    source_run_id: str
    operator_rating: int
    first_crack_temp_c: float | None
    first_crack_elapsed_s: float | None
    drop_temp_c: float | None
    drop_development_percent: float | None
    curve_points: int


def reference_info_from_reference(reference: ReferenceRoast | None) -> ReferenceInfo | None:
    """Project a :class:`~roastpilot_agent.models.ReferenceRoast` to a report-ready summary."""
    if reference is None:
        return None
    landmarks = reference.landmarks
    return ReferenceInfo(
        source_run_id=reference.source_run_id,
        operator_rating=landmarks.operator_rating,
        first_crack_temp_c=landmarks.first_crack_temp_c,
        first_crack_elapsed_s=landmarks.first_crack_elapsed_s,
        drop_temp_c=landmarks.drop_temp_c,
        drop_development_percent=landmarks.drop_development_percent,
        curve_points=len(reference.curve),
    )


def reference_info_to_json(info: ReferenceInfo | None) -> dict[str, Any] | None:
    """Serialize a :class:`ReferenceInfo`."""
    return None if info is None else dataclasses.asdict(info)


def reference_info_from_json(data: dict[str, Any] | None) -> ReferenceInfo | None:
    """Deserialize a :class:`ReferenceInfo` (checkpoint resume)."""
    if data is None:
        return None
    return ReferenceInfo(
        source_run_id=str(data["source_run_id"]),
        operator_rating=int(data["operator_rating"]),
        first_crack_temp_c=cast("float | None", data["first_crack_temp_c"]),
        first_crack_elapsed_s=cast("float | None", data["first_crack_elapsed_s"]),
        drop_temp_c=cast("float | None", data["drop_temp_c"]),
        drop_development_percent=cast("float | None", data["drop_development_percent"]),
        curve_points=int(data["curve_points"]),
    )


@dataclasses.dataclass(frozen=True)
class RunRecord:
    """One held-out run's full three-arm comparison."""

    run_id: str
    bean_label: str
    origin_slug: str
    operator_rating: int
    charge_grams: float
    tick_count: int
    reference: ReferenceInfo | None
    arms: dict[str, ArmRecord]


def run_record_to_json(record: RunRecord) -> dict[str, Any]:
    """Serialize a :class:`RunRecord` for the ``--out`` artifact."""
    return {
        "run_id": record.run_id,
        "bean_label": record.bean_label,
        "origin_slug": record.origin_slug,
        "operator_rating": record.operator_rating,
        "charge_grams": record.charge_grams,
        "tick_count": record.tick_count,
        "reference": reference_info_to_json(record.reference),
        "arms": {
            key: {
                "arm_key": arm.arm_key,
                "arm_label": arm.arm_label,
                "prompt_version": arm.prompt_version,
                "reference_injected": arm.reference_injected,
                "ticks": [tick_record_to_json(t) for t in arm.ticks],
            }
            for key, arm in record.arms.items()
        },
    }


# --- Checkpoint (resume) + cost guard -----------------------------------------


def sidecar_path(out: Path) -> Path:
    """Return the sidecar JSONL path next to the ``--out`` JSON path."""
    return out.with_name(out.name + ".cells.jsonl")


CellKey = tuple[str, str, str]


def settings_key(
    model: str,
    cadence_seconds: float,
    *,
    dry_run: bool,
    reasoning: ReasoningEffort | None,
    reference_min_rating: int,
    weight_tolerance_frac: float,
) -> str:
    """The run-settings fingerprint folded into every checkpoint cell key.

    Codex PR #578 finding (round 1): a bare ``(run_id, arm_key)`` key means a
    ``--dry-run`` smoke or a different ``--model``/``--cadence-seconds`` run
    against the SAME ``--out`` silently reuses another settings combination's
    stale cells (a dry-run's fake decisions could even be replayed back as if
    they were a real model's). Folding the settings that change what a cell
    actually MEANS into the key means an incompatible re-run simply produces
    a cache MISS (and appends a fresh, distinctly-keyed record) rather than a
    silent collision — never a mixed sidecar file requiring manual cleanup.

    Round 2 finding: ``--reasoning`` changes the real advisor's own behavior
    (the OpenAI-compatible reasoning-effort request param), and
    ``--reference-min-rating`` / ``--weight-tolerance-frac`` change WHICH
    reference roast :func:`find_self_excluded_reference` retrieves for a run
    (arm 2/3's injected content) — all three are decision-affecting exactly
    like model/cadence/dry-run, so they belong in the same fingerprint.

    Args:
        model: The candidate model slug.
        cadence_seconds: Roast-time spacing between scored ticks.
        dry_run: Whether this run used the network-free fake advisor.
        reasoning: Reasoning effort for the real OpenAI-compatible path
            (``None`` = provider default; ignored but still fingerprinted
            under ``--dry-run``, where it has no effect but costs nothing to
            include).
        reference_min_rating: The reference candidate quality floor.
        weight_tolerance_frac: The reference candidate charge-weight
            tolerance.

    Returns:
        A stable string fingerprint for the full settings tuple.
    """
    reasoning_label = reasoning if reasoning is not None else "default"
    return (
        f"{model}|{cadence_seconds:.3f}|{dry_run}|{reasoning_label}|"
        f"{reference_min_rating}|{weight_tolerance_frac:.4f}"
    )


class Checkpoint:
    """Append-only sidecar of completed ``(run_id, arm_key, settings_key)`` cells.

    Mirrors ``advisor_bakeoff.Checkpoint``'s incremental-flush + resume
    discipline at the ``(run_id, arm, settings)`` grain this script uses: each
    completed arm-cell is appended to the JSONL sidecar immediately, so a kill
    / budget stop / crash leaves every finished cell recoverable, and a re-run
    with the same ``--out`` AND settings skips cells already on disk. A record
    from an incompatible settings combination (or a legacy record predating
    the settings key) never resolves as a hit — see :func:`settings_key`.
    """

    def __init__(self, path: Path, *, resume: bool = True) -> None:
        """Open (and optionally load) the sidecar at ``path``.

        Args:
            path: The sidecar JSONL path.
            resume: Load + skip existing cells when ``True``; truncate when
                ``False``.
        """
        self.path = path
        self._records: dict[CellKey, dict[str, Any]] = {}
        if resume and path.exists():
            self._load()
        elif not resume and path.exists():
            path.unlink()

    def _load(self) -> None:
        """Load existing sidecar records, keyed by ``(run_id, arm_key, settings_key)``.

        A record written before this fix carries no ``settings_key`` field;
        it is loaded under the empty-string fingerprint, which no live run
        can ever request (:func:`settings_key` never returns ``""``), so a
        legacy record is preserved on disk but never resolves as a resume hit.
        """
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            record = cast("dict[str, Any]", json.loads(line))
            key = (
                str(record["run_id"]),
                str(record["arm_key"]),
                str(record.get("settings_key", "")),
            )
            self._records[key] = record

    def has(self, run_id: str, arm_key: str, key_settings: str) -> bool:
        """Return whether ``(run_id, arm_key, key_settings)`` is already complete on disk."""
        return (run_id, arm_key, key_settings) in self._records

    def get(self, run_id: str, arm_key: str, key_settings: str) -> dict[str, Any]:
        """Return the stored record for an already-complete cell."""
        return self._records[(run_id, arm_key, key_settings)]

    def append(self, record: dict[str, Any]) -> None:
        """Persist one completed cell to disk immediately and remember it.

        ``record`` must carry a ``"settings_key"`` field (see
        :func:`settings_key`) — every call site in this script sets it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
        key = (str(record["run_id"]), str(record["arm_key"]), str(record["settings_key"]))
        self._records[key] = record

    def completed_count(self) -> int:
        """How many cells are already complete on disk."""
        return len(self._records)


class CostGuard:
    """Tracks cumulative estimated spend and trips a graceful stop (mirrors
    ``advisor_bakeoff.CostGuard``).

    Spend is estimated as ``calls * cost_per_call``; ``would_exceed`` lets the
    caller decide BEFORE paying for the next cell.
    """

    def __init__(self, cost_per_call: float, max_spend: float | None) -> None:
        """Initialize the guard.

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
        """Whether running ``upcoming_calls`` more would breach the budget."""
        if self.max_spend is None:
            return False
        projected = (self._calls + upcoming_calls) * self.cost_per_call
        return projected > self.max_spend


# --- Orchestration -------------------------------------------------------------


@dataclasses.dataclass
class BakeoffResult:
    """The full run outcome: every completed (partial, on a budget stop) run."""

    runs: list[RunRecord]
    skipped_runs: list[str]
    stopped_for_budget: bool
    resumed_cells: int
    fresh_cells: int
    total_cells: int


def _reference_from_json_or_meta(record: dict[str, Any]) -> ReferenceInfo | None:
    """Read the ``reference`` field a checkpoint cell record carries."""
    raw = cast("dict[str, Any] | None", record.get("reference"))
    return reference_info_from_json(raw)


async def run_bakeoff(
    *,
    store_path: Path,
    model: str,
    run_ids: list[str] | None,
    min_group_size: int,
    reference_min_rating: int,
    weight_tolerance_frac: float,
    cadence_seconds: float,
    reasoning: ReasoningEffort | None,
    dry_run: bool,
    out: Path,
    resume: bool,
    max_spend: float | None,
    cost_per_call: float,
) -> BakeoffResult:
    """Run the full three-arm bake-off over every (auto-discovered or explicit) held-out run.

    Args:
        store_path: The real operator SQLite store — read via a private
            snapshot copy (:func:`snapshot_store_to_temp`); never opened
            read-write directly.
        model: The candidate model slug (all three arms use the same model).
        run_ids: Explicit held-out run ids, or ``None`` to auto-discover every
            completed+rated run in a same-bean group of >= ``min_group_size``.
        min_group_size: Auto-discovery's minimum same-bean group size.
        reference_min_rating: The reference candidate quality floor.
        weight_tolerance_frac: The reference candidate charge-weight tolerance.
        cadence_seconds: Roast-time spacing between scored ticks.
        reasoning: Reasoning effort for the real advisor path.
        dry_run: When ``True``, use :class:`DryRunAdvisor` — zero network, zero
            spend, regardless of ``max_spend``.
        out: The JSON artifact path (also anchors the checkpoint sidecar).
        resume: Skip cells already on the checkpoint sidecar.
        max_spend: Optional USD budget; ignored when ``dry_run``.
        cost_per_call: Estimated USD per recommender call.

    Returns:
        The :class:`BakeoffResult`.
    """
    with tempfile.TemporaryDirectory(prefix="bakeoff-reference-567-") as tmp:
        tmp_dir = Path(tmp)
        # Codex PR #578 finding: never open the operator's live store
        # read-write. RoastStore.initialize() opens read-write and applies
        # WAL/migrations, so this script only ever opens a private snapshot
        # COPY, never store_path itself — regardless of --dry-run.
        snapshot_path = snapshot_store_to_temp(store_path, tmp_dir)
        store = RoastStore(snapshot_path)
        await store.initialize()
        try:
            metas = (
                await resolve_explicit_runs(store, run_ids)
                if run_ids
                else await discover_held_out_runs(store, min_group_size=min_group_size)
            )
            if not metas:
                print(
                    "no held-out runs found (no completed+rated run in a same-bean "
                    f"group of >= {min_group_size}, and --run-ids was not given)",
                    flush=True,
                )
                return BakeoffResult(
                    runs=[],
                    skipped_runs=[],
                    stopped_for_budget=False,
                    resumed_cells=0,
                    fresh_cells=0,
                    total_cells=0,
                )

            checkpoint = Checkpoint(sidecar_path(out), resume=resume)
            guard = CostGuard(cost_per_call, None if dry_run else max_spend)
            key_settings = settings_key(
                model,
                cadence_seconds,
                dry_run=dry_run,
                reasoning=reasoning,
                reference_min_rating=reference_min_rating,
                weight_tolerance_frac=weight_tolerance_frac,
            )
            total_cells = len(metas) * len(ARMS)
            run_labels = ", ".join(f"{m.bean_label} [{m.run_id[:8]}]" for m in metas)
            print(
                f"held-out runs: {len(metas)} ({run_labels}) -> {total_cells} cells "
                f"(runs x {len(ARMS)} arms)",
                flush=True,
            )

            runs: list[RunRecord] = []
            skipped_runs: list[str] = []
            resumed_cells = 0
            fresh_cells = 0
            stopped = False

            def _arm_record_from_checkpoint(record: dict[str, Any]) -> ArmRecord:
                """Rebuild an :class:`ArmRecord` from a loaded checkpoint record."""
                return ArmRecord(
                    arm_key=str(record["arm_key"]),
                    arm_label=str(record["arm_label"]),
                    prompt_version=str(record["prompt_version"]),
                    reference_injected=bool(record["reference_injected"]),
                    ticks=[
                        tick_record_from_json(cast("dict[str, Any]", t))
                        for t in cast("list[Any]", record["ticks"])
                    ],
                )

            for meta in metas:
                if stopped:
                    break
                all_cached = all(checkpoint.has(meta.run_id, arm.key, key_settings) for arm in ARMS)
                arm_records: dict[str, ArmRecord] = {}
                reference_info: ReferenceInfo | None = None
                tick_count = 0

                if all_cached:
                    for arm in ARMS:
                        record = checkpoint.get(meta.run_id, arm.key, key_settings)
                        arm_record = _arm_record_from_checkpoint(record)
                        arm_records[arm.key] = arm_record
                        resumed_cells += 1
                        # #578 finding 3: seed the guard with resumed calls too, so a
                        # re-run's spend accounting (and --max-spend enforcement)
                        # reflects EVERY call this --out has ever paid for, not just
                        # the calls made in this process.
                        if not dry_run:
                            guard.add_calls(len(arm_record.ticks))
                    reference_info = _reference_from_json_or_meta(
                        checkpoint.get(meta.run_id, ARMS[0].key, key_settings)
                    )
                    tick_count = len(arm_records[ARMS[0].key].ticks)
                    print(
                        f"[resume] {meta.bean_label} [{meta.run_id[:8]}]: all 3 arms on disk",
                        flush=True,
                    )
                else:
                    try:
                        fixture = build_fixture_for_run(snapshot_path, meta.run_id, tmp_dir)
                        dev_ticks = build_development_ticks(
                            fixture,
                            cadence_seconds=cadence_seconds,
                            profile_name=meta.bean_label,
                        )
                    except (FixtureConversionError, ValueError) as exc:
                        print(f"[skip] {meta.run_id}: {exc}", flush=True)
                        skipped_runs.append(meta.run_id)
                        continue
                    if not dev_ticks:
                        print(f"[skip] {meta.run_id}: no post-FC development ticks", flush=True)
                        skipped_runs.append(meta.run_id)
                        continue
                    tick_count = len(dev_ticks)
                    reference = await find_self_excluded_reference(
                        store,
                        meta,
                        min_rating=reference_min_rating,
                        weight_tolerance_frac=weight_tolerance_frac,
                    )
                    reference_info = reference_info_from_reference(reference)
                    reference_summary = (
                        f"{reference_info.source_run_id[:8]} "
                        f"(rating {reference_info.operator_rating})"
                        if reference_info is not None
                        else "NONE"
                    )
                    print(
                        f"[run] {meta.bean_label} [{meta.run_id[:8]}] "
                        f"rating={meta.operator_rating} ticks={tick_count} "
                        f"reference={reference_summary}",
                        flush=True,
                    )

                    for arm in ARMS:
                        if checkpoint.has(meta.run_id, arm.key, key_settings):
                            record = checkpoint.get(meta.run_id, arm.key, key_settings)
                            arm_record = _arm_record_from_checkpoint(record)
                            arm_records[arm.key] = arm_record
                            resumed_cells += 1
                            if not dry_run:
                                guard.add_calls(len(arm_record.ticks))
                            continue
                        if not dry_run and guard.would_exceed(tick_count):
                            stopped = True
                            print(
                                f"[budget] stopping before {meta.run_id}/{arm.key} "
                                f"(~{tick_count} calls) would exceed --max-spend "
                                f"${max_spend:.2f} (spent ~${guard.spend:.2f} over "
                                f"{guard.calls} calls)",
                                flush=True,
                            )
                            break
                        # #578 finding 1: reference may be None (no PRIOR same-bean run
                        # qualified), in which case arm 2/3 silently degrade to arm 1's
                        # empty context — reference_injected below records that REALITY.
                        actually_injected = arm.inject_reference and reference is not None
                        arm_ticks = ticks_for_arm(dev_ticks, reference, inject=arm.inject_reference)
                        recommender = build_recommender(
                            model, arm.prompt_version, reasoning, dry_run=dry_run
                        )
                        outcomes = await replay_roast(
                            arm_ticks, recommender, clock=time.perf_counter
                        )
                        if not dry_run:
                            guard.add_calls(len(outcomes))
                        arm_record = arm_record_from_outcomes(
                            arm, outcomes, reference_injected=actually_injected
                        )
                        arm_records[arm.key] = arm_record
                        fresh_cells += 1
                        first_drop = arm_record.first_drop()
                        first_drop_label = f"tick {first_drop.tick_index}" if first_drop else "none"
                        print(
                            f"  [cell] {arm.key} | ticks={len(outcomes)} "
                            f"errors={arm_record.error_count()} "
                            f"first_drop={first_drop_label} ~${guard.spend:.2f} total",
                            flush=True,
                        )
                        # #578 round 3 finding 1: a cell where EVERY tick errored (the
                        # exact case that hit this PR's first paid run — a stale/401
                        # OpenRouter key) must NOT be checkpointed as complete. A
                        # completed-looking checkpoint record for an all-error cell
                        # would make a later resume SKIP it — silently treating the
                        # auth/provider failure as "done" and producing an empty
                        # report instead of retrying once the real problem is fixed.
                        # A PARTIALLY-errored cell (some ticks got a real decision)
                        # still checkpoints — only a wholly-failed cell is withheld.
                        all_ticks_errored = bool(outcomes) and arm_record.error_count() == len(
                            arm_record.ticks
                        )
                        if all_ticks_errored:
                            print(
                                f"  [cell] {arm.key} | ALL {len(outcomes)} ticks errored — "
                                f"NOT checkpointed, a resume will retry this cell",
                                flush=True,
                            )
                        else:
                            checkpoint.append(
                                {
                                    "run_id": meta.run_id,
                                    "arm_key": arm.key,
                                    "arm_label": arm.label,
                                    "prompt_version": arm.prompt_version,
                                    "reference_injected": actually_injected,
                                    "reference": reference_info_to_json(reference_info),
                                    "ticks": [tick_record_to_json(t) for t in arm_record.ticks],
                                    "settings_key": key_settings,
                                    "model": model,
                                    "cadence_seconds": cadence_seconds,
                                    "dry_run": dry_run,
                                }
                            )

                if arm_records:
                    runs.append(
                        RunRecord(
                            run_id=meta.run_id,
                            bean_label=meta.bean_label,
                            origin_slug=meta.origin_slug,
                            operator_rating=meta.operator_rating,
                            charge_grams=meta.charge_grams,
                            tick_count=tick_count,
                            reference=reference_info,
                            arms=arm_records,
                        )
                    )

            return BakeoffResult(
                runs=runs,
                skipped_runs=skipped_runs,
                stopped_for_budget=stopped,
                resumed_cells=resumed_cells,
                fresh_cells=fresh_cells,
                total_cells=total_cells,
            )
        finally:
            await store.close()


# --- Report rendering ----------------------------------------------------------


def _fmt(value: float | None, digits: int = 1) -> str:
    """Render an optional float, or an em-dash placeholder when ``None``."""
    return "—" if value is None else f"{value:.{digits}f}"


def render_report(result: BakeoffResult, *, model: str, dry_run: bool) -> str:
    """Render the markdown scorecard: per-run 3-arm comparison + an aggregate.

    Args:
        result: The completed (or budget-partial) bake-off result.
        model: The candidate model slug under test.
        dry_run: Whether this was a zero-spend dry run (stamped into the header
            so the report is never mistaken for a real-model result).

    Returns:
        The markdown report text.
    """
    lines: list[str] = []
    lines.append("# #567 reference-curve three-arm bake-off")
    lines.append("")
    lines.append(f"- model: `{model}`{' (DRY RUN — no network, no cost)' if dry_run else ''}")
    lines.append(
        f"- held-out runs scored: {len(result.runs)} (skipped: {len(result.skipped_runs)})"
    )
    lines.append(
        f"- cells: {result.fresh_cells} fresh + {result.resumed_cells} resumed "
        f"/ {result.total_cells} total"
    )
    if result.stopped_for_budget:
        lines.append(
            "- **STOPPED EARLY on --max-spend** — this is a PARTIAL report over the "
            "completed cells. Re-run the same command (resume is on) to finish the rest."
        )
    lines.append("")
    lines.append(
        "Arm 1 = c8, no reference. Arm 2 = c8, reference present but UNTAUGHT "
        "(isolates the raw data). Arm 3 = c9, reference present AND taught "
        "(isolates the teaching, vs arm 2)."
    )
    lines.append("")

    for run in result.runs:
        lines.append(
            f"## {run.bean_label} — run `{run.run_id[:8]}` "
            f"(rating {run.operator_rating}/5, {run.charge_grams:g} g, {run.tick_count} ticks)"
        )
        if run.reference is not None:
            ref = run.reference
            lines.append(
                f"Reference: run `{ref.source_run_id[:8]}` (rating {ref.operator_rating}/5) — "
                f"FC {_fmt(ref.first_crack_temp_c)} °C @ {_fmt(ref.first_crack_elapsed_s, 0)} s, "
                f"drop {_fmt(ref.drop_temp_c)} °C @ dev {_fmt(ref.drop_development_percent)} %, "
                f"{ref.curve_points} curve points."
            )
        else:
            lines.append(
                "Reference: **none found** (no OTHER same-bean run passed the quality/"
                "weight-tolerance floor) — arms 2/3 fall back to arm 1's empty context "
                "for this run."
            )
        lines.append("")
        lines.append(
            "| Arm | Prompt | Ref injected | First drop tick | Drop bean °C | Drop DTR % | Errors |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for arm in ARMS:
            record = run.arms.get(arm.key)
            if record is None:
                lines.append(f"| {arm.label} | {arm.prompt_version} | — | not run | — | — | — |")
                continue
            first_drop = record.first_drop()
            drop_tick_label = f"tick {first_drop.tick_index}" if first_drop else "never"
            drop_temp_label = _fmt(first_drop.bean_temp_c) if first_drop else "—"
            drop_dtr_pct = (
                _fmt((first_drop.development_time_ratio or 0.0) * 100) if first_drop else "—"
            )
            lines.append(
                f"| {arm.label} | {arm.prompt_version} | "
                f"{'yes' if record.reference_injected else 'no'} | "
                f"{drop_tick_label} | {drop_temp_label} | {drop_dtr_pct} | "
                f"{record.error_count()}/{len(record.ticks)} |"
            )
        lines.append("")
        for arm in ARMS:
            record = run.arms.get(arm.key)
            if record is None:
                continue
            lines.append(f"### {arm.label} — per-tick trace")
            for tick in record.ticks:
                dtr_pct = (
                    "—"
                    if tick.development_time_ratio is None
                    else f"{tick.development_time_ratio * 100:.1f}%"
                )
                if tick.error is not None:
                    lines.append(
                        f"- tick {tick.tick_index} @ {tick.roast_elapsed_seconds:.0f}s "
                        f"(bean {tick.bean_temp_c:.1f} °C, DTR {dtr_pct}): **ERROR** {tick.error}"
                    )
                    continue
                lines.append(
                    f"- tick {tick.tick_index} @ {tick.roast_elapsed_seconds:.0f}s "
                    f"(bean {tick.bean_temp_c:.1f} °C, DTR {dtr_pct}): "
                    f"heat={tick.target_heat} fan={tick.target_fan} "
                    f"drop={tick.should_drop} conf={tick.confidence} "
                    f"lat={_fmt(tick.latency_seconds, 2)}s"
                )
                if tick.rationale:
                    lines.append(f"  > {tick.rationale}")
            lines.append("")

    lines.append("## Aggregate")
    lines.append("")
    lines.append(
        "| Arm | runs with a drop call | mean drop DTR % | mean drop bean °C | "
        'rationale mentions "reference" | ref actually injected |'
    )
    lines.append("|---|---|---|---|---|---|")
    for arm in ARMS:
        drop_dtrs: list[float] = []
        drop_temps: list[float] = []
        runs_with_drop = 0
        # #578 round 1 finding 6: the denominator must be runs that actually
        # HAVE a cell for this arm, not len(result.runs) — a --max-spend stop
        # mid-run can leave a RunRecord with only arm 1 present, and dividing
        # by every run (including ones this arm never ran on) makes a
        # not-yet-run cell read as a false "no drop" instead of "not run".
        runs_with_cell = 0
        # #578 round 2 finding 5: how many of this arm's cells ACTUALLY had a
        # reference spliced in (vs. requested — see ArmRecord.reference_injected,
        # round 1 finding 7). Always 0 for arm 1 (never requests injection).
        injected_cell_count = 0
        reference_mentions = 0
        total_ticks = 0
        for run in result.runs:
            record = run.arms.get(arm.key)
            if record is None:
                continue
            runs_with_cell += 1
            if record.reference_injected:
                injected_cell_count += 1
            # #578 round 2 finding 5: arm 2/3's drop-rate / DTR / temp / mention
            # aggregate is scoped to cells where a reference was ACTUALLY
            # injected. A bean with no usable PRIOR same-bean run (round 1
            # finding 1's temporal filter) makes arm 2/3 silently degrade to
            # arm 1's empty context — that run must not be counted as
            # "reference present" evidence, or the two arms' numbers mix
            # genuine reference-present behavior with plain no-reference
            # behavior. Arm 1 never requests injection, so this never filters
            # its own row — it stays the full, always-no-reference baseline.
            if arm.inject_reference and not record.reference_injected:
                continue
            first_drop = record.first_drop()
            if first_drop is not None:
                runs_with_drop += 1
                if first_drop.development_time_ratio is not None:
                    drop_dtrs.append(first_drop.development_time_ratio * 100)
                drop_temps.append(first_drop.bean_temp_c)
            for tick in record.ticks:
                total_ticks += 1
                if tick.rationale and "reference" in tick.rationale.lower():
                    reference_mentions += 1
        mean_dtr = sum(drop_dtrs) / len(drop_dtrs) if drop_dtrs else None
        mean_temp = sum(drop_temps) / len(drop_temps) if drop_temps else None
        # The drop-rate denominator mirrors the same injected-only scoping as
        # the metrics above: arm 2/3 count against injected_cell_count (the
        # REAL population their numbers are drawn from), arm 1 against every
        # cell it ran on.
        drop_rate_denominator = injected_cell_count if arm.inject_reference else runs_with_cell
        lines.append(
            f"| {arm.label} | {runs_with_drop}/{drop_rate_denominator} | {_fmt(mean_dtr)} | "
            f"{_fmt(mean_temp)} | {reference_mentions}/{total_ticks} ticks | "
            f"{injected_cell_count}/{runs_with_cell} |"
        )
    lines.append("")
    lines.append(
        'The "mentions reference" column is a coarse keyword heuristic over the '
        "verbatim rationale text (never a scored metric) — a rough signal of whether "
        "the model's stated reasoning engages with the reference at all, useful for "
        "spot-checking arm 2 (untaught — expected near-zero) against arm 3 (taught)."
    )
    lines.append(
        'The "ref actually injected" column is the REAL denominator behind arm 2/3\'s '
        "other columns: cells where a usable PRIOR same-bean reference existed and was "
        "spliced in, out of every cell that ran for that arm. A run whose bean has no "
        "qualifying prior roast falls back to arm 1's empty context for arm 2/3 too — "
        "excluded from their drop-rate/DTR/temp/mention numbers above, never silently "
        "counted as reference-present evidence."
    )
    if result.skipped_runs:
        lines.append("")
        lines.append(f"Skipped runs (fixture/tick reconstruction failed): {result.skipped_runs}")
    return "\n".join(lines)


def bakeoff_result_to_json(
    result: BakeoffResult,
    *,
    model: str,
    dry_run: bool,
    max_spend: float | None,
    cost_per_call: float,
) -> dict[str, Any]:
    """Serialize the full :class:`BakeoffResult` for the ``--out`` artifact."""
    return {
        "model": model,
        "dry_run": dry_run,
        "max_spend": max_spend,
        "cost_per_call": cost_per_call,
        "stopped_for_budget": result.stopped_for_budget,
        "resumed_cells": result.resumed_cells,
        "fresh_cells": result.fresh_cells,
        "total_cells": result.total_cells,
        "held_out_run_count": len(result.runs),
        "skipped_runs": result.skipped_runs,
        "arms": [
            {
                "key": arm.key,
                "label": arm.label,
                "prompt_version": arm.prompt_version,
                "inject_reference": arm.inject_reference,
            }
            for arm in ARMS
        ],
        "runs": [run_record_to_json(r) for r in result.runs],
    }


# --- CLI -----------------------------------------------------------------------


async def main() -> int:
    """CLI entrypoint: run the three-arm bake-off and write the report(s).

    Returns:
        ``0`` on a completed (or gracefully budget-stopped) run.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=Path.home() / "roasts" / "roastpilot.sqlite3",
        help="path to the real operator SQLite store; NEVER opened directly — a "
        "private temp snapshot copy is opened instead, so this file is never "
        "opened read-write",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"candidate model slug (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--run-ids",
        nargs="+",
        default=None,
        help="explicit held-out roast_runs.id(s) to replay (default: auto-discover "
        "every completed+rated run in a same-bean group of >= --min-group-size)",
    )
    parser.add_argument(
        "--min-group-size",
        type=int,
        default=DEFAULT_MIN_GROUP_SIZE,
        help=f"auto-discovery: minimum same-bean completed+rated group size "
        f"(default: {DEFAULT_MIN_GROUP_SIZE}) — a singleton bean has no OTHER "
        f"run to serve as its own reference",
    )
    parser.add_argument(
        "--reference-min-rating",
        type=int,
        default=DEFAULT_REFERENCE_MIN_RATING,
        help=f"minimum operator_rating a reference CANDIDATE must have "
        f"(default: {DEFAULT_REFERENCE_MIN_RATING}, the design's quality floor)",
    )
    parser.add_argument(
        "--weight-tolerance-frac",
        type=float,
        default=DEFAULT_WEIGHT_TOLERANCE_FRAC,
        help=f"fractional charge-weight tolerance for a reference candidate "
        f"(default: {DEFAULT_WEIGHT_TOLERANCE_FRAC:g})",
    )
    parser.add_argument(
        "--cadence-seconds",
        type=float,
        default=DEFAULT_CADENCE_SECONDS,
        help=f"roast-time spacing between scored ticks (default: {DEFAULT_CADENCE_SECONDS:g})",
    )
    parser.add_argument(
        "--reasoning",
        default="default",
        choices=["default", "off", "minimal", "low", "medium", "high"],
        help="reasoning effort for the real OpenAI-compatible path (default: provider default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use the deterministic, network-free DryRunAdvisor — zero spend, "
        "for wiring verification only; never a stand-in for real model judgement",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore + truncate any existing checkpoint sidecar and run every "
        "cell from scratch (default: resume — skip cells already on disk)",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=None,
        help="REQUIRED for a real (non-dry-run) run: USD budget; the run stops "
        "gracefully before a cell that would breach it starts. No default — an "
        "unbounded real run is refused. Ignored (and not required) under --dry-run.",
    )
    parser.add_argument(
        "--cost-per-call",
        type=float,
        default=DEFAULT_COST_PER_CALL_USD,
        help=f"estimated USD per recommender call for the cost guard "
        f"(default: {DEFAULT_COST_PER_CALL_USD})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/bakeoff-reference-567.json"),
        help="JSON artifact path (also anchors the checkpoint sidecar)",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=None,
        help="also write the markdown report here",
    )
    args = parser.parse_args()

    if not args.dry_run and args.max_spend is None:
        parser.error(
            "--max-spend is required for a real (non-dry-run) run — pass --dry-run instead "
            "for a zero-spend wiring check"
        )
    # Codex PR #578 round 2: a zero or negative --cost-per-call makes
    # would_exceed's "projected = calls * cost_per_call" arithmetic never
    # exceed --max-spend, so the budget guard never trips — the same
    # unbounded-spend hole the required --max-spend exists to close.
    if cast("float", args.cost_per_call) <= 0:
        parser.error("--cost-per-call must be > 0 (a zero/negative value defeats --max-spend)")
    # Codex PR #578 round 2: a repeated --run-ids entry double-counts that
    # held-out roast in `runs` and the aggregate (it is silently replayed
    # twice under separate loop iterations). Reject rather than silently
    # dedup, so a copy-paste mistake is visible immediately.
    run_ids_arg = cast("list[str] | None", args.run_ids)
    if run_ids_arg is not None:
        seen_run_ids: set[str] = set()
        duplicate_run_ids: set[str] = set()
        for run_id in run_ids_arg:
            if run_id in seen_run_ids:
                duplicate_run_ids.add(run_id)
            seen_run_ids.add(run_id)
        if duplicate_run_ids:
            parser.error(
                f"--run-ids: duplicate id(s) {sorted(duplicate_run_ids)} — "
                f"pass each run id only once"
            )

    reasoning: ReasoningEffort | None = (
        None if args.reasoning == "default" else cast("ReasoningEffort", args.reasoning)
    )

    result = await run_bakeoff(
        store_path=cast("Path", args.store),
        model=cast("str", args.model),
        run_ids=cast("list[str] | None", args.run_ids),
        min_group_size=int(args.min_group_size),
        reference_min_rating=int(args.reference_min_rating),
        weight_tolerance_frac=float(args.weight_tolerance_frac),
        cadence_seconds=float(args.cadence_seconds),
        reasoning=reasoning,
        dry_run=bool(args.dry_run),
        out=cast("Path", args.out),
        resume=not bool(args.no_resume),
        max_spend=cast("float | None", args.max_spend),
        cost_per_call=float(args.cost_per_call),
    )

    report = render_report(result, model=cast("str", args.model), dry_run=bool(args.dry_run))
    print("\n" + report, flush=True)

    out_path = cast("Path", args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            bakeoff_result_to_json(
                result,
                model=cast("str", args.model),
                dry_run=bool(args.dry_run),
                max_spend=cast("float | None", args.max_spend),
                cost_per_call=float(args.cost_per_call),
            ),
            indent=2,
        )
    )
    print(f"\nwrote artifact -> {out_path}", flush=True)

    if args.report_md is not None:
        report_md_path = cast("Path", args.report_md)
        report_md_path.write_text(report)
        print(f"wrote markdown report -> {report_md_path}", flush=True)

    if result.stopped_for_budget:
        print(
            "NOTE: the run stopped early on --max-spend; the scorecard above is a "
            "PARTIAL over the completed cells. Re-run (resume is on) to finish the rest.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
