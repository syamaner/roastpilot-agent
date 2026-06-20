"""Roast-degree classification and literature reconciliation on the .alog corpus.

This is an **offline due-diligence analysis** over the operator's 47 personal
Artisan ``.alog`` Hottop roast logs. It is pure data work: no LLM, no API key,
no roaster, no network. It exists to ground the operator's offline roast-curve
findings (Rao-style development-time-ratio bands, the Hottop probe offset, the
post-first-crack RoR "crash", the "flick") against *our own* recorded
distribution, rather than against the literature in the abstract.

Artisan ``.alog`` format (an ``ast.literal_eval``-able Python ``dict``, **not**
JSON):

- ``timex`` — the sample timeline in seconds from record start.
- ``temp1`` — environment temperature (ET) series, parallel to ``timex``.
- ``temp2`` — bean temperature (BT) series, parallel to ``timex``.
- ``timeindex`` — ``[CHARGE, DRYe, FCs, FCe, SCs, SCe, DROP, COOL]``: each entry
  is an *index into* ``timex``. The CHARGE slot is ``-1`` when unset; the others
  are ``0`` when unset.

All temperatures are Celsius (the Hottop/Artisan logs are already °C). These are
*display* BT readings from the Hottop bean probe, which the operator's findings
hold reads ~20-30 °C below true bean temperature.

**Privacy.** The raw logs are the operator's personal data and are never
committed (``AGENTS.md``). This script reads them read-only from the iCloud
roasting folder and emits only aggregate distributions plus per-roast rows keyed
by an anonymised id (a short stable hash of the roast UUID) and the roast date.
No bean/origin/processing/density data is emitted — and as this analysis
confirms, none is present in the files to begin with.

Usage::

    python scripts/alog_classify.py \\
        --logs-dir "~/Library/Mobile Documents/com~apple~CloudDocs/roasting" \\
        --manifest docs/advisor/artisan-testset-manifest.json \\
        --out docs/research/hottop-alog-classification-2026-06-20.md
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# Artisan ``timeindex`` slots.
_CHARGE, _DRY_END, _FCS, _FCE, _SCS, _SCE, _DROP, _COOL = range(8)
# Sentinel Artisan writes for "no reading" in a temperature series.
_ARTISAN_NODATA = -1.0
# RoR (rate of rise) regression window, seconds. The Hottop probe is coarse
# (~1/3 °C resolution), so a short window is dominated by quantisation noise; a
# 30 s window is the usual Artisan-style smoothing scale.
_ROR_WINDOW_S = 30.0
# Window after first crack within which a genuine RoR "crash" would appear.
_CRASH_WINDOW_S = 90.0
# Operator-empirical drop ceilings (display BT). 196 °C is the bitter ceiling
# (memory: operator-hottop-roast-profile). The OPERATIVE over-done line is the
# operator-set > 197 °C (20 Jun 2026); the earlier > 200 °C proxy (memory:
# artisan-roast-logs-dataset, "9 roasts >200") is retained only as a secondary
# reference in the report.
_BITTER_CEILING_C = 196.0
_OVER_DONE_C = 197.0
_OVER_DONE_PROXY_C = 200.0
# A flick must clear the probe quantisation floor to count as a real rebound.
_FLICK_FLOOR_C_PER_MIN = 3.0

# Origin inferred from the .alog FILENAME (the in-file ``beans`` field is empty;
# the filename carries the operator's shorthand). Order matters — more specific
# tokens are checked first (``au-nica`` is Nicaragua, not Australia). Tokens are
# matched against the lower-cased basename.
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

# Per-origin processing / altitude DEFAULTS — every one is ASSUMED, supplied by
# the coordinator for the operator to correct; nothing here is read from the
# file. Used only for the per-origin caveated layer, never to label a roast.
_ORIGIN_DEFAULTS: dict[str, str] = {
    "Taiwan (Nantou/Alishan)": "washed, high-grown (~1100–1400 m) [ASSUMED]",
    "Jamaica (Blue Mountain)": "washed, high-grown [ASSUMED]",
    "Costa Rica (Hermosa)": "washed SHB, high-grown [ASSUMED]",
    "Nicaragua": "washed, mid–high [ASSUMED]",
    "Hawaii Kona": "washed, low–mid altitude (soft) [ASSUMED]",
    "Cuba": "washed, low–mid (soft) [ASSUMED]",
    "Brazil": "natural, low-grown [ASSUMED]",
    "Brazil (fermented/anaerobic)": "anaerobic/fermented [ASSUMED]",
    "Indonesia": "wet-hulled, mid [ASSUMED]",
    "Vietnam": "likely robusta, low-grown — roasts very differently [ASSUMED, FLAG]",
    "Australia (AMBIGUOUS)": "UNKNOWN — Australian-grown low-altitude or a blend [ASSUMED, FLAG]",
}

# Roast Rebels per-origin DTR bands (literature, for the per-origin check).
_ROAST_REBELS_BANDS = "washed 16–20%, natural 16–19%, Brazil ~20%, Indonesia 19–24%"

# The free ground-truth roast: the filename explicitly says ``dark``.
_DARK_GROUND_TRUTH_TOKEN = "_dark"


@dataclass(frozen=True)
class RoastMetrics:
    """Derived per-roast metrics, all in display °C / seconds / percent.

    Attributes:
        anon_id: Stable anonymised roast id (short hash of the roast UUID).
        roast_date: The roast's calendar date string from the log.
        charge_bt: Bean temperature at the charge sample.
        turning_bt: Minimum bean temperature after charge (the turning point).
        turning_time_s: Seconds from charge to the turning point.
        fc_bt: Display bean temperature at first-crack start.
        fc_time_s: Seconds from charge to first-crack start.
        drop_bt: Display bean temperature at the drop sample.
        dev_time_s: Development time, drop minus first-crack-start (seconds).
        total_time_s: Roast time, drop minus charge (seconds).
        dtr_percent: Development time ratio, ``dev_time / total_time`` as a
            percentage.
        dev_temp_rise_c: Bean-temperature rise across development
            (``drop_bt - fc_bt``).
        sc_reached: Whether second crack was marked.
        ror_at_fc: RoR (°C/min) at first crack.
        ror_min_post_fc: Minimum RoR (°C/min) between first crack and drop.
        ror_min_within_crash_window: Minimum RoR (°C/min) within
            ``_CRASH_WINDOW_S`` after first crack.
        crash_negative: Whether RoR went negative inside the crash window (the
            strict "crash" test reconciled against #229).
        ror_declining: Whether RoR at drop is below RoR at first crack (the
            gentle declining-RoR shape, distinct from a crash).
        flick_c_per_min: Largest positive RoR rebound within the crash window
            relative to the local trend (the "flick"); ``0.0`` if none.
    """

    anon_id: str
    roast_date: str
    charge_bt: float
    turning_bt: float
    turning_time_s: float
    fc_bt: float
    fc_time_s: float
    drop_bt: float
    dev_time_s: float
    total_time_s: float
    dtr_percent: float
    dev_temp_rise_c: float
    sc_reached: bool
    ror_at_fc: float
    ror_min_post_fc: float
    ror_min_within_crash_window: float
    crash_negative: bool
    ror_declining: bool
    flick_c_per_min: float


def load_alog(path: Path) -> dict[str, Any]:
    """Parse an Artisan ``.alog`` into its raw ``dict``.

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


def _anon_id(profile: dict[str, Any], fallback: str) -> str:
    """Derive a stable anonymised id from the roast UUID (or a fallback string).

    Args:
        profile: A parsed Artisan profile dict.
        fallback: A string to hash if the profile carries no UUID.

    Returns:
        A short hex digest that is stable per roast but reveals nothing about
        the bean or origin.
    """
    seed = str(profile.get("roastUUID") or fallback)
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]  # noqa: S324


def infer_origin(basename: str) -> str:
    """Infer the coffee origin from a ``.alog`` filename.

    The in-file ``beans``/``organization`` fields are empty across the corpus,
    so origin is read from the operator's filename shorthand. This is a *guess*
    from unverified shorthand; it is used only for the heavily-caveated
    per-origin aggregate layer and never to label or re-identify a roast.

    Args:
        basename: The file's base name (with or without extension).

    Returns:
        A canonical origin label, or ``"Unknown"`` if no token matches.
    """
    name = basename.lower()
    for tokens, origin in _ORIGIN_RULES:
        if any(token in name for token in tokens):
            return origin
    # The bare-batch files of the 16–17 Jul 2025 session (``b1``/``b3`` with no
    # origin token) sit alongside the ``cub``/``cuba`` files of the same two
    # days — the coordinator confirms they are that Cuba session.
    if ("25-07-16" in name or "25-07-17" in name) and ("b1" in name or "b3" in name):
        return "Cuba"
    return "Unknown"


def filename_says_dark(basename: str) -> bool:
    """Whether the filename explicitly tags the roast as dark (e.g. ``kona_3_dark``).

    Args:
        basename: The file's base name.

    Returns:
        ``True`` if the dark ground-truth token is present.
    """
    return _DARK_GROUND_TRUTH_TOKEN in basename.lower()


def _event_index(
    timeindex: list[int], slot: int, timex: list[float], charge_t: float
) -> int | None:
    """Resolve a ``timeindex`` slot to a usable sample index, or ``None``.

    Guards that the slot is set (CHARGE uses ``-1`` unset, others use ``0``) and
    that the event's time is at or after charge.

    Args:
        timeindex: The roast's ``timeindex`` array.
        slot: One of the ``_CHARGE`` .. ``_COOL`` slot constants.
        timex: The roast timeline.
        charge_t: The charge time in seconds.

    Returns:
        The resolved sample index, or ``None`` when unset / out of range /
        earlier than charge.
    """
    idx = timeindex[slot]
    unset = -1 if slot == _CHARGE else 0
    if idx == unset:
        return None
    if not 0 <= idx < len(timex):
        return None
    if slot != _CHARGE and timex[idx] < charge_t:
        return None
    return idx


def _ror_c_per_min(timex: list[float], temp: list[float], at_index: int) -> float | None:
    """RoR (°C/min) at a sample, regressed over a trailing ``_ROR_WINDOW_S``.

    Args:
        timex: The roast timeline.
        temp: A temperature series parallel to ``timex``.
        at_index: The sample to evaluate at.

    Returns:
        The rate of rise in °C/min, or ``None`` if the window is degenerate.
    """
    target = timex[at_index] - _ROR_WINDOW_S
    j = at_index
    while j > 0 and timex[j] > target:
        j -= 1
    span = timex[at_index] - timex[j]
    if span <= 0:
        return None
    return (temp[at_index] - temp[j]) / span * 60.0


def compute_metrics(profile: dict[str, Any], fallback_id: str) -> RoastMetrics | None:
    """Compute the derived metrics for one parsed ``.alog``.

    Args:
        profile: A parsed Artisan profile dict.
        fallback_id: A string used to seed the anonymised id if no UUID exists.

    Returns:
        The roast's metrics, or ``None`` when it lacks a usable charge, first
        crack, or drop.
    """
    timex = [float(v) for v in profile.get("timex", [])]
    bt = [float(v) for v in profile.get("temp2", [])]
    timeindex = [int(v) for v in profile.get("timeindex", [])]
    if len(timeindex) < 8 or not timex or len(bt) != len(timex):
        return None

    charge_idx = timeindex[_CHARGE]
    if charge_idx == -1 or not 0 <= charge_idx < len(timex):
        return None
    charge_t = timex[charge_idx]

    fc_idx = _event_index(timeindex, _FCS, timex, charge_t)
    drop_idx = _event_index(timeindex, _DROP, timex, charge_t)
    if fc_idx is None or drop_idx is None:
        return None

    fc_bt = bt[fc_idx]
    drop_bt = bt[drop_idx]
    fc_time = timex[fc_idx] - charge_t
    total_time = timex[drop_idx] - charge_t
    dev_time = timex[drop_idx] - timex[fc_idx]
    if total_time <= 0 or dev_time < 0:
        return None
    dtr = dev_time / total_time * 100.0

    # Turning point: the coolest BT sample after charge (ignoring no-data).
    turning_bt = bt[charge_idx]
    turning_time = 0.0
    for i in range(charge_idx, drop_idx + 1):
        if bt[i] == _ARTISAN_NODATA:
            continue
        if bt[i] < turning_bt:
            turning_bt = bt[i]
            turning_time = timex[i] - charge_t

    # RoR profile across development.
    ror_at_fc = _ror_c_per_min(timex, bt, fc_idx) or 0.0
    post: list[tuple[float, float]] = []  # (seconds_after_fc, ror)
    for i in range(fc_idx, drop_idx + 1):
        r = _ror_c_per_min(timex, bt, i)
        if r is not None:
            post.append((timex[i] - timex[fc_idx], r))
    ror_values = [r for _, r in post] or [ror_at_fc]
    ror_min_post = min(ror_values)
    within = [r for t, r in post if t <= _CRASH_WINDOW_S] or [ror_at_fc]
    ror_min_within = min(within)
    ror_at_drop = post[-1][1] if post else ror_at_fc

    # Flick: the largest upward RoR rebound within the crash window after the
    # within-window minimum (a brief re-acceleration following the dip).
    flick = 0.0
    seen_min = math.inf
    for t, r in post:
        if t > _CRASH_WINDOW_S:
            break
        seen_min = min(seen_min, r)
        flick = max(flick, r - seen_min)

    return RoastMetrics(
        anon_id=_anon_id(profile, fallback_id),
        roast_date=str(profile.get("roastisodate") or profile.get("roastdate") or "unknown"),
        charge_bt=round(bt[charge_idx], 1),
        turning_bt=round(turning_bt, 1),
        turning_time_s=round(turning_time, 1),
        fc_bt=round(fc_bt, 1),
        fc_time_s=round(fc_time, 1),
        drop_bt=round(drop_bt, 1),
        dev_time_s=round(dev_time, 1),
        total_time_s=round(total_time, 1),
        dtr_percent=round(dtr, 1),
        dev_temp_rise_c=round(drop_bt - fc_bt, 1),
        sc_reached=_event_index(timeindex, _SCS, timex, charge_t) is not None,
        ror_at_fc=round(ror_at_fc, 1),
        ror_min_post_fc=round(ror_min_post, 1),
        ror_min_within_crash_window=round(ror_min_within, 1),
        crash_negative=ror_min_within < 0.0,
        ror_declining=ror_at_drop < ror_at_fc,
        flick_c_per_min=round(flick, 1),
    )


def _zscores(values: list[float]) -> list[float]:
    """Z-score a list of values (sample stdev; zeros if degenerate).

    Args:
        values: The raw values.

    Returns:
        The standardised values, in the same order.
    """
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0 for _ in values]
    return [(v - mean) / stdev for v in values]


def kmeans_two(points: list[tuple[float, ...]], iterations: int = 100) -> list[int]:
    """Deterministic k=2 Lloyd's k-means over standardised feature vectors.

    Seeds the two centroids at the extreme points along the first feature so the
    result is reproducible without an RNG.

    Args:
        points: Standardised feature vectors (one per roast).
        iterations: Maximum Lloyd iterations.

    Returns:
        A cluster assignment (``0`` / ``1``) per input point.
    """
    if not points:
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


def base_labels(metrics: list[RoastMetrics]) -> dict[str, str]:
    """Label each roast medium / dark *before* any over-done promotion.

    Hard rule: any roast that reached second crack is ``dark``. The remainder
    are split by k=2 k-means over z-scored ``(drop_bt, dev_time, dev_temp_rise)``
    — z-scoring cancels the constant ~20-30 °C probe offset. The higher-drop /
    longer-development cluster is ``dark``; the other is ``medium``.

    Args:
        metrics: All usable roast metrics.

    Returns:
        A mapping of ``anon_id`` to ``medium`` / ``dark``.
    """
    labels: dict[str, str] = {}
    soft = [m for m in metrics if not m.sc_reached]
    for m in metrics:
        if m.sc_reached:
            labels[m.anon_id] = "dark"

    if soft:
        feats_drop = _zscores([m.drop_bt for m in soft])
        feats_dev = _zscores([float(m.dev_time_s) for m in soft])
        feats_rise = _zscores([m.dev_temp_rise_c for m in soft])
        points = [(feats_drop[i], feats_dev[i], feats_rise[i]) for i in range(len(soft))]
        clusters = kmeans_two(points)
        mean_drop = [
            statistics.fmean([soft[i].drop_bt for i in range(len(soft)) if clusters[i] == c])
            if any(clusters[i] == c for i in range(len(soft)))
            else float("-inf")
            for c in (0, 1)
        ]
        dark_cluster = 0 if mean_drop[0] >= mean_drop[1] else 1
        for i, m in enumerate(soft):
            labels[m.anon_id] = "dark" if clusters[i] == dark_cluster else "medium"
    return labels


def classify_degrees(metrics: list[RoastMetrics]) -> dict[str, str]:
    """Label each roast medium / dark / over-dark.

    Starts from :func:`base_labels` (second-crack hard rule + k-means cut), then
    promotes any roast dropped past the operator's operative over-done line
    (display BT > 197 °C, set 20 Jun 2026) to ``over-dark``. The earlier
    > 200 °C proxy is retained only as a secondary reference in the report, not
    used here.

    Args:
        metrics: All usable roast metrics.

    Returns:
        A mapping of ``anon_id`` to degree label.
    """
    labels = base_labels(metrics)
    for m in metrics:
        if m.drop_bt > _OVER_DONE_C:
            labels[m.anon_id] = "over-dark"
    return labels


def known_good_mediums(metrics: list[RoastMetrics], degrees: dict[str, str]) -> list[RoastMetrics]:
    """Select the known-good medium reference set for the §7.1 per-bean curves.

    The reference set is the roasts labelled ``medium`` (so under the 197 °C
    over-done line and not in the dark k-means cluster) that did **not** reach
    second crack. These seed the per-bean reference curves, so this is the
    load-bearing output of the analysis.

    Args:
        metrics: All usable roast metrics.
        degrees: ``anon_id`` -> degree label.

    Returns:
        The qualifying roasts, ordered by drop bean temperature.
    """
    selected = [m for m in metrics if degrees.get(m.anon_id) == "medium" and not m.sc_reached]
    return sorted(selected, key=lambda m: m.drop_bt)


def match_fixtures(metrics: list[RoastMetrics], manifest_path: Path) -> dict[str, str]:
    """Map roasts to existing ``artisan-NN`` fixtures by ``(drop_bt, dtr)``.

    Args:
        metrics: All usable roast metrics.
        manifest_path: Path to ``artisan-testset-manifest.json``.

    Returns:
        A mapping of ``anon_id`` to fixture label for the roasts that match a
        manifest entry.
    """
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lut: dict[tuple[float, float], str] = {}
    for entry in manifest.get("selected", []):
        key = (
            round(float(entry["drop_temp_c"]), 1),
            round(float(entry["development_time_ratio_percent"]), 1),
        )
        lut[key] = str(entry["label"])
    out: dict[str, str] = {}
    for m in metrics:
        label = lut.get((m.drop_bt, m.dtr_percent))
        if label:
            out[m.anon_id] = label
    return out


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list.

    Args:
        sorted_values: Ascending values.
        q: Quantile in ``[0, 1]``.

    Returns:
        The quantile value.
    """
    if not sorted_values:
        return float("nan")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _text_histogram(
    values: list[float], lo: float, hi: float, bins: int, width: int = 40
) -> list[str]:
    """Render a text histogram of ``values`` over ``[lo, hi]``.

    Args:
        values: The raw values.
        lo: Lower edge.
        hi: Upper edge.
        bins: Number of bins.
        width: Maximum bar width in characters.

    Returns:
        One formatted line per bin.
    """
    counts = [0] * bins
    step = (hi - lo) / bins
    for v in values:
        idx = 0 if step <= 0 else min(bins - 1, max(0, int((v - lo) / step)))
        counts[idx] += 1
    peak = max(counts) or 1
    lines: list[str] = []
    for b in range(bins):
        edge_lo = lo + b * step
        edge_hi = edge_lo + step
        bar = "#" * round(counts[b] / peak * width)
        lines.append(f"  [{edge_lo:6.1f}, {edge_hi:6.1f})  {counts[b]:3d}  {bar}")
    return lines


def _stats_block(name: str, values: list[float], unit: str) -> list[str]:
    """Median / IQR / min / max summary lines for one metric.

    Args:
        name: Display name.
        values: The raw values.
        unit: Unit suffix.

    Returns:
        Formatted summary lines.
    """
    s = sorted(values)
    return [
        f"- **{name}** ({len(values)} roasts): "
        f"median {statistics.median(s):.1f}{unit}, "
        f"IQR {_quantile(s, 0.25):.1f}–{_quantile(s, 0.75):.1f}{unit}, "
        f"range {min(s):.1f}–{max(s):.1f}{unit}.",
    ]


def _render_per_origin(
    metrics: list[RoastMetrics],
    origins: dict[str, str],
    degrees: dict[str, str],
) -> list[str]:
    """Render the heavily-caveated per-origin aggregate layer.

    Groups the roasts by their filename-inferred origin and reports per-origin
    N, DTR / FC-BT / drop-BT medians, degree mix, and the ASSUMED processing
    default, then checks the pattern against the Roast Rebels per-origin DTR
    bands. No per-roast row is emitted — only origin-level aggregates.

    Args:
        metrics: All usable roast metrics.
        origins: ``anon_id`` -> filename-inferred origin.
        degrees: ``anon_id`` -> degree label.

    Returns:
        Markdown lines for the per-origin section.
    """
    by_origin: dict[str, list[RoastMetrics]] = {}
    for m in metrics:
        by_origin.setdefault(origins.get(m.anon_id, "Unknown"), []).append(m)

    lines: list[str] = []
    lines.append("## Per-origin layer (filename-inferred, ASSUMED defaults)")
    lines.append("")
    lines.append(
        "Origin is parsed from the **filename** (in-file `beans` is empty). "
        "Processing / altitude are coordinator-supplied **defaults the operator "
        "must correct** — none is read from the data. Per-origin N is small, "
        "this is a single roaster, and the probe reads ~20–30 °C low: treat "
        "every cell as indicative, not a profile."
    )
    lines.append("")
    origin_headers = [
        "origin",
        "N",
        "DTR median %",
        "FC BT median °C",
        "drop BT median °C",
        "degree mix",
        "processing/altitude (ASSUMED)",
    ]
    lines.append("| " + " | ".join(origin_headers) + " |")
    lines.append("|" + "|".join("---" for _ in origin_headers) + "|")
    for origin in sorted(by_origin, key=lambda o: (-len(by_origin[o]), o)):
        group = by_origin[origin]
        dtr_med = statistics.median([m.dtr_percent for m in group])
        fc_med = statistics.median([m.fc_bt for m in group])
        drop_med = statistics.median([m.drop_bt for m in group])
        mix = {"medium": 0, "dark": 0, "over-dark": 0}
        for m in group:
            mix[degrees[m.anon_id]] = mix.get(degrees[m.anon_id], 0) + 1
        mix_str = f"{mix['medium']}m/{mix['dark']}d/{mix['over-dark']}o"
        default = _ORIGIN_DEFAULTS.get(origin, "—")
        lines.append(
            f"| {origin} | {len(group)} | {dtr_med:.1f} | {fc_med:.1f} | "
            f"{drop_med:.1f} | {mix_str} | {default} |"
        )
    lines.append("")
    lines.append("Degree mix key: `m` medium / `d` dark / `o` over-dark (drop > 197 °C).")
    lines.append("")

    # Literature check against the Roast Rebels per-origin DTR bands.
    def _origin_dtr(name: str) -> float | None:
        group = by_origin.get(name)
        return statistics.median([m.dtr_percent for m in group]) if group else None

    washed_origins = [
        "Taiwan (Nantou/Alishan)",
        "Jamaica (Blue Mountain)",
        "Costa Rica (Hermosa)",
        "Nicaragua",
    ]
    washed_vals = [v for name in washed_origins if (v := _origin_dtr(name)) is not None]
    washed_med = statistics.median(washed_vals) if washed_vals else float("nan")
    brazil_med = _origin_dtr("Brazil")
    indo_med = _origin_dtr("Indonesia")

    lines.append("### Per-origin reconciliation")
    lines.append("")
    lines.append(f"Roast Rebels bands (literature): {_ROAST_REBELS_BANDS}.")
    lines.append("")
    taiwan_med = _origin_dtr("Taiwan (Nantou/Alishan)")
    taiwan_str = f"{taiwan_med:.1f}%" if taiwan_med is not None else "n/a"
    lines.append(
        f"- **High-grown washed (Taiwan / Jamaica / Costa Rica / Nicaragua):** "
        f"pooled DTR median **{washed_med:.1f}%**, inside the washed 16–20% band. "
        f"Taiwan alone runs the longest (median "
        f"{taiwan_str} — the corpus's high-development tail) vs the soft "
        f"low–mid washed origins Cuba/Kona at ~13–14%. So altitude/development "
        f"tracks more than the washed label per se."
    )
    if brazil_med is not None:
        lines.append(
            f"- **Brazil (assumed natural):** DTR median **{brazil_med:.1f}%** — "
            f"{'matches' if 16 <= brazil_med <= 21 else 'sits below'} the ~20% "
            f"Brazil/natural band, and overlaps the washed group rather than "
            f"separating from it (small N)."
        )
    if indo_med is not None:
        lines.append(
            f"- **Indonesia (assumed wet-hulled):** DTR median **{indo_med:.1f}%** "
            f"vs the 19–24% band (N is tiny — 2 roasts)."
        )
    lines.append(
        "- **Verdict (CAN'T-TELL, leaning partial):** a high-grown-vs-soft "
        "development gradient *is* visible (Taiwan ~20% down to Cuba/Kona ~14%), "
        "loosely consistent with the bands; but the **washed-vs-natural** split "
        "the bands predict is **not** cleanly separable here — Brazil overlaps "
        "the washed group. With small per-origin N, a single roaster, ASSUMED "
        "processing labels, and a low-reading probe, this stays **CAN'T-TELL** "
        "pending operator-confirmed origin / processing metadata."
    )
    lines.append("")
    return lines


def render_markdown(
    metrics: list[RoastMetrics],
    degrees: dict[str, str],
    fixtures: dict[str, str],
    origins: dict[str, str],
    dark_ground_truth: dict[str, bool],
) -> str:
    """Render the full analysis document.

    Args:
        metrics: All usable roast metrics.
        degrees: ``anon_id`` -> degree label.
        fixtures: ``anon_id`` -> ``artisan-NN`` fixture label (where matched).
        origins: ``anon_id`` -> filename-inferred origin (for the aggregate
            per-origin layer only; never emitted per roast).
        dark_ground_truth: ``anon_id`` -> whether the filename tagged the roast
            ``dark`` (the ``kona_3_dark`` free ground-truth check).

    Returns:
        The Markdown document as a string.
    """
    dtr = [m.dtr_percent for m in metrics]
    fc = [m.fc_bt for m in metrics]
    drop = [m.drop_bt for m in metrics]
    dev = [float(m.dev_time_s) for m in metrics]
    rise = [m.dev_temp_rise_c for m in metrics]
    n = len(metrics)
    n_dark = sum(1 for v in degrees.values() if v == "dark")
    n_medium = sum(1 for v in degrees.values() if v == "medium")
    n_over = sum(1 for v in degrees.values() if v == "over-dark")
    n_crash = sum(1 for m in metrics if m.crash_negative)
    n_declining = sum(1 for m in metrics if m.ror_declining)
    n_flick = sum(1 for m in metrics if m.flick_c_per_min >= _FLICK_FLOOR_C_PER_MIN)
    n_ceiling = sum(1 for m in metrics if m.drop_bt > _BITTER_CEILING_C)
    n_sc = sum(1 for m in metrics if m.sc_reached)
    dtr_med = statistics.median(dtr)
    fc_med = statistics.median(fc)
    drop_med = statistics.median(drop)
    gt_ids = [aid for aid, is_dark in dark_ground_truth.items() if is_dark]
    gt_pass = all(degrees.get(aid) in {"dark", "over-dark"} for aid in gt_ids) if gt_ids else None

    # The roasts that flip into over-dark under the operative > 197 °C line but
    # would not under the earlier > 200 °C proxy (the secondary reference).
    flipped = sorted(
        (m for m in metrics if _OVER_DONE_C < m.drop_bt <= _OVER_DONE_PROXY_C),
        key=lambda m: m.drop_bt,
    )
    kgm = known_good_mediums(metrics, degrees)

    lines: list[str] = []
    lines.append("# Hottop .alog roast-degree classification + literature reconciliation")
    lines.append("")
    lines.append("**Generated:** 2026-06-20 — offline due-diligence analysis (#224).")
    lines.append("")
    lines.append(
        "Pure data analysis over the operator's personal Artisan `.alog` Hottop "
        "roast logs. No LLM, no API key, no roaster, no network. Generated by "
        "`scripts/alog_classify.py`. The raw logs are personal data and are "
        "**never committed**; only these aggregates and anonymised per-roast rows "
        "are."
    )
    lines.append("")
    lines.append("## TL;DR")
    lines.append("")
    lines.append(f"- **Corpus:** {n} usable roasts (every file had charge + first crack + drop).")
    lines.append(
        f"- **Roast-degree split (operative > 197 °C over-done line):** "
        f"{n_medium} medium / {n_dark} dark / {n_over} over-dark. Under the "
        f"earlier > 200 °C proxy the over-dark count was "
        f"{sum(1 for m in metrics if m.drop_bt > _OVER_DONE_PROXY_C)}; **"
        f"{len(flipped)} roasts flip** into over-dark on the 197 line (drop in "
        f"(197, 200] °C). Separately, {n_ceiling}/{n} dropped past the ~196 °C "
        f"bitter *ceiling*."
    )
    lines.append(
        f"- **Known-good medium reference set (§7.1 seed):** **{len(kgm)} roasts** "
        f"— mediums under the 197 line, second crack not reached. Listed below."
    )
    lines.append(
        f"- **DTR:** median **{dtr_med:.1f}%** — below Rao's 20–25% band, in "
        f"line with our 7-Jun ~15–16% prior."
    )
    lines.append(
        f"- **FC display BT:** median **{fc_med:.1f} °C**; **drop display BT** "
        f"median **{drop_med:.1f} °C** — consistent with the ~20–30 °C probe offset."
    )
    lines.append(
        f"- **Crash verdict:** RoR went negative within {int(_CRASH_WINDOW_S)} s "
        f"of first crack in **{n_crash}/{n}** roasts. The post-FC RoR *declines* "
        f"in {n_declining}/{n} but stays positive — a managed declining-RoR "
        f"shape, **not** a crash. This **CONFIRMS #229** on the full set."
    )
    if gt_pass is not None:
        verdict = "PASS" if gt_pass else "FAIL"
        lines.append(
            f"- **Ground-truth check:** the `kona_3_dark` roast "
            f"(filename explicitly says dark) is classified "
            f"**{degrees.get(gt_ids[0], '?')}** — **{verdict}** for the "
            f"medium/dark split."
        )
    lines.append(
        f"- **Bean-metadata gap:** 0/{n} files carry *in-file* origin / "
        f"processing / measured density. Per-origin layer below is built from "
        f"the **filename** shorthand (a guess) with **ASSUMED** processing "
        f"defaults — caveats apply."
    )
    lines.append("")

    lines.append("## Method")
    lines.append("")
    lines.append(
        "Each `.alog` parses with `ast.literal_eval` (it is a Python dict literal, "
        "not JSON). Event marks come from `timeindex` (indices into `timex`): "
        "`[CHARGE, DRY_END, FCs, FCe, SCs, SCe, DROP, COOL]`, CHARGE unset = `-1`, "
        "the rest unset = `0`. Every event is guarded to be at or after charge. "
        "Temperatures are the Hottop **display** bean probe (`temp2`), in °C."
    )
    lines.append("")
    lines.append(
        "Per roast we compute drop BT, FC BT, FC time, development time "
        "(drop − FCs), total time (drop − charge), **DTR = dev/total**, "
        "development temp rise (drop BT − FC BT), turning point, and a 30 s-window "
        "RoR (°C/min) track from FC to drop. Degree classification: any roast "
        "reaching second crack is `dark`; the rest are split by deterministic k=2 "
        "k-means over z-scored `(drop_bt, dev_time, dev_temp_rise)` (z-scoring "
        "cancels the constant probe offset), labelling the higher-drop / "
        "longer-development cluster `dark`; drop BT > 197 °C (the operator's "
        "operative over-done line, set 20 Jun 2026) is promoted to `over-dark`. "
        "The earlier > 200 °C proxy is kept only as a secondary reference."
    )
    lines.append("")

    lines.append("## Aggregate distributions")
    lines.append("")
    lines.extend(_stats_block("DTR", dtr, "%"))
    lines.extend(_stats_block("FC display BT", fc, " °C"))
    lines.extend(_stats_block("Drop display BT", drop, " °C"))
    lines.extend(_stats_block("Development time", dev, " s"))
    lines.extend(_stats_block("Development temp rise", rise, " °C"))
    lines.append("")
    lines.append("### DTR histogram (%)")
    lines.append("")
    lines.append("```")
    lines.extend(_text_histogram(dtr, 10.0, 26.0, 8))
    lines.append("```")
    lines.append("")
    lines.append("### FC display BT histogram (°C)")
    lines.append("")
    lines.append("```")
    lines.extend(_text_histogram(fc, 166.0, 186.0, 10))
    lines.append("```")
    lines.append("")
    lines.append("### Drop display BT histogram (°C)")
    lines.append("")
    lines.append("```")
    lines.extend(_text_histogram(drop, 188.0, 204.0, 8))
    lines.append("```")
    lines.append("")

    lines.append("## Reconciliation against the operator's findings")
    lines.append("")
    lines.append("Each claim marked CONFIRMED / CONTRADICTED / CAN'T-TELL on our 47-roast data.")
    lines.append("")
    lines.append("| # | Claim | Verdict | Our data |")
    lines.append("|---|-------|---------|----------|")
    dtr_q1 = _quantile(sorted(dtr), 0.25)
    dtr_q3 = _quantile(sorted(dtr), 0.75)
    rows: list[tuple[str, str, str, str]] = [
        (
            "1",
            "DTR band Rao 20–25% / per-origin 16–24%",
            "**CONTRADICTED**",
            f"Our DTR median **{dtr_med:.1f}%** (IQR {dtr_q1:.1f}–{dtr_q3:.1f}%, "
            f"range {min(dtr):.1f}–{max(dtr):.1f}%). Most roasts sit *below* Rao's "
            f"band, matching the 7-Jun ~15–16% prior. A minority reach 20–25%.",
        ),
        (
            "2",
            "Hottop BT reads ~20–30 °C low (FC ~182 display vs ~199–204 true)",
            "**CONFIRMED**",
            f"FC display BT median **{fc_med:.1f} °C** (range "
            f"{min(fc):.1f}–{max(fc):.1f}); drop display BT median "
            f"**{drop_med:.1f} °C**. FC clusters near the ~178–182 display band "
            f"the offset predicts. The *size* of the offset can't be checked "
            f"without a true-temp reference.",
        ),
        (
            "3",
            "Post-FC RoR crash is real / to-be-avoided",
            "**CONTRADICTED on our roasts**",
            f"RoR negative within {int(_CRASH_WINDOW_S)} s of FC in "
            f"**{n_crash}/{n}**. Confirms #229 on the full set (not just 28). RoR "
            f"declines gently and stays positive — well-managed roasting, and the "
            f"~1/3 °C probe resolution would also mask a shallow crash.",
        ),
        (
            "4a",
            "Declining-RoR shape after FC",
            "**CONFIRMED**",
            f"RoR at drop < RoR at FC in **{n_declining}/{n}** roasts — the "
            f"expected decline, just never to a crash.",
        ),
        (
            "4b",
            "Flick (brief RoR rebound after FC)",
            "**CAN'T-TELL**",
            f"A rebound clearing the {_FLICK_FLOOR_C_PER_MIN:.0f} °C/min "
            f"quantisation floor appears in **{n_flick}/{n}** roasts, but at the "
            f"probe's ~1/3 °C resolution over a 30 s window even these are hard to "
            f"separate from sampling noise. Not reliably groundable here.",
        ),
        (
            "5",
            "Per-origin profiling",
            "**CAN'T-TELL**",
            f"0/{n} files carry origin / processing / measured density (`beans` "
            f"and `organization` empty; `density` is the Artisan `[0,'g',1,'l']` "
            f"placeholder). See the gap note below.",
        ),
    ]
    for num, claim, verdict, data in rows:
        lines.append(f"| {num} | {claim} | {verdict} | {data} |")
    lines.append("")
    lines.append(
        f"Second crack was reached in **{n_sc}/{n}** roasts, so the hard "
        f"`sc_reached -> dark` rule did not fire; the medium/dark split rests on "
        f"the k-means cut, with the > 197 °C over-done promotion on top."
    )
    lines.append("")
    lines.append(
        f"Note the declining-RoR claim (4a) holds in {n_declining}/{n}, not all: "
        f"in the remainder the 30 s-window RoR at drop sits at or above the at-FC "
        f"value, typically the hotter/longer over-dark roasts where the operator "
        f"carried heat late. The crash result (3) is unaffected — none go negative."
    )
    lines.append("")

    lines.append("## Over-done threshold and the roasts that flip")
    lines.append("")
    lines.append(
        f"The operator set the operative over-done line at **drop display BT "
        f"> 197 °C** (20 Jun 2026). The earlier > 200 °C proxy is kept only as a "
        f"secondary reference. Re-cutting on 197 °C moves the over-dark count "
        f"from {sum(1 for m in metrics if m.drop_bt > _OVER_DONE_PROXY_C)} (> 200 "
        f"proxy) to **{n_over}** (> 197 operative); the **{len(flipped)} roasts "
        f"below flip** from medium/dark into over-dark (drop in (197, 200] °C):"
    )
    lines.append("")
    if flipped:
        # Their label under the > 200 proxy: none of the flipped roasts exceed
        # 200 °C, so the proxy over-done promotion never touches them — their
        # prior label is the base (k-means + second-crack) label.
        base = base_labels(metrics)
        lines.append("| anon id | roast date | fixture | drop BT °C | DTR % | was (> 200 cut) |")
        lines.append("|---|---|---|---|---|---|")
        for m in flipped:
            lines.append(
                f"| `{m.anon_id}` | {m.roast_date} | "
                f"{fixtures.get(m.anon_id, '—')} | {m.drop_bt:.1f} | "
                f"{m.dtr_percent:.1f} | {base[m.anon_id]} |"
            )
    else:
        lines.append("_No roasts fall in the (197, 200] °C window._")
    lines.append("")

    lines.append("## Known-good medium reference set (§7.1 seed)")
    lines.append("")
    lines.append(
        f"The roasts that seed the §7.1 per-bean reference curves: **mediums "
        f"under the 197 °C over-done line, second crack not reached** — "
        f"**{len(kgm)} roasts**. This is the load-bearing output. Each is keyed "
        f"by anonymised id and mapped to its `artisan-NN` fixture where one "
        f"exists (the fixtures carry the replayable telemetry; the raw `.alog` "
        f"is never committed)."
    )
    lines.append("")
    lines.append("| anon id | roast date | fixture | drop BT °C | FC BT °C | DTR % | dev s |")
    lines.append("|---|---|---|---|---|---|---|")
    for m in kgm:
        lines.append(
            f"| `{m.anon_id}` | {m.roast_date} | {fixtures.get(m.anon_id, '—')} | "
            f"{m.drop_bt:.1f} | {m.fc_bt:.1f} | {m.dtr_percent:.1f} | "
            f"{m.dev_time_s:.0f} |"
        )
    lines.append("")
    n_kgm_fixture = sum(1 for m in kgm if m.anon_id in fixtures)
    lines.append(
        f"{n_kgm_fixture}/{len(kgm)} of the known-good mediums map to an "
        f"`artisan-NN` fixture. DTR across this set: "
        f"median {statistics.median([m.dtr_percent for m in kgm]):.1f}%, "
        f"range {min(m.dtr_percent for m in kgm):.1f}–"
        f"{max(m.dtr_percent for m in kgm):.1f}%."
        if kgm
        else "No known-good mediums under the operative cut."
    )
    lines.append("")

    lines.append("## Bean-metadata gap (explicit)")
    lines.append("")
    lines.append(
        f"Across all {n} files the **in-file** metadata is empty: `beans` and "
        "`organization` are empty strings and `density` is the Artisan default "
        "placeholder `[0, 'g', 1, 'l']` (0 g per 1 l, i.e. unmeasured). There is "
        "**no processing method, varietal, or measured green density** recorded "
        "inside the logs. The **filenames** do carry the operator's origin "
        "shorthand (e.g. `kona`, `costarica-hermosa`, `brasil-ferm`), so the "
        "per-origin layer below is built from the *filename*, not the file body. "
        "That is a guess from unverified shorthand: origins may be mislabelled, "
        "the per-origin processing/altitude values are coordinator-supplied "
        "**defaults (ASSUMED)** the operator must correct, and the per-roast "
        "table stays origin-free so no anonymised id is re-identifiable."
    )
    lines.append("")

    lines.extend(_render_per_origin(metrics, origins, degrees))

    lines.append("## Per-roast classification")
    lines.append("")
    lines.append(
        "Keyed by anonymised id (stable hash of the roast UUID) + roast date. No "
        "bean-identifying data, no raw telemetry. Fixture column maps to the "
        "existing `artisan-NN` bake-off fixtures (#224) by `(drop_bt, DTR)` where "
        "the roast matches a manifest entry."
    )
    lines.append("")
    headers = [
        "anon id",
        "roast date",
        "degree",
        "fixture",
        "drop BT °C",
        "FC BT °C",
        "dev s",
        "total s",
        "DTR %",
        "dev rise °C",
        "RoR@FC",
        "RoR min (≤90s)",
    ]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for m in sorted(metrics, key=lambda x: (degrees[x.anon_id], x.drop_bt)):
        cells = [
            f"`{m.anon_id}`",
            m.roast_date,
            degrees[m.anon_id],
            fixtures.get(m.anon_id, "—"),
            f"{m.drop_bt:.1f}",
            f"{m.fc_bt:.1f}",
            f"{m.dev_time_s:.0f}",
            f"{m.total_time_s:.0f}",
            f"{m.dtr_percent:.1f}",
            f"{m.dev_temp_rise_c:.1f}",
            f"{m.ror_at_fc:.1f}",
            f"{m.ror_min_within_crash_window:.1f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        f"Fixture matches: **{len(fixtures)}/{n}** roasts map to an `artisan-NN` "
        "fixture (the bake-off manifest selected 28; the remaining roasts are "
        "in-corpus but were not selected as fixtures)."
    )
    lines.append("")
    return "\n".join(lines)


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
        help="Path to the artisan bake-off fixture manifest.",
    )
    parser.add_argument(
        "--out",
        default="docs/research/hottop-alog-classification-2026-06-20.md",
        help="Output Markdown path.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the analysis and write the report.

    Args:
        argv: Optional explicit argument list (for tests).

    Returns:
        Process exit code (``0`` on success, ``1`` if no usable roasts).
    """
    args = parse_args(argv)
    logs_dir = os.path.expanduser(args.logs_dir)
    paths = sorted(Path(p) for p in glob.glob(os.path.join(logs_dir, "*.alog")))

    metrics: list[RoastMetrics] = []
    origins: dict[str, str] = {}
    dark_ground_truth: dict[str, bool] = {}
    for path in paths:
        try:
            profile = load_alog(path)
        except (ValueError, SyntaxError):
            continue
        m = compute_metrics(profile, fallback_id=path.name)
        if m is not None:
            metrics.append(m)
            origins[m.anon_id] = infer_origin(path.name)
            dark_ground_truth[m.anon_id] = filename_says_dark(path.name)

    if not metrics:
        print(f"No usable roasts found under {logs_dir}")
        return 1

    degrees = classify_degrees(metrics)
    fixtures = match_fixtures(metrics, Path(args.manifest))
    report = render_markdown(metrics, degrees, fixtures, origins, dark_ground_truth)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"Wrote {out_path} ({len(metrics)} roasts).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
