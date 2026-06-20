"""Empirically validate in-roast curve features on the recorded roasts.

This is a **pure data-analysis** harness for issue #229 (D36 curve-insight
spike). It makes **no** LLM / API calls and needs no key. It reads the
operator's consolidated recorded roasts (the gitignored
``.artisan-fixtures/artisan-*/roast.jsonl`` set, 28 roasts) and scores the
*predictive / discriminative value* of three candidate derived features, so
#275 (the context builder) knows which are worth feeding the control loop and
which stay display-only:

1. **FC-ETA** — extrapolate bean temp + RoR toward the profile first-crack band
   at each pre-FC tick and compare the predicted FC time with the actual FC
   event. Reports the error distribution and how early a useful ETA appears.
2. **RoR curvature (crash / flick)** — the second derivative of bean-temp RoR;
   tests whether the crash / flick signatures actually appear around FC / early
   development, and whether they survive 5 s-sampled telemetry.
3. **Turning point + recovery** — post-charge bean-temp minimum (temp and time)
   and the recovery slope; correlated against downstream outcomes (FC timing,
   total roast time, drop temp) to answer the open "validate TP/recovery before
   trusting them" question.

The telemetry is recorded at ~1 s cadence; the live controller tick and the
research note both reason about **5 s** samples, so every feature is computed on
a 5 s-decimated view of each roast (the 1 s view is used only as a noise-floor
reference for the curvature question).

Temperatures are Celsius throughout (the Hottop / Artisan logs are already °C).
The fixtures are the operator's personal roast logs, so per ``AGENTS.md`` they
are **never** modified or committed; this script only reads them.

Run::

    .venv/bin/python scripts/curve_feature_eval.py
    .venv/bin/python scripts/curve_feature_eval.py --fixtures-dir .artisan-fixtures --json

The defaults print a human-readable report; ``--json`` emits the same numbers
as a machine-readable blob (used to regenerate the findings doc).
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

# --- Domain constants -------------------------------------------------------

#: Decimation cadence (seconds) for the live-tick view of every roast. The
#: controller ticks at 1 s but the curve-feature reasoning targets ~5 s samples
#: (research note Tier-3 item 8); validate on what the loop would actually see.
SAMPLE_SECONDS: float = 5.0

#: RoR least-squares span (seconds). Artisan's live RoR is a degree-1 polyfit
#: over a configurable span (``deltaBTspan`` default 20 s); we mirror that as a
#: low-lag estimator suitable for the ETA projection.
ROR_SPAN_SECONDS: float = 30.0

#: Curvature (RoR-of-RoR) span (seconds). The second derivative is noisier than
#: the first, so it is fit over a wider span to stay above the sensor floor.
CURVATURE_SPAN_SECONDS: float = 60.0

#: First-crack target band for the ETA, in bean °C. Derived from this dataset's
#: own FC-event bean temps (see ``derive_fc_band``); the operator profile is
#: FC ~170-180 °C. The midpoint is the extrapolation target; the band width is
#: used to report ETA error.
DEFAULT_FC_BAND_C: tuple[float, float] = (170.0, 181.0)

#: An ETA is "useful" if its predicted FC time lands within this tolerance of
#: the actual FC event. Chosen to bracket the 12-21 s audio-detector lag the ETA
#: is meant to anticipate / absorb.
ETA_USEFUL_TOLERANCE_SECONDS: float = 30.0

#: Lead-time buckets (seconds before actual FC) at which to summarise ETA error.
ETA_LEAD_BUCKETS_SECONDS: tuple[float, ...] = (180.0, 120.0, 90.0, 60.0, 30.0)


# --- Data model -------------------------------------------------------------


@dataclass(frozen=True)
class Roast:
    """One recorded roast, decimated to a fixed sample cadence.

    All arrays are parallel and ordered by time. Times are seconds from the
    start of recording (not from charge); ``charge_seconds`` etc. are absolute
    on the same clock.

    Attributes:
        name: Fixture directory name (e.g. ``artisan-01``).
        t: Sample times (seconds, recording clock).
        bean: Bean temperature (°C) per sample.
        env: Environment temperature (°C) per sample.
        heat: Heat duty (0-100 %) per sample.
        fan: Fan duty (0-100 %) per sample.
        charge_seconds: ``beans_added`` event time.
        fc_seconds: ``first_crack_detected`` event time.
        drop_seconds: ``beans_dropped`` event time.
        fc_bean_c: Bean temperature at the FC sample.
        drop_bean_c: Bean temperature at the drop sample.
    """

    name: str
    t: NDArray[np.float64]
    bean: NDArray[np.float64]
    env: NDArray[np.float64]
    heat: NDArray[np.float64]
    fan: NDArray[np.float64]
    charge_seconds: float
    fc_seconds: float
    drop_seconds: float
    fc_bean_c: float
    drop_bean_c: float

    @property
    def time_to_fc_seconds(self) -> float:
        """Charge-to-first-crack duration (seconds)."""
        return self.fc_seconds - self.charge_seconds

    @property
    def total_roast_seconds(self) -> float:
        """Charge-to-drop duration (seconds)."""
        return self.drop_seconds - self.charge_seconds

    @property
    def development_seconds(self) -> float:
        """First-crack-to-drop duration (seconds)."""
        return self.drop_seconds - self.fc_seconds


# --- Loading ----------------------------------------------------------------


def _nearest_index(times: NDArray[np.float64], target: float) -> int:
    """Return the index of the sample nearest ``target`` on ``times``."""
    return int(np.argmin(np.abs(times - target)))


def load_roast(fixture: Path, sample_seconds: float = SAMPLE_SECONDS) -> Roast:
    """Load and decimate one roast fixture.

    Reads the ``roast.jsonl`` telemetry + event rows, then resamples the
    telemetry onto a uniform ``sample_seconds`` grid by nearest-neighbour (the
    source is already ~1 s, so this is a clean decimation, not interpolation).

    Args:
        fixture: A roast fixture directory containing ``roast.jsonl``.
        sample_seconds: The output sample cadence in seconds.

    Returns:
        The decimated :class:`Roast`.

    Raises:
        ValueError: If the fixture lacks telemetry or any required event.
    """
    telemetry: list[dict[str, Any]] = []
    events: dict[str, float] = {}
    with (fixture / "roast.jsonl").open() as handle:
        for line in handle:
            row = json.loads(line)
            if row["type"] == "telemetry":
                telemetry.append(row)
            elif row["type"] == "event":
                events[str(row["kind"])] = float(row["monotonic_seconds"])
    missing = {"beans_added", "first_crack_detected", "beans_dropped"} - events.keys()
    if missing:
        raise ValueError(f"fixture {fixture} lacks required events: {sorted(missing)}")
    if not telemetry:
        raise ValueError(f"fixture {fixture} has no telemetry rows")

    raw_t = np.array([float(r["monotonic_seconds"]) for r in telemetry], dtype=np.float64)
    raw_bean = np.array([float(r["bean_temp_c"]) for r in telemetry], dtype=np.float64)
    raw_env = np.array([float(r["env_temp_c"]) for r in telemetry], dtype=np.float64)
    raw_heat = np.array([float(r["heat_level_percent"]) for r in telemetry], dtype=np.float64)
    raw_fan = np.array([float(r["fan_level_percent"]) for r in telemetry], dtype=np.float64)

    grid = np.arange(raw_t[0], raw_t[-1] + 1e-9, sample_seconds, dtype=np.float64)
    idx = np.array([_nearest_index(raw_t, g) for g in grid], dtype=np.int64)

    drop_i = _nearest_index(raw_t, events["beans_dropped"])
    fc_i = _nearest_index(raw_t, events["first_crack_detected"])
    return Roast(
        name=fixture.name,
        t=grid,
        bean=raw_bean[idx],
        env=raw_env[idx],
        heat=raw_heat[idx],
        fan=raw_fan[idx],
        charge_seconds=events["beans_added"],
        fc_seconds=events["first_crack_detected"],
        drop_seconds=events["beans_dropped"],
        fc_bean_c=float(raw_bean[fc_i]),
        drop_bean_c=float(raw_bean[drop_i]),
    )


def load_all(fixtures_dir: Path, sample_seconds: float = SAMPLE_SECONDS) -> list[Roast]:
    """Load every ``artisan-*`` roast under ``fixtures_dir`` (sorted by name)."""
    roasts = [
        load_roast(d, sample_seconds)
        for d in sorted(fixtures_dir.glob("artisan-*"))
        if (d / "roast.jsonl").exists()
    ]
    if not roasts:
        raise ValueError(f"no artisan-* roast fixtures found under {fixtures_dir}")
    return roasts


# --- Numerics ---------------------------------------------------------------


def slope_per_min(times: NDArray[np.float64], values: NDArray[np.float64]) -> float:
    """Least-squares slope of ``values`` vs ``times``, scaled to per-minute.

    A degree-1 ``polyfit`` over the supplied window (Artisan's live-RoR method),
    converted from per-second to per-minute. Returns ``nan`` for < 2 points.

    Args:
        times: Sample times (seconds).
        values: Parallel values (e.g. bean °C, or RoR °C/min).

    Returns:
        The slope per minute, or ``nan`` if undeterminable.
    """
    if times.size < 2:
        return math.nan
    coeffs = np.polyfit(times, values, 1)
    return float(coeffs[0]) * 60.0


def ror_series(
    t: NDArray[np.float64], values: NDArray[np.float64], span_seconds: float
) -> NDArray[np.float64]:
    """Rate-of-rise (°/min) at each sample via a trailing least-squares slope.

    For each index, fits a line over the trailing ``span_seconds`` window. The
    first samples (window not yet full) are ``nan``.

    Args:
        t: Sample times (seconds), uniformly spaced.
        values: Parallel series to differentiate.
        span_seconds: Trailing window width for the slope fit.

    Returns:
        A parallel array of slopes per minute (leading ``nan`` until the window
        fills).
    """
    out = np.full(t.shape, np.nan, dtype=np.float64)
    for i in range(t.size):
        lo = t[i] - span_seconds
        mask = (t <= t[i]) & (t >= lo)
        if int(np.count_nonzero(mask)) >= 2:
            out[i] = slope_per_min(t[mask], values[mask])
    return out


def pearson(x: NDArray[np.float64], y: NDArray[np.float64]) -> float:
    """Pearson correlation of two parallel arrays (``nan`` if degenerate)."""
    if x.size < 3 or float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return math.nan
    return float(np.corrcoef(x, y)[0, 1])


def rankdata(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """Average-rank transform (ties share the mean rank)."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=np.float64)
    ranks[order] = np.arange(1, values.size + 1, dtype=np.float64)
    # Average tied ranks.
    sorted_vals = values[order]
    i = 0
    while i < values.size:
        j = i
        while j + 1 < values.size and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        if j > i:
            ranks[order[i : j + 1]] = float(np.mean(np.arange(i + 1, j + 2)))
        i = j + 1
    return ranks


def spearman(x: NDArray[np.float64], y: NDArray[np.float64]) -> float:
    """Spearman rank correlation of two parallel arrays."""
    if x.size < 3:
        return math.nan
    return pearson(rankdata(x), rankdata(y))


def approx_two_sided_p(r: float, n: int) -> float:
    """A rough two-sided p-value for a correlation ``r`` at sample size ``n``.

    Uses the ``t = r * sqrt((n-2)/(1-r^2))`` statistic with a normal-tail
    approximation (no scipy dependency). This is **indicative only** — at N=28
    it is a sanity flag, not a rigorous test — and the findings doc treats it as
    such.

    Args:
        r: The correlation coefficient.
        n: The sample size.

    Returns:
        An approximate two-sided p-value, or ``nan`` if undeterminable.
    """
    if n < 3 or not math.isfinite(r) or abs(r) >= 1.0:
        return math.nan
    t = abs(r) * math.sqrt((n - 2) / (1.0 - r * r))
    # Normal-tail approximation of the two-sided p-value.
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(t / math.sqrt(2.0))))
    return max(0.0, min(1.0, p))


def percentile(values: list[float], q: float) -> float:
    """Percentile ``q`` (0-100) of ``values`` (``nan`` if empty)."""
    if not values:
        return math.nan
    return float(np.percentile(np.array(values, dtype=np.float64), q))


# --- FC band ----------------------------------------------------------------


def derive_fc_band(roasts: list[Roast]) -> tuple[float, float]:
    """Empirical FC band (10th-90th percentile of FC-event bean temps).

    The summary ``first_crack_temp_c`` field is unreliable in these fixtures
    (often the ambient/initial reading), so the band is taken from the bean
    temperature **at the FC event sample** across the set.

    Args:
        roasts: The loaded roasts.

    Returns:
        ``(low_c, high_c)`` band; the midpoint is the ETA extrapolation target.
    """
    fc_temps = [r.fc_bean_c for r in roasts]
    return percentile(fc_temps, 10.0), percentile(fc_temps, 90.0)


# --- Feature 1: FC-ETA ------------------------------------------------------


@dataclass
class EtaResult:
    """ETA accuracy aggregated across roasts.

    Attributes:
        fc_band_c: The FC band (low, high) used as the extrapolation target.
        fc_target_c: The band midpoint (the actual extrapolation target).
        per_lead_abs_median_s: Median |ETA error| (s) at each lead-time bucket.
        per_lead_abs_p90_s: 90th-percentile |ETA error| (s) at each bucket.
        per_lead_signed_median_s: Median signed ETA error (s; + = late) per
            bucket.
        per_lead_n_ticks: Tick count contributing to each bucket.
        first_useful_lead_median_s: Median (across roasts) of the earliest lead
            time at which the ETA first stays within tolerance through to FC.
        useful_tolerance_s: The "useful" tolerance used.
    """

    fc_band_c: tuple[float, float]
    fc_target_c: float
    per_lead_abs_median_s: dict[str, float]
    per_lead_abs_p90_s: dict[str, float]
    per_lead_signed_median_s: dict[str, float]
    per_lead_n_ticks: dict[str, int]
    first_useful_lead_median_s: float
    useful_tolerance_s: float


def eta_at_tick(
    now_t: float,
    bean_now: float,
    ror_now: float,
    accel_per_min2: float,
    target_c: float,
    use_quadratic: bool,
) -> float | None:
    """Predict the absolute FC time by extrapolating bean temp toward ``target_c``.

    Linear model: ``target = bean + RoR * dt`` (RoR in °C/min). Quadratic model
    adds the RoR acceleration (Artisan's post-5-min projection): solve
    ``target = bean + RoR*dt + 0.5*accel*dt^2``.

    Args:
        now_t: Current sample time (seconds).
        bean_now: Current bean temperature (°C).
        ror_now: Current bean RoR (°C/min).
        accel_per_min2: RoR acceleration (°C/min per min).
        target_c: The FC target temperature (°C).
        use_quadratic: Whether to use the quadratic (accel) projection.

    Returns:
        The predicted absolute FC time (seconds), or ``None`` if the projection
        does not reach the target ahead (RoR non-positive, or no real forward
        root).
    """
    gap = target_c - bean_now
    if gap <= 0:
        return now_t
    if not math.isfinite(ror_now) or ror_now <= 0:
        return None
    ror_per_s = ror_now / 60.0
    if not use_quadratic or not math.isfinite(accel_per_min2) or abs(accel_per_min2) < 1e-9:
        return now_t + gap / ror_per_s
    accel_per_s2 = accel_per_min2 / 3600.0
    # 0.5*a*dt^2 + v*dt - gap = 0
    a, b, c = 0.5 * accel_per_s2, ror_per_s, -gap
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    roots = [(-b + s * math.sqrt(disc)) / (2 * a) for s in (1.0, -1.0)]
    forward = [r for r in roots if r > 0]
    if not forward:
        return None
    return now_t + min(forward)


def evaluate_fc_eta(roasts: list[Roast], fc_band: tuple[float, float]) -> EtaResult:
    """Score the FC-ETA feature across all roasts.

    At every pre-FC tick, computes the low-lag bean RoR and its acceleration,
    projects to the FC-band midpoint (linear for the first 5 min after charge,
    quadratic thereafter, per Artisan), and records the ETA error against the
    actual FC event. Errors are bucketed by how far before FC the tick is.

    Args:
        roasts: The loaded roasts.
        fc_band: The FC target band (low, high) °C.

    Returns:
        The aggregated :class:`EtaResult`.
    """
    target = 0.5 * (fc_band[0] + fc_band[1])
    bucket_abs: dict[float, list[float]] = {b: [] for b in ETA_LEAD_BUCKETS_SECONDS}
    bucket_signed: dict[float, list[float]] = {b: [] for b in ETA_LEAD_BUCKETS_SECONDS}
    first_useful_leads: list[float] = []

    for r in roasts:
        ror = ror_series(r.t, r.bean, ROR_SPAN_SECONDS)
        accel = ror_series(r.t, np.nan_to_num(ror, nan=0.0), CURVATURE_SPAN_SECONDS)
        # Track the earliest contiguous-to-FC lead time the ETA stays useful.
        useful_streak_start: float | None = None
        for i in range(r.t.size):
            now_t = float(r.t[i])
            if now_t >= r.fc_seconds or now_t < r.charge_seconds:
                continue
            if not math.isfinite(ror[i]):
                continue
            since_charge = now_t - r.charge_seconds
            use_quad = since_charge >= 300.0
            eta = eta_at_tick(
                now_t, float(r.bean[i]), float(ror[i]), float(accel[i]), target, use_quad
            )
            lead = r.fc_seconds - now_t
            if eta is None:
                useful_streak_start = None
                continue
            err = eta - r.fc_seconds  # + = predicted late
            for b in ETA_LEAD_BUCKETS_SECONDS:
                if abs(lead - b) <= SAMPLE_SECONDS / 2.0:
                    bucket_abs[b].append(abs(err))
                    bucket_signed[b].append(err)
            if abs(err) <= ETA_USEFUL_TOLERANCE_SECONDS:
                if useful_streak_start is None:
                    useful_streak_start = lead
            else:
                useful_streak_start = None
        if useful_streak_start is not None:
            first_useful_leads.append(useful_streak_start)

    def _key(b: float) -> str:
        return f"{int(b)}s"

    return EtaResult(
        fc_band_c=fc_band,
        fc_target_c=round(target, 1),
        per_lead_abs_median_s={
            _key(b): round(percentile(bucket_abs[b], 50.0), 1) for b in ETA_LEAD_BUCKETS_SECONDS
        },
        per_lead_abs_p90_s={
            _key(b): round(percentile(bucket_abs[b], 90.0), 1) for b in ETA_LEAD_BUCKETS_SECONDS
        },
        per_lead_signed_median_s={
            _key(b): round(percentile(bucket_signed[b], 50.0), 1) for b in ETA_LEAD_BUCKETS_SECONDS
        },
        per_lead_n_ticks={_key(b): len(bucket_abs[b]) for b in ETA_LEAD_BUCKETS_SECONDS},
        first_useful_lead_median_s=round(percentile(first_useful_leads, 50.0), 1),
        useful_tolerance_s=ETA_USEFUL_TOLERANCE_SECONDS,
    )


# --- Feature 2: RoR curvature (crash / flick) -------------------------------


@dataclass
class CurvatureResult:
    """Crash / flick detectability around FC.

    Attributes:
        n_roasts: Roast count.
        crash_detected_count: Roasts with a clear RoR-crash (negative curvature
            dip) in the FC +/- window.
        flick_detected_count: Roasts with a clear RoR-flick (RoR rebound) in the
            early-development window.
        median_min_curvature_fc: Median of each roast's most-negative curvature
            (°C/min/min) in the FC window.
        median_post_fc_ror_rebound: Median RoR rebound (°C/min) from the FC-window
            RoR trough to the early-development peak.
        curvature_noise_floor_1s: Curvature noise std on the 1 s view in a stable
            pre-FC window (the detectability reference).
        curvature_noise_floor_5s: Same on the 5 s view.
        signal_to_noise_5s: Median |FC-window curvature| divided by the 5 s noise
            floor.
    """

    n_roasts: int
    crash_detected_count: int
    flick_detected_count: int
    median_min_curvature_fc: float
    median_post_fc_ror_rebound: float
    curvature_noise_floor_1s: float
    curvature_noise_floor_5s: float
    signal_to_noise_5s: float


def _window_mask(
    t: NDArray[np.float64], centre: float, before: float, after: float
) -> NDArray[np.bool_]:
    """Boolean mask for samples in ``[centre - before, centre + after]``."""
    return (t >= centre - before) & (t <= centre + after)


def evaluate_curvature(roasts_5s: list[Roast], roasts_1s: list[Roast]) -> CurvatureResult:
    """Score crash / flick detectability from RoR curvature.

    For each roast: compute bean RoR and its derivative (curvature). The crash
    signature is a strong negative curvature dip around FC; the flick is an RoR
    rebound in early development (FC to FC+150 s). Detection thresholds are set
    relative to each roast's own pre-FC noise so they are scale-free. The noise
    floor is measured on both the 1 s and 5 s views to answer "is it detectable
    from 5 s telemetry".

    Args:
        roasts_5s: Roasts decimated to 5 s (the live-tick view).
        roasts_1s: The same roasts at 1 s (the noise-floor reference).

    Returns:
        The aggregated :class:`CurvatureResult`.
    """
    crash = 0
    flick = 0
    min_curv: list[float] = []
    rebounds: list[float] = []
    snr: list[float] = []
    noise_1s: list[float] = []
    noise_5s: list[float] = []

    by_name_1s = {r.name: r for r in roasts_1s}
    for r in roasts_5s:
        ror = ror_series(r.t, r.bean, ROR_SPAN_SECONDS)
        curv = ror_series(r.t, np.nan_to_num(ror, nan=0.0), CURVATURE_SPAN_SECONDS)

        # Noise floor: curvature std over a stable maillard window
        # (charge+120 s .. FC-120 s), where the curve is near-linear.
        stable = _stable_window_curvature(r, curv)
        if math.isfinite(stable):
            noise_5s.append(stable)
        r1 = by_name_1s.get(r.name)
        if r1 is not None:
            ror1 = ror_series(r1.t, r1.bean, ROR_SPAN_SECONDS)
            curv1 = ror_series(r1.t, np.nan_to_num(ror1, nan=0.0), CURVATURE_SPAN_SECONDS)
            stable1 = _stable_window_curvature(r1, curv1)
            if math.isfinite(stable1):
                noise_1s.append(stable1)

        # Crash: most-negative curvature in FC +/- 60 s.
        fc_mask = _window_mask(r.t, r.fc_seconds, 60.0, 60.0) & np.isfinite(curv)
        if int(np.count_nonzero(fc_mask)) >= 2:
            dip = float(np.min(curv[fc_mask]))
            min_curv.append(dip)
            if math.isfinite(stable) and stable > 0 and dip < -3.0 * stable:
                crash += 1
            if math.isfinite(stable) and stable > 0:
                snr.append(abs(dip) / stable)

        # Flick: RoR rebound from the FC-window trough to the early-dev peak.
        trough_mask = _window_mask(r.t, r.fc_seconds, 30.0, 60.0) & np.isfinite(ror)
        peak_mask = _window_mask(r.t, r.fc_seconds + 105.0, 45.0, 45.0) & np.isfinite(ror)
        if int(np.count_nonzero(trough_mask)) >= 1 and int(np.count_nonzero(peak_mask)) >= 1:
            trough = float(np.min(ror[trough_mask]))
            peak = float(np.max(ror[peak_mask]))
            rebound = peak - trough
            rebounds.append(rebound)
            if rebound > 2.0:
                flick += 1

    return CurvatureResult(
        n_roasts=len(roasts_5s),
        crash_detected_count=crash,
        flick_detected_count=flick,
        median_min_curvature_fc=round(percentile(min_curv, 50.0), 2),
        median_post_fc_ror_rebound=round(percentile(rebounds, 50.0), 2),
        curvature_noise_floor_1s=round(percentile(noise_1s, 50.0), 2),
        curvature_noise_floor_5s=round(percentile(noise_5s, 50.0), 2),
        signal_to_noise_5s=round(percentile(snr, 50.0), 2),
    )


def _stable_window_curvature(r: Roast, curv: NDArray[np.float64]) -> float:
    """Std of curvature over the near-linear maillard window (the noise floor)."""
    mask = (r.t >= r.charge_seconds + 120.0) & (r.t <= r.fc_seconds - 120.0) & np.isfinite(curv)
    if int(np.count_nonzero(mask)) < 4:
        return math.nan
    return float(np.std(curv[mask]))


# --- Feature 3: turning point + recovery ------------------------------------


@dataclass
class TpRoast:
    """Per-roast turning-point and recovery metrics + downstream outcomes.

    Attributes:
        name: Fixture name.
        tp_temp_c: Bean temperature at the post-charge minimum.
        time_to_tp_s: Charge-to-TP duration.
        recovery_slope_c_per_min: Bean RoR over the 60 s after TP.
        time_to_fc_s: Downstream: charge-to-FC.
        total_roast_s: Downstream: charge-to-drop.
        drop_temp_c: Downstream: bean temp at drop.
    """

    name: str
    tp_temp_c: float
    time_to_tp_s: float
    recovery_slope_c_per_min: float
    time_to_fc_s: float
    total_roast_s: float
    drop_temp_c: float


@dataclass
class Correlation:
    """One predictor-vs-outcome correlation.

    Attributes:
        predictor: Predictor name.
        outcome: Outcome name.
        pearson_r: Pearson r.
        spearman_r: Spearman rho.
        approx_p: Indicative two-sided p-value (Pearson).
        n: Sample size.
    """

    predictor: str
    outcome: str
    pearson_r: float
    spearman_r: float
    approx_p: float
    n: int


@dataclass
class TpResult:
    """Turning-point / recovery correlation results.

    Attributes:
        per_roast: The per-roast metrics.
        correlations: Every predictor-vs-outcome correlation.
        tp_temp_range_c: (min, max) TP temperature across the set.
        time_to_tp_range_s: (min, max) time-to-TP across the set.
        recovery_slope_range: (min, max) recovery slope across the set.
    """

    per_roast: list[TpRoast]
    correlations: list[Correlation]
    tp_temp_range_c: tuple[float, float]
    time_to_tp_range_s: tuple[float, float]
    recovery_slope_range: tuple[float, float]


def turning_point(r: Roast) -> tuple[float, float]:
    """Post-charge bean-temp minimum: ``(tp_temp_c, time_to_tp_s)``.

    Searches the window from charge to charge+180 s (the turning point always
    falls inside the first few minutes after the cold beans hit the drum).
    """
    mask = (r.t >= r.charge_seconds) & (r.t <= r.charge_seconds + 180.0)
    bean = r.bean[mask]
    times = r.t[mask]
    j = int(np.argmin(bean))
    return float(bean[j]), float(times[j] - r.charge_seconds)


def _recovery_slope(r: Roast, tp_time_abs: float) -> float:
    """Bean RoR (°C/min) over the 60 s immediately after the turning point."""
    mask = (r.t >= tp_time_abs) & (r.t <= tp_time_abs + 60.0)
    if int(np.count_nonzero(mask)) < 2:
        return math.nan
    return slope_per_min(r.t[mask], r.bean[mask])


def evaluate_turning_point(roasts: list[Roast]) -> TpResult:
    """Score whether TP / recovery metrics predict downstream outcomes.

    Computes TP temperature, time-to-TP, and the post-TP recovery slope per
    roast, then correlates each (Pearson + Spearman) against FC timing, total
    roast time, and drop temperature.

    Args:
        roasts: The loaded roasts.

    Returns:
        The aggregated :class:`TpResult`.
    """
    per_roast: list[TpRoast] = []
    for r in roasts:
        tp_temp, time_to_tp = turning_point(r)
        slope = _recovery_slope(r, r.charge_seconds + time_to_tp)
        # Store unrounded values so the correlation arrays use full precision.
        # Rounding happens at the serialisation / display step (Correlation fields
        # and _rng already round; TpRoast fields in JSON output are handled by
        # _sanitise_for_json in main()).
        per_roast.append(
            TpRoast(
                name=r.name,
                tp_temp_c=tp_temp,
                time_to_tp_s=time_to_tp,
                recovery_slope_c_per_min=slope,
                time_to_fc_s=r.time_to_fc_seconds,
                total_roast_s=r.total_roast_seconds,
                drop_temp_c=r.drop_bean_c,
            )
        )

    # Build correlation arrays from unrounded per-roast values.
    predictors = {
        "tp_temp_c": np.array([p.tp_temp_c for p in per_roast], dtype=np.float64),
        "time_to_tp_s": np.array([p.time_to_tp_s for p in per_roast], dtype=np.float64),
        "recovery_slope": np.array(
            [p.recovery_slope_c_per_min for p in per_roast], dtype=np.float64
        ),
    }
    outcomes = {
        "time_to_fc_s": np.array([p.time_to_fc_s for p in per_roast], dtype=np.float64),
        "total_roast_s": np.array([p.total_roast_s for p in per_roast], dtype=np.float64),
        "drop_temp_c": np.array([p.drop_temp_c for p in per_roast], dtype=np.float64),
    }
    correlations: list[Correlation] = []
    for pname, pv in predictors.items():
        for oname, ov in outcomes.items():
            finite = np.isfinite(pv) & np.isfinite(ov)
            x, y = pv[finite], ov[finite]
            pr = pearson(x, y)
            correlations.append(
                Correlation(
                    predictor=pname,
                    outcome=oname,
                    pearson_r=round(pr, 3),
                    spearman_r=round(spearman(x, y), 3),
                    approx_p=round(approx_two_sided_p(pr, int(x.size)), 4),
                    n=int(x.size),
                )
            )

    def _rng(key: str) -> tuple[float, float]:
        vals = predictors[key]
        finite = vals[np.isfinite(vals)]
        return round(float(np.min(finite)), 2), round(float(np.max(finite)), 2)

    return TpResult(
        per_roast=per_roast,
        correlations=correlations,
        tp_temp_range_c=_rng("tp_temp_c"),
        time_to_tp_range_s=_rng("time_to_tp_s"),
        recovery_slope_range=_rng("recovery_slope"),
    )


# --- Top-level report -------------------------------------------------------


@dataclass
class Report:
    """The full curve-feature evaluation.

    Attributes:
        n_roasts: Roast count.
        fc_band_c: The derived FC band.
        fc_eta: FC-ETA results.
        curvature: RoR-curvature results.
        turning_point: TP / recovery results.
    """

    n_roasts: int
    fc_band_c: tuple[float, float]
    fc_eta: EtaResult
    curvature: CurvatureResult
    turning_point: TpResult


def build_report(fixtures_dir: Path, fc_band: tuple[float, float] | None = None) -> Report:
    """Run all three feature evaluations and assemble the report.

    Args:
        fixtures_dir: Directory containing the ``artisan-*`` fixtures.
        fc_band: An explicit FC target band (low, high) °C to use for the ETA.
            When ``None`` the band is derived empirically from the set's own
            FC-event bean temps, falling back to :data:`DEFAULT_FC_BAND_C` if
            the derivation is degenerate.

    Returns:
        The assembled :class:`Report`.
    """
    roasts_5s = load_all(fixtures_dir, SAMPLE_SECONDS)
    roasts_1s = load_all(fixtures_dir, 1.0)
    if fc_band is None:
        fc_band = derive_fc_band(roasts_5s)
        if not (math.isfinite(fc_band[0]) and fc_band[1] > fc_band[0]):
            fc_band = DEFAULT_FC_BAND_C
    return Report(
        n_roasts=len(roasts_5s),
        fc_band_c=fc_band,
        fc_eta=evaluate_fc_eta(roasts_5s, fc_band),
        curvature=evaluate_curvature(roasts_5s, roasts_1s),
        turning_point=evaluate_turning_point(roasts_5s),
    )


def _print_report(report: Report) -> None:
    """Print a human-readable summary of the report to stdout."""
    print(f"Curve-feature validation on N={report.n_roasts} recorded roasts")
    print(f"Derived FC band: {report.fc_band_c[0]:.0f}-{report.fc_band_c[1]:.0f} C")
    print()
    e = report.fc_eta
    print(f"== Feature 1: FC-ETA (target {e.fc_target_c} C) ==")
    print(f"  median first-useful lead time: {e.first_useful_lead_median_s:.0f} s before FC")
    print("  |error| by lead-time (median / p90, n ticks):")
    for b in e.per_lead_abs_median_s:
        print(
            f"    lead {b:>5}: median |err| {e.per_lead_abs_median_s[b]:>6.1f}s  "
            f"p90 {e.per_lead_abs_p90_s[b]:>6.1f}s  signed {e.per_lead_signed_median_s[b]:>6.1f}s  "
            f"(n={e.per_lead_n_ticks[b]})"
        )
    print()
    c = report.curvature
    print("== Feature 2: RoR curvature (crash / flick) ==")
    print(f"  crash detected: {c.crash_detected_count}/{c.n_roasts} roasts")
    print(f"  flick detected: {c.flick_detected_count}/{c.n_roasts} roasts")
    print(f"  median min curvature @FC: {c.median_min_curvature_fc} C/min/min")
    print(f"  median post-FC RoR rebound: {c.median_post_fc_ror_rebound} C/min")
    print(
        f"  curvature noise floor 1s={c.curvature_noise_floor_1s} "
        f"5s={c.curvature_noise_floor_5s}  median SNR(5s)={c.signal_to_noise_5s}"
    )
    print()
    tp = report.turning_point
    print("== Feature 3: turning point + recovery ==")
    print(
        f"  TP temp range {tp.tp_temp_range_c} C, time-to-TP {tp.time_to_tp_range_s} s, "
        f"recovery slope {tp.recovery_slope_range} C/min"
    )
    print("  predictor -> outcome   pearson  spearman  ~p   n")
    for cor in tp.correlations:
        print(
            f"    {cor.predictor:<14} -> {cor.outcome:<14} "
            f"{cor.pearson_r:>6.3f}  {cor.spearman_r:>6.3f}  {cor.approx_p:>5.3f}  n={cor.n}"
        )


def _sanitise_for_json(obj: Any) -> Any:
    """Recursively replace non-finite floats with None for valid JSON output.

    ``json.dumps`` raises ``ValueError`` on ``nan`` / ``inf`` / ``-inf`` unless
    a custom encoder or ``allow_nan=True`` is used (the latter produces invalid
    JSON). This helper walks the structure and substitutes ``None`` (JSON null)
    so the output is always spec-compliant.

    Args:
        obj: A dict, list, float, or other JSON-compatible scalar.

    Returns:
        The sanitised object, safe to pass to ``json.dumps``.
    """
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {str(k): _sanitise_for_json(v) for k, v in obj.items()}  # type: ignore[return-value,unknown-variable-type]
    if isinstance(obj, list):
        return [_sanitise_for_json(v) for v in obj]  # type: ignore[return-value]
    return obj


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: run the evaluation and print a report.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code (``0`` on success).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path(".artisan-fixtures"),
        help="Directory containing the artisan-* roast fixtures.",
    )
    parser.add_argument(
        "--fc-band",
        type=float,
        nargs=2,
        metavar=("LOW_C", "HIGH_C"),
        default=None,
        help=(
            "Explicit FC target band in bean °C (default: derive from the set, "
            f"falling back to {DEFAULT_FC_BAND_C} if degenerate)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the human-readable summary.",
    )
    args = parser.parse_args(argv)
    band = (float(args.fc_band[0]), float(args.fc_band[1])) if args.fc_band else None
    report = build_report(args.fixtures_dir, band)
    if args.json:
        print(json.dumps(_sanitise_for_json(asdict(report)), indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
