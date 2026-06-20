"""Per-bean *reference curve* prototype on the known-good medium roasts (D42 §7.1).

This is the **step-2** prototype of the §7 sequencing in
``roastpilot-plan/roastpilot-agent/ml-learning-loop-plan.md`` (the proposed D42),
a follow-on to the #290 classification
(``docs/research/hottop-alog-classification-2026-06-20.md``). It is **pure
offline analysis**: no LLM, no API key, no roaster, no network. It exists to test
whether a *landmark-registered* reference curve is buildable on our tiny,
single-machine corpus, and to answer the two empirical questions §7 flags before
committing to a pooling design:

* does pooling across origins help, or is the curve largely per-origin (§5.1)?
* how sensitive is the reference to including the soft 196-197 °C drop tier?

**Method — registration first (D42 §2.1).** Naive cross-time averaging of
time-varying trajectories is a documented failure mode (Müller / FDA): the mean
"resembles no real roast and distorts the dynamics". So each known-good roast is
**landmark-registered** onto a common phase axis built from its own event marks
(CHARGE -> DRY-END -> FIRST-CRACK -> DROP), and only *then* aggregated. The phase
axis has a fixed number of grid nodes per segment; at each node the roast's bean
temperature (Hottop display BT, ``temp2``, °C) is linearly interpolated at the
corresponding real time, and a 30 s-window rate-of-rise (RoR, °C/min) is derived
on the roast's real clock and sampled at the same nodes. The pooled reference is
the per-node mean with an SD / percentile uncertainty band — the band is what
tells the controller how much latitude it has at each phase (D42 §2.1). For a
prototype this registered mean + percentile band stands in for the
FDA (sparse-FPCA / PACE) or Gaussian-process curve the production design would
use; the code notes where those would slot in.

**The seed set (operator-decided, 20 Jun 2026).** The known-good mediums =
drop <= 197 °C, second crack not reached (the 17 roasts mapping to
``artisan-01..22`` in #290). Two tiers are reported side by side so the operator
sees the boundary sensitivity:

* **core** — drop <= 195 °C, the confident core;
* **soft** — drop in (195, 197] °C, a down-weighted tier (good roasts do occur
  there, but 197 sits in the densest drop-BT bin). The soft tier is **not**
  hard-excluded; ``--soft-weight`` (default 0.5) down-weights it in the pooled
  ``core+soft`` curve.

**Privacy (AGENTS.md, same as #290).** The raw ``.alog`` logs are the operator's
personal data and are **never committed**. This script reads them read-only and
emits only aggregate / anonymised output: a registered reference curve described
as per-landmark numbers, per-origin breakdowns where N allows, the inter-origin
correlation finding, and the tier sensitivity. No raw telemetry, no
bean-identifying data; per-roast rows are keyed by the same anonymised id as #290.

Usage::

    python scripts/reference_curve_prototype.py \\
        --logs-dir "~/Library/Mobile Documents/com~apple~CloudDocs/roasting" \\
        --manifest docs/advisor/artisan-testset-manifest.json \\
        --out docs/research/reference-curve-prototype-2026-06-20.md
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Artisan ``timeindex`` slots: [CHARGE, DRY_END, FCs, FCe, SCs, SCe, DROP, COOL].
_CHARGE, _DRY_END, _FCS, _FCE, _SCS, _SCE, _DROP, _COOL = range(8)
# Sentinel Artisan writes for a "no reading" sample in a temperature series.
_ARTISAN_NODATA = -1.0
# RoR regression window, seconds. The Hottop probe is coarse (~1/3 °C), so a
# short window is dominated by quantisation noise; 30 s is the Artisan smoothing
# scale, matching #290.
_ROR_WINDOW_S = 30.0

# The operator's drop tiers for the seed set (display BT, °C):
#   core: drop <= 195   (confident core)
#   soft: 195 < drop <= 197  (down-weighted)
#   anything > 197 is over-done and excluded (D42 §1, set 20 Jun 2026).
_CORE_DROP_C = 195.0
_SOFT_DROP_C = 197.0

# The landmark phase axis. Three registered segments. The first is anchored on
# the TURNING POINT, not the CHARGE mark: on the Hottop the probe at charge reads
# its own residual thermal state (a preheated drum, ~100-190 °C), then *dives* as
# the cold charge cools it to the turning point before the real roast ramp begins
# — so the charge BT is not a bean temperature and CHARGE->DRY-END is non-monotone
# (verified on all 47). Registering the meaningful trajectory on TURNING-POINT
# (the coolest post-charge sample) gives a monotone, physically-sensible warp.
# CHARGE is still recorded as a flagged-unreliable landmark for transparency.
# Second crack is never reached on this corpus, so FCs is the canonical FC mark
# (as in #290). Each segment gets ``_NODES_PER_SEGMENT`` interior steps; segment
# boundaries are shared nodes.
_SEGMENT_NAMES: tuple[str, ...] = ("dry", "maillard", "develop")
_NODES_PER_SEGMENT = 8

# Origin inference from the .alog FILENAME (the in-file ``beans`` field is empty
# across the corpus). Lifted verbatim from #290 so the two analyses agree on
# origin; more specific tokens first.
_ORIGIN_RULES: list[tuple[tuple[str, ...], str]] = [
    (("au-nica", "nicaragua", "nicragua"), "Nicaragua"),
    (("costarica", "hermosa"), "Costa Rica (Hermosa)"),
    (("brasil-ferm", "brasil-fm"), "Brazil (fermented/anaerobic)"),
    (("brazil", "brasil"), "Brazil"),
    (("taiwan", "tw_", "tw-", "nantou", "nantouu", "alisan", "alishan"), "Taiwan (Nantou/Alishan)"),
    (("jamaica", "jamaice"), "Jamaica (Blue Mountain)"),
    (("kona",), "Hawaii Kona"),
    (("cuba", "cub-", "cub_"), "Cuba"),
    (("vietnam",), "Vietnam"),
    (("indo", "ind2", "ind-", "ind_"), "Indonesia"),
    (("australia", "au-so", "au-"), "Australia (AMBIGUOUS)"),
]


# ---------------------------------------------------------------------------
# Parsing / registration data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredRoast:
    """One known-good roast warped onto the common landmark phase axis.

    Attributes:
        anon_id: Stable anonymised id (short hash of the roast UUID), matching
            #290 so the two analyses share ids.
        origin: Filename-inferred origin (a guess; never used to label a roast).
        drop_bt: Display bean temperature at drop (°C); selects the tier.
        dev_time_s: Development time (drop minus first-crack), seconds — a
            classification feature, matching #290.
        dev_temp_rise_c: Bean-temperature rise across development
            (``drop_bt - first_crack_bt``), °C — a classification feature.
        sc_reached: Whether second crack was marked (never true on this corpus,
            kept for the #290-identical classification rule).
        tier: ``"core"`` (drop <= 195 °C) or ``"soft"`` (195 < drop <= 197 °C).
        landmark_bt: Display bean temperature at each landmark, °C — ``charge``
            (a flagged probe-thermal-state artifact, not a bean temperature),
            ``turning_point`` (the meaningful start of the ramp, anchoring node 0),
            ``dry_end``, ``first_crack``, ``drop``.
        node_bt: Registered bean temperature at every phase-axis node (°C),
            parallel to the module's node grid; node 0 is the turning point.
        node_ror: Registered RoR at every phase-axis node (°C/min), derived on
            the roast's real clock with a 30 s window then sampled at the nodes.
    """

    anon_id: str
    origin: str
    drop_bt: float
    dev_time_s: float
    dev_temp_rise_c: float
    sc_reached: bool
    tier: str
    landmark_bt: dict[str, float]
    node_bt: list[float]
    node_ror: list[float]


def load_alog(path: Path) -> dict[str, Any]:
    """Parse an Artisan ``.alog`` (an ``ast.literal_eval``-able dict) into a dict.

    Args:
        path: The ``.alog`` file.

    Returns:
        The parsed profile dictionary.

    Raises:
        ValueError: If the file does not parse into a ``dict``.
    """
    parsed = ast.literal_eval(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} did not parse into a dict")
    return cast("dict[str, Any]", parsed)


def anon_id(profile: dict[str, Any], fallback: str) -> str:
    """Derive the stable anonymised id from the roast UUID (matching #290).

    Args:
        profile: A parsed Artisan profile dict.
        fallback: A string to hash if the profile carries no UUID.

    Returns:
        A short hex digest, stable per roast, revealing nothing about the bean.
    """
    seed = str(profile.get("roastUUID") or fallback)
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def infer_origin(basename: str) -> str:
    """Infer the coffee origin from a ``.alog`` filename (a guess; see #290).

    Args:
        basename: The file's base name.

    Returns:
        A canonical origin label, or ``"Unknown"`` if no token matches.
    """
    name = basename.lower()
    for tokens, origin in _ORIGIN_RULES:
        if any(token in name for token in tokens):
            return origin
    if ("25-07-16" in name or "25-07-17" in name) and ("b1" in name or "b3" in name):
        return "Cuba"
    return "Unknown"


def _phase_axis() -> list[tuple[str, float]]:
    """Build the common phase-axis nodes as ``(segment, fraction)`` pairs.

    Each segment spans fraction ``0..1`` between its two landmarks; the start
    landmark of every segment after the first is dropped to avoid duplicating a
    shared boundary node, so the axis is monotone across the whole roast.

    Returns:
        The ordered list of ``(segment_name, within_segment_fraction)`` nodes.
    """
    axis: list[tuple[str, float]] = []
    for s_i, name in enumerate(_SEGMENT_NAMES):
        start = 0 if s_i == 0 else 1
        for k in range(start, _NODES_PER_SEGMENT + 1):
            axis.append((name, k / _NODES_PER_SEGMENT))
    return axis


# Module-level axis: every registered roast and every aggregate shares it.
PHASE_AXIS: list[tuple[str, float]] = _phase_axis()


def _interp(timex: list[float], series: list[float], t: float) -> float:
    """Linearly interpolate ``series`` (parallel to ``timex``) at real time ``t``.

    No-data samples (the Artisan ``-1`` sentinel) are skipped so a dropout does
    not pull the interpolation to a spurious value.

    Args:
        timex: The roast timeline (ascending, seconds).
        series: A temperature series parallel to ``timex``.
        t: The real time to evaluate at (seconds).

    Returns:
        The interpolated value (clamped to the series ends outside its range).
    """
    pts = [(timex[i], series[i]) for i in range(len(timex)) if series[i] != _ARTISAN_NODATA]
    if not pts:  # pragma: no cover - defensive: an all-no-data BT series never reaches here
        return float("nan")
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:  # pragma: no cover - defensive: the warp never samples past drop
        return pts[-1][1]
    lo = 0
    hi = len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    t0, v0 = pts[lo]
    t1, v1 = pts[hi]
    if t1 == t0:  # pragma: no cover - defensive: ascending timex has no duplicate stamps
        return v0
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def _ror_at(timex: list[float], series: list[float], t: float) -> float:
    """RoR (°C/min) at real time ``t``, over a trailing ``_ROR_WINDOW_S`` window.

    Computed on the roast's *real* clock (not the warped axis) so the rate keeps
    its physical units, then sampled at the landmark node's real time.

    Args:
        timex: The roast timeline (ascending, seconds).
        series: A temperature series parallel to ``timex``.
        t: The real time to evaluate at (seconds).

    Returns:
        The rate of rise in °C/min (``0.0`` for a degenerate window).
    """
    t_prev = max(timex[0], t - _ROR_WINDOW_S)
    span = t - t_prev
    if span <= 0:  # pragma: no cover - defensive: every landmark sits >0 s after charge
        return 0.0
    return (_interp(timex, series, t) - _interp(timex, series, t_prev)) / span * 60.0


def _event_real_time(
    timeindex: list[int], slot: int, timex: list[float], charge_t: float
) -> float | None:
    """Resolve a ``timeindex`` slot to its real time, or ``None`` if unusable.

    Args:
        timeindex: The roast's ``timeindex`` array.
        slot: One of the ``_CHARGE`` .. ``_COOL`` slot constants.
        timex: The roast timeline.
        charge_t: The charge time (seconds).

    Returns:
        The event's real time (seconds), or ``None`` when unset / out of range /
        earlier than charge.
    """
    idx = timeindex[slot]
    unset = -1 if slot == _CHARGE else 0
    if idx == unset or not 0 <= idx < len(timex):
        return None
    t = timex[idx]
    if slot != _CHARGE and t < charge_t:  # pragma: no cover - defensive: marks sit after charge
        return None
    return t


def register_roast(profile: dict[str, Any], fallback_id: str) -> RegisteredRoast | None:
    """Landmark-register one parsed ``.alog`` onto the common phase axis.

    Resolves the four landmarks (CHARGE / DRY-END / FIRST-CRACK / DROP), warps
    the roast's real time onto the shared phase axis segment by segment, and
    samples registered BT and RoR at every node. Returns ``None`` for any roast
    missing a landmark or with a non-monotone landmark sequence.

    Args:
        profile: A parsed Artisan profile dict.
        fallback_id: A string used to seed the anonymised id if no UUID exists.

    Returns:
        The registered roast, or ``None`` when it cannot be registered.
    """
    timex = [float(v) for v in profile.get("timex", [])]
    bt = [float(v) for v in profile.get("temp2", [])]
    timeindex = [int(v) for v in profile.get("timeindex", [])]
    if len(timeindex) < 8 or not timex or len(bt) != len(timex):
        return None

    charge_t = _event_real_time(timeindex, _CHARGE, timex, 0.0)
    if charge_t is None:
        return None
    marks = {
        _CHARGE: charge_t,
        _DRY_END: _event_real_time(timeindex, _DRY_END, timex, charge_t),
        _FCS: _event_real_time(timeindex, _FCS, timex, charge_t),
        _DROP: _event_real_time(timeindex, _DROP, timex, charge_t),
    }
    if any(marks[s] is None for s in (_DRY_END, _FCS, _DROP)):
        return None
    sc_reached = _event_real_time(timeindex, _SCS, timex, charge_t) is not None

    # The turning point: the coolest BT sample at or after charge (the probe dives
    # as the cold charge cools it, then climbs). This, not the charge mark, is the
    # meaningful start of the roast ramp on a Hottop.
    charge_idx = timeindex[_CHARGE]
    drop_t_for_tp = _event_real_time(timeindex, _DROP, timex, charge_t)
    drop_idx = next(
        (i for i in range(len(timex)) if timex[i] >= cast("float", drop_t_for_tp)),
        len(timex) - 1,
    )
    turning_bt = bt[charge_idx]
    turning_t = charge_t
    for i in range(charge_idx, drop_idx + 1):
        if bt[i] == _ARTISAN_NODATA:
            continue
        if bt[i] < turning_bt:
            turning_bt = bt[i]
            turning_t = timex[i]
    # A strictly increasing landmark sequence is required for a valid warp, with
    # the turning point (not the charge mark) anchoring the first segment.
    seq = [turning_t, marks[_DRY_END], marks[_FCS], marks[_DROP]]
    for a, b in zip(seq, seq[1:], strict=False):
        if a is None or b is None or b <= a:
            return None

    seg_times = {
        "dry": (turning_t, cast("float", marks[_DRY_END])),
        "maillard": (cast("float", marks[_DRY_END]), cast("float", marks[_FCS])),
        "develop": (cast("float", marks[_FCS]), cast("float", marks[_DROP])),
    }
    node_bt: list[float] = []
    node_ror: list[float] = []
    for seg_name, frac in PHASE_AXIS:
        t0, t1 = seg_times[seg_name]
        t = t0 + (t1 - t0) * frac
        node_bt.append(round(_interp(timex, bt, t), 2))
        node_ror.append(round(_ror_at(timex, bt, t), 2))

    drop_bt = round(_interp(timex, bt, cast("float", marks[_DROP])), 1)
    fc_bt = round(_interp(timex, bt, cast("float", marks[_FCS])), 1)
    tier = "core" if drop_bt <= _CORE_DROP_C else "soft"
    landmark_bt = {
        "charge": round(bt[charge_idx], 1),
        "turning_point": round(turning_bt, 1),
        "dry_end": round(_interp(timex, bt, cast("float", marks[_DRY_END])), 1),
        "first_crack": fc_bt,
        "drop": drop_bt,
    }
    return RegisteredRoast(
        anon_id=anon_id(profile, fallback_id),
        origin=infer_origin(fallback_id),
        drop_bt=drop_bt,
        dev_time_s=round(cast("float", marks[_DROP]) - cast("float", marks[_FCS]), 1),
        dev_temp_rise_c=round(drop_bt - fc_bt, 1),
        sc_reached=sc_reached,
        tier=tier,
        landmark_bt=landmark_bt,
        node_bt=node_bt,
        node_ror=node_ror,
    )


# ---------------------------------------------------------------------------
# Aggregation: registered mean + uncertainty band
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceCurve:
    """A pooled registered reference curve with an uncertainty band.

    All series are parallel to :data:`PHASE_AXIS`. The band is the per-node
    spread of the registered population; a production build would replace the
    naive mean/SD with sparse-FPCA (PACE) or a Gaussian-process posterior
    (D42 §2.1), but the registered mean + percentile band is the honest
    prototype stand-in.

    Attributes:
        n: Number of roasts pooled (effective integer count; weighting is a
            separate concern handled by :func:`build_reference`).
        weight_sum: Sum of roast weights actually pooled (``n`` for unweighted).
        mean_bt: Weighted per-node mean registered BT (°C).
        sd_bt: Per-node BT standard deviation (°C) — the band half-width proxy.
        p10_bt: Per-node 10th-percentile registered BT (°C).
        p90_bt: Per-node 90th-percentile registered BT (°C).
        mean_ror: Weighted per-node mean registered RoR (°C/min).
        sd_ror: Per-node RoR standard deviation (°C/min).
    """

    n: int
    weight_sum: float
    mean_bt: list[float]
    sd_bt: list[float]
    p10_bt: list[float]
    p90_bt: list[float]
    mean_ror: list[float]
    sd_ror: list[float]


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    """Weighted mean of ``values`` (returns NaN for a zero total weight).

    Args:
        values: The values.
        weights: Parallel non-negative weights.

    Returns:
        The weighted mean.
    """
    total = sum(weights)
    if total <= 0:  # pragma: no cover - defensive: pooled roasts always carry weight >0
        return float("nan")
    return sum(v * w for v, w in zip(values, weights, strict=True)) / total


def _weighted_sd(values: list[float], weights: list[float], mean: float) -> float:
    """Weighted sample standard deviation about ``mean``.

    Uses reliability weights with Bessel-style correction
    (``1 - sum(w^2)/sum(w)^2`` in the denominator) so an unweighted call matches
    the ordinary sample SD; degenerate cases return ``0.0``.

    Args:
        values: The values.
        weights: Parallel non-negative weights.
        mean: The (weighted) mean to deviate about.

    Returns:
        The weighted standard deviation.
    """
    total = sum(weights)
    sq = sum(w * w for w in weights)
    denom = total - (sq / total if total > 0 else 0.0)
    if denom <= 0:  # pragma: no cover - defensive: a single non-zero weight is the only way in
        return 0.0
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights, strict=True)) / denom
    return math.sqrt(max(0.0, var))


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile of ``values`` (unweighted).

    Args:
        values: The values (any order).
        q: Quantile in ``[0, 1]``.

    Returns:
        The percentile value (NaN for an empty input).
    """
    if not values:  # pragma: no cover - defensive: a node always has >=1 pooled value
        return float("nan")
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - pos) + s[hi] * (pos - lo)


def build_reference(
    roasts: list[RegisteredRoast], weights: dict[str, float] | None = None
) -> ReferenceCurve:
    """Pool registered roasts into a reference curve with an uncertainty band.

    Args:
        roasts: The registered roasts to pool.
        weights: Optional per-``anon_id`` weight (default ``1.0`` each). The soft
            tier is down-weighted through this in the ``core+soft`` pool.

    Returns:
        The pooled reference curve. An empty input yields an all-NaN curve of
        the correct length.
    """
    nodes = len(PHASE_AXIS)
    if not roasts:
        nan = [float("nan")] * nodes
        return ReferenceCurve(0, 0.0, nan, nan[:], nan[:], nan[:], nan[:], nan[:])
    w = [(weights or {}).get(r.anon_id, 1.0) for r in roasts]
    mean_bt: list[float] = []
    sd_bt: list[float] = []
    p10: list[float] = []
    p90: list[float] = []
    mean_ror: list[float] = []
    sd_ror: list[float] = []
    for j in range(nodes):
        bt_j = [r.node_bt[j] for r in roasts]
        ror_j = [r.node_ror[j] for r in roasts]
        m_bt = _weighted_mean(bt_j, w)
        m_ror = _weighted_mean(ror_j, w)
        mean_bt.append(round(m_bt, 2))
        sd_bt.append(round(_weighted_sd(bt_j, w, m_bt), 2))
        p10.append(round(_percentile(bt_j, 0.10), 2))
        p90.append(round(_percentile(bt_j, 0.90), 2))
        mean_ror.append(round(m_ror, 2))
        sd_ror.append(round(_weighted_sd(ror_j, w, m_ror), 2))
    return ReferenceCurve(
        n=len(roasts),
        weight_sum=round(sum(w), 2),
        mean_bt=mean_bt,
        sd_bt=sd_bt,
        p10_bt=p10,
        p90_bt=p90,
        mean_ror=mean_ror,
        sd_ror=sd_ror,
    )


def landmark_node_indices() -> dict[str, int]:
    """Phase-axis node index of each landmark (turning-point / dry-end / FC / drop).

    Node 0 is the turning point (the first segment is anchored there, not on the
    charge mark — see the registration note).

    Returns:
        A mapping of landmark name to its index in :data:`PHASE_AXIS`.
    """
    out = {"turning_point": 0}
    for i, (seg, frac) in enumerate(PHASE_AXIS):
        if seg == "dry" and frac == 1.0:
            out["dry_end"] = i
        elif seg == "maillard" and frac == 1.0:
            out["first_crack"] = i
        elif seg == "develop" and frac == 1.0:
            out["drop"] = i
    return out


# ---------------------------------------------------------------------------
# Inter-origin correlation (the §5.1 open question)
# ---------------------------------------------------------------------------


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation between two equal-length series (NaN if degenerate).

    Args:
        a: First series.
        b: Second series.

    Returns:
        The correlation coefficient in ``[-1, 1]``, or NaN when a series is
        constant or the lengths differ.
    """
    if len(a) != len(b) or len(a) < 2:  # pragma: no cover - defensive: curves share the axis
        return float("nan")
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:  # pragma: no cover - defensive: a flat de-trended curve is degenerate
        return float("nan")
    return num / (da * db)


@dataclass(frozen=True)
class CorrelationFinding:
    """The inter-origin correlation result that decides pooling vs per-origin.

    Attributes:
        origins: The origins compared (those with N >= ``min_n``), in order.
        bt_matrix: Pairwise Pearson correlation of registered *de-trended* BT
            curves (each origin curve minus the pooled mean curve, so the
            comparison is of shape deviation, not the shared overall ramp).
        mean_offdiag_bt: Mean of the off-diagonal BT correlations (the pooling
            signal — high means origins share a shape, so pooling helps).
        verdict: A short pool-vs-per-origin reading of ``mean_offdiag_bt``.
    """

    origins: list[str]
    bt_matrix: list[list[float]]
    mean_offdiag_bt: float
    verdict: str


def inter_origin_correlation(
    roasts: list[RegisteredRoast], pooled: ReferenceCurve, min_n: int = 2
) -> CorrelationFinding:
    """Correlate per-origin registered curves to test whether pooling helps.

    Each origin with at least ``min_n`` roasts gets a registered mean BT curve;
    the pooled mean is subtracted from every origin curve first, so the
    correlation measures whether origins share the same *shape deviation* from
    the population (the multi-task-GP "genuinely correlated origins" condition,
    D42 §2.2), not merely the trivially-shared overall ramp that would make every
    origin look correlated.

    Args:
        roasts: All registered roasts (any tier).
        pooled: The pooled reference curve (its ``mean_bt`` is the de-trend
            baseline).
        min_n: Minimum roasts an origin needs to be included.

    Returns:
        The correlation finding (origins, matrix, mean off-diagonal, verdict).
    """
    by_origin: dict[str, list[RegisteredRoast]] = {}
    for r in roasts:
        by_origin.setdefault(r.origin, []).append(r)
    origins = sorted(o for o, rs in by_origin.items() if len(rs) >= min_n and o != "Unknown")

    detrended: dict[str, list[float]] = {}
    for o in origins:
        ref = build_reference(by_origin[o])
        detrended[o] = [ref.mean_bt[j] - pooled.mean_bt[j] for j in range(len(PHASE_AXIS))]

    matrix: list[list[float]] = []
    offdiag: list[float] = []
    for i, oi in enumerate(origins):
        row: list[float] = []
        for k, ok in enumerate(origins):
            c = 1.0 if i == k else _pearson(detrended[oi], detrended[ok])
            row.append(round(c, 2))
            if i < k and not math.isnan(c):
                offdiag.append(c)
        matrix.append(row)

    mean_off = statistics.fmean(offdiag) if offdiag else float("nan")
    return CorrelationFinding(origins, matrix, round(mean_off, 3), verdict_for(mean_off))


def verdict_for(mean_offdiag: float) -> str:
    """Map a mean off-diagonal inter-origin correlation to a pooling verdict.

    This is the report's load-bearing pool-vs-per-origin reading (D42 §2.2 /
    §5.1): multi-task sharing helps only for genuinely correlated origins, so a
    strongly positive mean justifies partial pooling, a near-zero mean means the
    "free lunch" does not apply, and a negative mean warns that pooling would
    average away real per-origin structure.

    Args:
        mean_offdiag: The mean off-diagonal Pearson correlation of the origins'
            de-trended registered curves (NaN if too few origins).

    Returns:
        A short verdict sentence.
    """
    if math.isnan(mean_offdiag):
        return "insufficient origins with N >= 2 to judge — cannot decide pooling here"
    if mean_offdiag >= 0.5:
        return (
            "origins' shape deviations are POSITIVELY correlated on average — "
            "partial pooling (shrink few-roast origins toward the population curve) "
            "is justified for the prototype"
        )
    if mean_offdiag <= -0.1:
        return (
            "origins' shape deviations are largely UNcorrelated / anti-correlated — "
            "pooling risks averaging away real per-origin structure; prefer "
            "per-origin (or learn the correlations, never assume uniform sharing)"
        )
    return (
        "origins' shape deviations are WEAKLY correlated (near zero) — the "
        "multi-task 'free lunch' does not apply; pool only with learned, not "
        "assumed, correlations, and lean per-origin where N allows"
    )


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def _landmark_table(curve: ReferenceCurve, title: str) -> list[str]:
    """Render the per-landmark registered BT/RoR + band table for one curve.

    Args:
        curve: The reference curve.
        title: The table's heading.

    Returns:
        Markdown lines.
    """
    idx = landmark_node_indices()
    order = [
        ("turning_point", "TURNING-POINT"),
        ("dry_end", "DRY-END"),
        ("first_crack", "FIRST-CRACK"),
        ("drop", "DROP"),
    ]
    lines = [
        f"**{title}** (N={curve.n}, pooled weight {curve.weight_sum:g})",
        "",
        "| landmark | reg BT °C | BT SD °C | BT p10–p90 °C | reg RoR °C/min | RoR SD |",
        "|---|---|---|---|---|---|",
    ]
    for key, label in order:
        j = idx[key]
        lines.append(
            f"| {label} | {curve.mean_bt[j]:.1f} | {curve.sd_bt[j]:.2f} | "
            f"{curve.p10_bt[j]:.1f}–{curve.p90_bt[j]:.1f} | "
            f"{curve.mean_ror[j]:.1f} | {curve.sd_ror[j]:.2f} |"
        )
    lines.append("")
    return lines


def _full_node_table(curve: ReferenceCurve, title: str) -> list[str]:
    """Render the registered curve at every phase-axis node (the band detail).

    Args:
        curve: The reference curve.
        title: The table's heading.

    Returns:
        Markdown lines.
    """
    lines = [
        f"**{title} — every registered node**",
        "",
        "| node | segment | frac | mean BT °C | BT SD | p10 BT | p90 BT | mean RoR | RoR SD |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, (seg, frac) in enumerate(PHASE_AXIS):
        lines.append(
            f"| {i} | {seg} | {frac:.3f} | {curve.mean_bt[i]:.1f} | {curve.sd_bt[i]:.2f} | "
            f"{curve.p10_bt[i]:.1f} | {curve.p90_bt[i]:.1f} | "
            f"{curve.mean_ror[i]:.1f} | {curve.sd_ror[i]:.2f} |"
        )
    lines.append("")
    return lines


def _sensitivity_lines(core: ReferenceCurve, core_soft: ReferenceCurve) -> list[str]:
    """Quantify how much including the soft 196-197 °C tier moves the reference.

    Args:
        core: The core-only (drop <= 195 °C) reference.
        core_soft: The core+soft (drop <= 197 °C, soft down-weighted) reference.

    Returns:
        Markdown lines reporting the per-landmark BT shift.
    """
    idx = landmark_node_indices()
    diffs = [core_soft.mean_bt[j] - core.mean_bt[j] for j in range(len(PHASE_AXIS))]
    max_abs = max(abs(d) for d in diffs)
    lines = [
        "The two curves are built on the same phase axis; the table is the shift "
        "from including the soft tier (down-weighted) on top of the core.",
        "",
        f"- **Largest BT shift at any node: {max_abs:.2f} °C.**",
        f"- Core N={core.n}; core+soft N={core_soft.n} (pooled weight {core_soft.weight_sum:g}).",
        "",
        "| landmark | core BT °C | core+soft BT °C | Δ °C |",
        "|---|---|---|---|",
    ]
    for key, label in (
        ("turning_point", "TURNING-POINT"),
        ("dry_end", "DRY-END"),
        ("first_crack", "FIRST-CRACK"),
        ("drop", "DROP"),
    ):
        j = idx[key]
        lines.append(
            f"| {label} | {core.mean_bt[j]:.1f} | {core_soft.mean_bt[j]:.1f} | "
            f"{core_soft.mean_bt[j] - core.mean_bt[j]:+.2f} |"
        )
    lines.append("")
    return lines


def render_markdown(
    roasts: list[RegisteredRoast],
    soft_weight: float,
) -> str:
    """Render the full anonymised reference-curve prototype report.

    Args:
        roasts: All registered known-good roasts (core + soft tiers).
        soft_weight: The down-weight applied to soft-tier roasts in the
            ``core+soft`` pool.

    Returns:
        The Markdown report as a single string.
    """
    core_roasts = [r for r in roasts if r.tier == "core"]
    core = build_reference(core_roasts)
    weights = {r.anon_id: (soft_weight if r.tier == "soft" else 1.0) for r in roasts}
    core_soft = build_reference(roasts, weights=weights)

    by_origin: dict[str, list[RegisteredRoast]] = {}
    for r in roasts:
        by_origin.setdefault(r.origin, []).append(r)
    corr = inter_origin_correlation(roasts, core_soft)

    soft_n = len(roasts) - len(core_roasts)
    out: list[str] = []
    out.append("# Per-bean reference-curve prototype on the known-good mediums (D42 §7.1)")
    out.append("")
    out.append(
        "**Generated:** 2026-06-20 — offline reference-curve prototype "
        "(D42 §7 step 2, follow-on to #290). Pure data analysis: no LLM, no API "
        "key, no roaster, no network. Generated by "
        "`scripts/reference_curve_prototype.py`. The raw `.alog` logs are personal "
        "data and are **never committed**; only these aggregates and anonymised "
        "ids are."
    )
    out.append("")
    out.append("## What this is (and is not)")
    out.append("")
    out.append(
        "This prototypes the **§7.1 *target* curve**: a *landmark-registered* "
        "reference built from our known-good medium roasts, the thing a future "
        "controller would track anticipatorily. It is **not** the #223/#275 "
        "in-roast reference (D40, the roast's own telemetry to time *t*). The two "
        "are deliberately distinct (D42 §0)."
    )
    out.append("")
    out.append(
        "Registration is done **first** (D42 §2.1): naive cross-time averaging of "
        "roast trajectories is a documented failure mode (the mean resembles no "
        "real roast). Each roast is warped onto a common phase axis built from its "
        "own marks TURNING-POINT -> DRY-END -> FIRST-CRACK -> DROP "
        f"({_NODES_PER_SEGMENT} nodes per segment, {len(PHASE_AXIS)} nodes total), "
        "*then* the registered curves are pooled into a mean + percentile / SD "
        "band. The band is a prototype stand-in for the FDA (sparse-FPCA / PACE) "
        "or Gaussian-process posterior the production design would use — those "
        "slot in at `build_reference`."
    )
    out.append("")
    out.append(
        "**Registration finding (the charge landmark is unusable on the Hottop).** "
        "The first segment is anchored on the **turning point**, not the charge "
        "mark. On all 47 roasts the probe at charge reads its own residual thermal "
        "state (a preheated drum, 112-192 °C across the seed set), then *dives* as "
        "the cold charge cools it to the turning point before the real ramp begins "
        "— so the charge BT is not a bean temperature and CHARGE -> DRY-END is "
        "non-monotone. Warping on the turning point gives a monotone, "
        "physically-sensible curve; charge BT is kept only as a flagged artifact "
        "column. This is a concrete corpus-design input for D42 §4 (the event "
        "markers a production registration relies on)."
    )
    out.append("")

    out.append("## TL;DR")
    out.append("")
    idx = landmark_node_indices()
    dj = idx["drop"]
    fj = idx["first_crack"]
    out.append(
        f"- **Seed set:** {len(roasts)} known-good mediums "
        f"(core drop <= {_CORE_DROP_C:.0f} °C: {len(core_roasts)}; "
        f"soft {_CORE_DROP_C:.0f} < drop <= {_SOFT_DROP_C:.0f} °C: {soft_n}, "
        f"down-weighted x{soft_weight:g})."
    )
    out.append(
        f"- **Registered reference shape (core+soft):** "
        f"TURNING-POINT {core_soft.mean_bt[0]:.0f} °C -> "
        f"DRY-END {core_soft.mean_bt[idx['dry_end']]:.0f} °C -> "
        f"FIRST-CRACK {core_soft.mean_bt[fj]:.0f} °C "
        f"(±{core_soft.sd_bt[fj]:.1f} SD) -> "
        f"DROP {core_soft.mean_bt[dj]:.0f} °C (±{core_soft.sd_bt[dj]:.1f} SD); "
        f"RoR falls {core_soft.mean_ror[fj]:.1f} -> {core_soft.mean_ror[dj]:.1f} "
        "°C/min across development (the managed declining shape #229/#290 found, "
        "never a crash)."
    )
    out.append(
        f"- **Pooling verdict:** mean off-diagonal inter-origin shape correlation "
        f"= {corr.mean_offdiag_bt:.2f} across {len(corr.origins)} origins "
        f"(N >= 2) — {corr.verdict}."
    )
    soft_max = max(abs(core_soft.mean_bt[j] - core.mean_bt[j]) for j in range(len(PHASE_AXIS)))
    out.append(
        f"- **<=195-vs-<=197 sensitivity:** including the soft tier "
        f"(down-weighted) moves the registered BT by at most "
        f"**{soft_max:.2f} °C** at any node — "
        + ("negligible" if soft_max < 1.0 else "modest" if soft_max < 2.0 else "material")
        + ", so the 197 boundary is not load-bearing for the reference shape."
    )
    out.append("")

    out.append("## 1. The pooled registered reference curve")
    out.append("")
    out.extend(_landmark_table(core_soft, "Core+soft pooled reference (soft down-weighted)"))
    out.extend(_landmark_table(core, "Core-only pooled reference (drop <= 195 °C)"))
    out.append("### 1b. The full registered curve (band detail)")
    out.append("")
    out.extend(_full_node_table(core_soft, "Core+soft pooled reference"))
    out.append(
        "> The `frac` column is the within-segment fraction of the warped phase "
        "axis (0 = the segment's opening landmark, 1 = its closing landmark). "
        "Segment boundaries are shared nodes, so DRY-END / FIRST-CRACK / DROP each "
        "appear once. The node-0 (turning-point) RoR is negative because the 30 s "
        "trailing window straddles the pre-turning-point dive; RoR is ~0 *at* the "
        "turning point by definition and climbs from node 1 — read the curve's RoR "
        "from node 1 onward."
    )
    out.append("")

    out.append("## 2. Per-origin reference (filename-inferred origin, N >= 2)")
    out.append("")
    out.append(
        "Origin is the #290 filename guess (the in-file `beans` field is empty "
        "across the corpus); treat every per-origin cell as indicative, not a "
        "profile. Only the registered landmarks are shown — no raw telemetry."
    )
    out.append("")
    out.append(
        "| origin | N | DRY-END BT °C | FIRST-CRACK BT °C | DROP BT °C | RoR@FC | RoR@DROP |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for origin in sorted(by_origin, key=lambda o: (-len(by_origin[o]), o)):
        rs = by_origin[origin]
        if len(rs) < 2 or origin == "Unknown":
            continue
        ref = build_reference(rs)
        out.append(
            f"| {origin} | {len(rs)} | {ref.mean_bt[idx['dry_end']]:.1f} | "
            f"{ref.mean_bt[fj]:.1f} | {ref.mean_bt[dj]:.1f} | "
            f"{ref.mean_ror[fj]:.1f} | {ref.mean_ror[dj]:.1f} |"
        )
    out.append("")
    singles = sorted(o for o, rs in by_origin.items() if len(rs) == 1 and o != "Unknown")
    if singles:
        out.append(
            f"> Origins with only one known-good medium (omitted from the per-origin "
            f"table, folded into the pool): {', '.join(singles)}."
        )
        out.append("")

    out.append("## 3. Inter-origin correlation — does pooling help? (D42 §5.1)")
    out.append("")
    out.append(
        "Each origin's registered BT curve has the pooled mean subtracted first, "
        "so this correlates *shape deviation from the population*, not the "
        "trivially-shared overall ramp. Multi-task sharing helps only for "
        "genuinely correlated origins (D42 §2.2, the rho->1 condition), so this is "
        "the empirical check §5.1 asks for before committing to a pooling design."
    )
    out.append("")
    if corr.origins:
        header = "| origin | " + " | ".join(corr.origins) + " |"
        out.append(header)
        out.append("|" + "---|" * (len(corr.origins) + 1))
        for i, oi in enumerate(corr.origins):
            row = " | ".join(f"{corr.bt_matrix[i][k]:+.2f}" for k in range(len(corr.origins)))
            out.append(f"| {oi} | {row} |")
        out.append("")
    out.append(f"- **Mean off-diagonal correlation:** {corr.mean_offdiag_bt:.3f}.")
    out.append(f"- **Verdict:** {corr.verdict}.")
    out.append("")
    out.append(
        "> Caveat (D42 §6): per-origin N is tiny, this is a single roaster, and "
        "origin is a filename guess — the sign / rough magnitude is the signal "
        "here, not a precise coefficient. A perfect correlation on a tiny set "
        "would itself be a warning."
    )
    out.append("")

    out.append("## 4. <=195 vs <=197 sensitivity")
    out.append("")
    out.extend(_sensitivity_lines(core, core_soft))

    out.append("## 5. Per-roast registration inputs (anonymised)")
    out.append("")
    out.append(
        "The landmarks each roast was registered on, keyed by the #290 anonymised "
        "id. Display BT, °C. No raw telemetry, no bean-identifying data. The "
        "`charge BT` column is the probe artifact (note its spread, 112-192 °C — "
        "why node 0 is anchored on the turning point instead)."
    )
    out.append("")
    out.append(
        "| anon id | tier | charge BT (artifact) | TURNING-POINT BT | DRY-END BT | "
        "FIRST-CRACK BT | DROP BT |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for r in sorted(roasts, key=lambda x: (x.tier, x.drop_bt)):
        lm = r.landmark_bt
        out.append(
            f"| `{r.anon_id}` | {r.tier} | {lm['charge']:.1f} | "
            f"{lm['turning_point']:.1f} | {lm['dry_end']:.1f} | "
            f"{lm['first_crack']:.1f} | {lm['drop']:.1f} |"
        )
    out.append("")

    out.append("## 6. Honest caveats (carried from D42 §6 + the eval lessons)")
    out.append("")
    out.append(
        "- **Prototype, not production.** Registered mean + percentile band stands "
        "in for FDA (sparse-FPCA / PACE) or a GP curve; the band is a population "
        "spread, not a calibrated predictive interval."
    )
    out.append(
        "- **Tiny N, single machine, single operator** confound everything; the "
        "probe reads ~20-30 °C low, so only the *registered shape* transfers, "
        "never absolute temps (D42 §3b)."
    )
    out.append(
        "- **Origin is a filename guess** with empty in-file metadata — the real "
        "capture gap #290 flags (the Start Roast form must gain processing + "
        "altitude/density, D42 §4)."
    )
    out.append(
        "- **A perfect tiny-set agreement is a warning, not a win** (the recurring "
        "eval lesson, D42 §5.4); leave-one-roast-out with registration *inside* "
        "each fold is the honest next step."
    )
    out.append(
        "- **Degenerate case = the deterministic rules** (D35/#222) when no learned "
        "target exists; this curve is the non-degenerate target it would track, "
        "advisory-only preserved (the ML supplies the target, never actuation, "
        "D42 §2.3)."
    )
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Seed-set selection + CLI
# ---------------------------------------------------------------------------


def _zscores(values: list[float]) -> list[float]:
    """Z-score a list of values (population stdev; zeros if degenerate).

    Args:
        values: The raw values.

    Returns:
        The standardised values, in order.
    """
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0 for _ in values]
    return [(v - mean) / stdev for v in values]


def _kmeans_two(points: list[tuple[float, ...]], iterations: int = 100) -> list[int]:
    """Deterministic k=2 Lloyd's k-means (seeded at the first-feature extremes).

    Lifted from the #290 classifier so the two analyses agree on the medium /
    dark cut without an RNG.

    Args:
        points: Standardised feature vectors (one per roast).
        iterations: Maximum Lloyd iterations.

    Returns:
        A cluster assignment (``0`` / ``1``) per point.
    """
    if not points:  # pragma: no cover - defensive: classification only runs on a non-empty corpus
        return []
    dims = len(points[0])
    ordered = sorted(range(len(points)), key=lambda i: points[i][0])
    centroids = [list(points[ordered[0]]), list(points[ordered[-1]])]
    labels = [0 for _ in points]
    for _ in range(iterations):
        changed = False
        for i, p in enumerate(points):
            d0 = sum((p[k] - centroids[0][k]) ** 2 for k in range(dims))
            d1 = sum((p[k] - centroids[1][k]) ** 2 for k in range(dims))
            new = 0 if d0 <= d1 else 1
            if new != labels[i]:
                changed = True
            labels[i] = new
        for c in (0, 1):
            members = [points[i] for i in range(len(points)) if labels[i] == c]
            if members:
                centroids[c] = [statistics.fmean(m[k] for m in members) for k in range(dims)]
        if not changed:
            break
    return labels


def classify_medium(roasts: list[RegisteredRoast]) -> set[str]:
    """Identify the ``medium`` roasts using the exact #290 classification.

    Reproduces #290's base label: any second-crack roast is ``dark`` (none on
    this corpus); the rest split by k=2 k-means over z-scored
    ``(drop_bt, dev_time, dev_temp_rise)`` (z-scoring cancels the constant probe
    offset), the higher-drop / longer-development cluster being ``dark``. The
    >197 °C over-done promotion is applied on top, exactly as #290 — so the
    medium set returned here is the #290 known-good medium seed.

    Args:
        roasts: All successfully-registered roasts.

    Returns:
        The set of ``anon_id``s classified medium (and under the 197 °C line).
    """
    soft = [r for r in roasts if not r.sc_reached]
    medium: set[str] = set()
    if soft:
        fd = _zscores([r.drop_bt for r in soft])
        fv = _zscores([r.dev_time_s for r in soft])
        fr = _zscores([r.dev_temp_rise_c for r in soft])
        pts = [(fd[i], fv[i], fr[i]) for i in range(len(soft))]
        clusters = _kmeans_two(pts)
        mean_drop = [
            statistics.fmean([soft[i].drop_bt for i in range(len(soft)) if clusters[i] == c])
            if any(clusters[i] == c for i in range(len(soft)))
            else float("-inf")
            for c in (0, 1)
        ]
        dark_cluster = 0 if mean_drop[0] >= mean_drop[1] else 1
        for i, r in enumerate(soft):
            if clusters[i] != dark_cluster:
                medium.add(r.anon_id)
    # The >197 °C over-done promotion (and the SC hard rule) removes a roast from
    # the medium set, matching #290's classify_degrees layering.
    return {
        r.anon_id
        for r in roasts
        if r.anon_id in medium and not r.sc_reached and r.drop_bt <= _SOFT_DROP_C
    }


def select_known_good(roasts: list[RegisteredRoast]) -> list[RegisteredRoast]:
    """Select the known-good medium seed set, exactly as #290 defines it.

    The seed is the roasts classified ``medium`` by :func:`classify_medium`
    (k-means cut + the <=197 °C over-done line + second-crack-not-reached) — the
    17 roasts mapping to ``artisan-01..22`` in #290, **not** merely every roast
    under 197 °C (that would pull in low-drop but long-development dark-cluster
    roasts). The per-roast ``tier`` then encodes the core (<=195 °C) / soft
    (195-197 °C) split for the sensitivity analysis.

    Args:
        roasts: All successfully-registered roasts.

    Returns:
        The known-good mediums, ordered by drop bean temperature.
    """
    medium = classify_medium(roasts)
    return sorted((r for r in roasts if r.anon_id in medium), key=lambda r: r.drop_bt)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional explicit argument list (for tests).

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--logs-dir",
        default="~/Library/Mobile Documents/com~apple~CloudDocs/roasting",
        help="Directory of .alog files (read-only).",
    )
    parser.add_argument(
        "--manifest",
        default="docs/advisor/artisan-testset-manifest.json",
        help="Path to the artisan bake-off fixture manifest (unused by the "
        "registration; accepted for symmetry with the #290 script).",
    )
    parser.add_argument(
        "--out",
        default="docs/research/reference-curve-prototype-2026-06-20.md",
        help="Output Markdown path.",
    )
    parser.add_argument(
        "--soft-weight",
        type=float,
        default=0.5,
        help="Down-weight applied to soft-tier (195 < drop <= 197 °C) roasts in "
        "the core+soft pool.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Register the known-good mediums and write the prototype report.

    Args:
        argv: Optional explicit argument list (for tests).

    Returns:
        Process exit code (``0`` on success, ``1`` if no usable roasts).
    """
    args = parse_args(argv)
    logs_dir = os.path.expanduser(args.logs_dir)
    paths = sorted(Path(p) for p in glob.glob(os.path.join(logs_dir, "*.alog")))

    registered: list[RegisteredRoast] = []
    for path in paths:
        try:
            profile = load_alog(path)
        except (ValueError, SyntaxError):
            continue
        r = register_roast(profile, fallback_id=path.name)
        if r is not None:
            registered.append(r)

    seed = select_known_good(registered)
    if not seed:
        print(f"No known-good medium roasts found under {logs_dir}")
        return 1

    report = render_markdown(seed, soft_weight=args.soft_weight)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(
        f"Wrote {out_path} ({len(seed)} known-good mediums of {len(registered)} registered roasts)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
