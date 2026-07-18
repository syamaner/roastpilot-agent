"""Phase-1 plant-model feasibility study: a linear ARX for bean-RoR projection.

Offline, deterministic, no network, no paid APIs. Unifies two corpora recorded
on the same Hottop in the same room -- the operator's Artisan ``.alog`` logs and
the completed store roasts in ``roastpilot.sqlite3`` -- onto a common 1 Hz,
charge-referenced schema, then asks whether a low-order linear ARX predicts bean
RoR at ``t+20`` / ``t+30`` / ``t+40`` s (past the ~25-35 s thermocouple lag) well
enough to justify a predictive controller.

The model is a numpy least-squares ARX; leave-one-roast-out CV and the naive
baselines are fully deterministic (no ``np.random``). The store is opened
**read-only**: the DB is copied to a temp directory and only the copy is read;
the harness verifies the copy's sha256 matches the source and never writes to it.

Raw roast data (``.alog`` files, the SQLite DB, raw per-tick telemetry) is
excluded from the repo by ``AGENTS.md``. This harness regenerates every artifact
from the operator's local data; the committed bundle carries only code, the
aggregate outputs, and a data fingerprint (``data-manifest.md``).

Usage::

    # run the study, write artifacts to an output directory
    python scripts/plant_model_arx_study.py --out-dir /tmp/phase1

    # regenerate the committable data manifest (fingerprint only)
    python scripts/plant_model_arx_study.py \\
        --emit-manifest docs/research/plant-model/data-manifest.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from alog_to_fixture import (  # noqa: E402
    _level_at,  # pyright: ignore[reportPrivateUsage]
    _step_track,  # pyright: ignore[reportPrivateUsage]
    extract_marks,
    load_alog,
)

DEFAULT_ALOG_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/roasting"
DEFAULT_STORE = Path.home() / "roasts/roastpilot.sqlite3"

# Artisan timeindex slots: [CHARGE, DRYe, FCs, FCe, SCs, SCe, DROP, COOL].
_DRYE_SLOT = 1
_TYPE_FAN = 0
_TYPE_HEAT = 3
_NODATA = -1.0

#: Trailing linear-fit window for RoR (causal, control-realistic), seconds.
ROR_WINDOW_S = 30
#: Prediction horizons, seconds.
HORIZONS: tuple[int, ...] = (20, 30, 40)
#: ``|Delta heat %|`` that counts as a step for the counterfactual.
HEAT_STEP_MIN = 10
#: Seconds after a step to score the step-response (dead-time window).
STEP_EVAL_LO, STEP_EVAL_HI = 5, 40
#: RoR autoregressive lag offsets, seconds.
ROR_LAGS: tuple[int, ...] = (0, 5, 10, 15, 20)
#: Heat-input lag offsets spanning the dead-time, seconds.
HEAT_LAGS: tuple[int, ...] = (0, 10, 20, 30, 40)
#: Fan-input lag offsets, seconds.
FAN_LAGS: tuple[int, ...] = (0, 20, 40)

FEATURE_NAMES: list[str] = (
    ["bias"]
    + [f"ror_lag{lag}" for lag in ROR_LAGS]
    + ["bt"]
    + [f"heat_lag{lag}" for lag in HEAT_LAGS]
    + [f"fan_lag{lag}" for lag in FAN_LAGS]
    + ["t_since_heat_chg", "fc_ind", "fc_x_heat"]
)
#: Columns kept for the "no heat/fan" ablation model.
_NOHEAT_COLS: list[int] = [
    i
    for i, name in enumerate(FEATURE_NAMES)
    if not (name.startswith(("heat_lag", "fan_lag")) or name == "fc_x_heat")
]


def _empty_landmarks() -> dict[str, float]:
    """Typed empty-landmarks factory (keeps the dataclass field strictly typed)."""
    return {}


@dataclass
class Roast:
    """One roast unified to the common per-tick schema (charge-referenced, 1 Hz).

    Attributes:
        rid: Roast identifier.
        corpus: ``"artisan"`` or ``"store"``.
        t: Integer seconds from charge (0..drop).
        bt: Bean temperature, deg C, per tick.
        et: Environment temperature, deg C, per tick.
        heat: Heat setpoint percent, per tick.
        fan: Fan setpoint percent, per tick.
        ror: Trailing-window bean RoR, deg C/min, per tick (NaN early).
        fc_t: First-crack time in seconds from charge (or NaN).
        drop_t: Drop time in seconds from charge.
        landmarks: Characteristic BT landmarks for calibration.
    """

    rid: str
    corpus: str
    t: NDArray[np.float64]
    bt: NDArray[np.float64]
    et: NDArray[np.float64]
    heat: NDArray[np.float64]
    fan: NDArray[np.float64]
    ror: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    fc_t: float = float("nan")
    drop_t: float = float("nan")
    landmarks: dict[str, float] = field(default_factory=_empty_landmarks)


def trailing_ror(
    t: NDArray[np.float64], temp: NDArray[np.float64], window_s: int = ROR_WINDOW_S
) -> NDArray[np.float64]:
    """Trailing linear-fit slope of ``temp`` over ``window_s``, in deg C/min.

    Causal: at tick ``i`` uses only samples in ``(t[i]-window_s, t[i]]``. Returns
    NaN until at least 15 s of history is available.

    Args:
        t: Integer second timestamps (monotonic, 1 Hz).
        temp: Temperature series parallel to ``t``.
        window_s: Trailing window length in seconds.

    Returns:
        RoR array parallel to ``t`` (deg C/min).
    """
    n = len(t)
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(n):
        lo = t[i] - window_s
        mask = (t <= t[i]) & (t > lo)
        if int(np.count_nonzero(mask)) < 15:
            continue
        tt = t[mask]
        yy = temp[mask]
        good = np.isfinite(yy)
        if int(np.count_nonzero(good)) < 15:
            continue
        tg = tt[good]
        coeffs = np.polyfit(tg - float(tg.mean()), yy[good], 1)
        out[i] = float(coeffs[0]) * 60.0
    return out


def _resample_1hz(
    times: NDArray[np.float64], values: NDArray[np.float64], t_grid: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Interpolate ``values`` sampled at ``times`` onto integer ``t_grid``."""
    good = np.isfinite(values) & (values > _NODATA)
    if int(np.count_nonzero(good)) < 2:
        return np.full(len(t_grid), np.nan, dtype=np.float64)
    return np.interp(t_grid, times[good], values[good])


def _carry_forward(
    t_grid: NDArray[np.float64], secs: NDArray[np.float64], vals: list[float]
) -> NDArray[np.float64]:
    """Step carry-forward of a setpoint series onto ``t_grid``."""
    out = np.full(len(t_grid), np.nan, dtype=np.float64)
    idx = np.searchsorted(secs, t_grid, side="right") - 1
    for i in range(len(t_grid)):
        j = int(idx[i])
        if j >= 0:
            out[i] = vals[j]
    return out


def load_artisan(alog_dir: Path) -> list[Roast]:
    """Load and unify all usable Artisan ``.alog`` roasts under ``alog_dir``.

    Args:
        alog_dir: Directory of ``.alog`` files.

    Returns:
        The parsed roasts (those with a marked FC and drop and >= 120 s span).
    """
    roasts: list[Roast] = []
    for path in sorted(alog_dir.glob("*.alog")):
        try:
            profile = load_alog(path)
            marks = extract_marks(profile)
        except (ValueError, SyntaxError):
            continue
        timex = np.array([float(v) for v in profile.get("timex", [])], dtype=np.float64)
        et_raw = np.array([float(v) for v in profile.get("temp1", [])], dtype=np.float64)
        bt_raw = np.array([float(v) for v in profile.get("temp2", [])], dtype=np.float64)
        events = [int(v) for v in profile.get("specialevents", [])]
        types = [int(v) for v in profile.get("specialeventstype", [])]
        values = [float(v) for v in profile.get("specialeventsvalue", [])]
        timeindex = [int(v) for v in profile.get("timeindex", [])]
        heat_track = _step_track(list(timex), events, types, values, _TYPE_HEAT)
        fan_track = _step_track(list(timex), events, types, values, _TYPE_FAN)

        charge_s = marks.charge_seconds
        drop_rel = int(round(marks.drop_seconds - charge_s))
        if drop_rel < 120:
            continue
        t_grid = np.arange(0, drop_rel + 1, dtype=np.float64)
        abs_times = timex - charge_s
        bt = _resample_1hz(abs_times, bt_raw, t_grid)
        et = _resample_1hz(abs_times, et_raw, t_grid)
        heat = np.array(
            [float(_level_at(heat_track, charge_s + float(t))) for t in t_grid], dtype=np.float64
        )
        fan = np.array(
            [float(_level_at(fan_track, charge_s + float(t))) for t in t_grid], dtype=np.float64
        )

        drye_bt = float("nan")
        if len(timeindex) > _DRYE_SLOT and timeindex[_DRYE_SLOT] > 0:
            di = timeindex[_DRYE_SLOT]
            if di < len(bt_raw):
                drye_bt = float(bt_raw[di])
        turn_bt = float(np.nanmin(bt[: min(len(bt), 180)])) if len(bt) else float("nan")

        r = Roast(
            rid=f"artisan:{path.stem}",
            corpus="artisan",
            t=t_grid,
            bt=bt,
            et=et,
            heat=heat,
            fan=fan,
            fc_t=marks.first_crack_seconds - charge_s,
            drop_t=float(drop_rel),
            landmarks={
                "dry_end_bt": drye_bt,
                "fc_bt": marks.first_crack_temp_c,
                "drop_bt": marks.drop_temp_c,
                "turnaround_bt": turn_bt,
            },
        )
        r.ror = trailing_ror(t_grid, bt)
        roasts.append(r)
    return roasts


def completed_store_run_ids(store_copy: Path) -> list[str]:
    """Return the completed store run ids, ordered, from a read-only copy."""
    con = sqlite3.connect(f"file:{store_copy}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id from roast_runs where outcome='completed' order by started_at_utc"
        ).fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in rows]


def load_store(store_copy: Path) -> list[Roast]:
    """Load and unify the completed store roasts from a read-only DB copy.

    Args:
        store_copy: Path to a read-only copy of the store SQLite DB.

    Returns:
        The parsed completed roasts with usable telemetry (>= 60 raw rows).
    """
    con = sqlite3.connect(f"file:{store_copy}?mode=ro", uri=True)
    roasts: list[Roast] = []
    try:
        run_ids = [
            str(r[0])
            for r in con.execute(
                "select id from roast_runs where outcome='completed' order by started_at_utc"
            )
        ]
        for rid in run_ids:
            rows = con.execute(
                "select charge_elapsed_seconds, bean_temp_c, env_temp_c, "
                "heat_level_percent, fan_level_percent, agent_phase "
                "from telemetry_snapshots where run_id=? "
                "and charge_elapsed_seconds is not null and bean_temp_c is not null "
                "and agent_phase in ('roasting_pre_first_crack','development') "
                "order by charge_elapsed_seconds",
                (rid,),
            ).fetchall()
            if len(rows) < 60:
                continue
            # Dedupe to integer seconds (last sample wins).
            by_sec: dict[int, tuple[float, float, float, float, str]] = {}
            for ce, bt, et, heat, fan, phase in rows:
                s = int(round(float(ce)))
                if s < 0:
                    continue
                by_sec[s] = (
                    float(bt),
                    float(et) if et is not None else float("nan"),
                    float(heat) if heat is not None else float("nan"),
                    float(fan) if fan is not None else float("nan"),
                    str(phase),
                )
            secs = np.array(sorted(by_sec), dtype=np.float64)
            grid = np.arange(float(secs.min()), float(secs.max()) + 1, dtype=np.float64)
            bt = np.interp(
                grid, secs, np.array([by_sec[int(s)][0] for s in secs], dtype=np.float64)
            )
            et = np.interp(
                grid, secs, np.array([by_sec[int(s)][1] for s in secs], dtype=np.float64)
            )
            heat = _carry_forward(grid, secs, [by_sec[int(s)][2] for s in secs])
            fan = _carry_forward(grid, secs, [by_sec[int(s)][3] for s in secs])
            base = float(grid.min())
            t_grid = grid - base

            fc_t = float("nan")
            for s in secs:
                if by_sec[int(s)][4] == "development":
                    fc_t = float(s) - base
                    break
            fc_ev = con.execute(
                "select payload_json from roast_events where run_id=? and kind='first_crack' "
                "order by id limit 1",
                (rid,),
            ).fetchone()
            drop_bt = float(bt[-1])
            fc_bt = float("nan")
            if fc_ev is not None:
                fc_bt = float(json.loads(str(fc_ev[0])).get("bean_temp_c", float("nan")))
            de_ev = con.execute(
                "select payload_json from roast_events where run_id=? and kind='drying_end' "
                "order by id limit 1",
                (rid,),
            ).fetchone()
            de_bt = float("nan")
            if de_ev is not None:
                de_bt = float(json.loads(str(de_ev[0])).get("bean_temp_c", float("nan")))
            turn_bt = float(np.nanmin(bt[: min(len(bt), 180)]))

            r = Roast(
                rid=f"store:{rid[:8]}",
                corpus="store",
                t=t_grid,
                bt=bt,
                et=et,
                heat=heat,
                fan=fan,
                fc_t=fc_t,
                drop_t=float(t_grid[-1]),
                landmarks={
                    "dry_end_bt": de_bt,
                    "fc_bt": fc_bt,
                    "drop_bt": drop_bt,
                    "turnaround_bt": turn_bt,
                },
            )
            r.ror = trailing_ror(t_grid, bt)
            roasts.append(r)
    finally:
        con.close()
    return roasts


def calibration_report(artisan: list[Roast], store: list[Roast]) -> dict[str, Any]:
    """Compare characteristic BT-landmark distributions between the corpora.

    Args:
        artisan: Artisan roasts.
        store: Store roasts.

    Returns:
        Per-landmark mean/std/n for each corpus and the store-minus-artisan offset.
    """
    out: dict[str, Any] = {}
    for key in ("turnaround_bt", "dry_end_bt", "fc_bt", "drop_bt"):
        a_all = np.array([r.landmarks[key] for r in artisan], dtype=np.float64)
        s_all = np.array([r.landmarks[key] for r in store], dtype=np.float64)
        a = a_all[np.isfinite(a_all)]
        s = s_all[np.isfinite(s_all)]
        out[key] = {
            "artisan_mean": float(a.mean()) if len(a) else None,
            "artisan_std": float(a.std(ddof=1)) if len(a) > 1 else None,
            "artisan_n": int(len(a)),
            "store_mean": float(s.mean()) if len(s) else None,
            "store_std": float(s.std(ddof=1)) if len(s) > 1 else None,
            "store_n": int(len(s)),
            "offset_store_minus_artisan": (
                float(s.mean() - a.mean()) if len(a) and len(s) else None
            ),
        }
    return out


def build_features(
    r: Roast,
) -> tuple[NDArray[np.float64], dict[int, NDArray[np.float64]], NDArray[np.float64]]:
    """Build the ARX design matrix, per-horizon targets, and base RoR for a roast.

    Features at tick ``i``: RoR autoregressive lags, BT level, a heat window
    spanning the dead-time, a fan window, time-since-last-heat-change, an FC
    indicator, and an FC-by-heat interaction. Rows with any NaN, or without all
    horizon targets present, are dropped.

    Args:
        r: The unified roast.

    Returns:
        ``(X, targets, base_ror)`` where ``X`` is ``(n_ticks, n_features)``,
        ``targets[h]`` is the RoR at ``t+h`` for each row, and ``base_ror`` is
        RoR[t] (the persistence baseline).
    """
    t = r.t
    ror = r.ror
    n = len(t)

    tslhc = np.zeros(n, dtype=np.float64)
    last = 0
    for i in range(1, n):
        if r.heat[i] != r.heat[i - 1]:
            last = i
        tslhc[i] = min(float(t[i] - t[last]), 120.0)

    tmap = {int(v): k for k, v in enumerate(t)}
    rows: list[NDArray[np.float64]] = []
    tgt: dict[int, list[float]] = {h: [] for h in HORIZONS}
    base: list[float] = []
    idx: list[int] = []
    for i in range(n):
        ti = int(t[i])
        if any((ti - lag) not in tmap for lag in (20, 40)):
            continue
        feats: list[float] = [1.0]
        feats += [float(ror[tmap[ti - lag]]) for lag in ROR_LAGS]
        feats.append(float(r.bt[i]))
        feats += [float(r.heat[tmap[ti - lag]]) for lag in HEAT_LAGS]
        feats += [float(r.fan[tmap[ti - lag]]) for lag in FAN_LAGS]
        feats.append(float(tslhc[i]))
        fc_ind = 1.0 if (np.isfinite(r.fc_t) and float(t[i]) >= r.fc_t) else 0.0
        feats.append(fc_ind)
        feats.append(fc_ind * float(r.heat[i]))
        fvec = np.array(feats, dtype=np.float64)
        if not bool(np.all(np.isfinite(fvec))) or not np.isfinite(float(ror[i])):
            continue
        futures = {
            h: (float(ror[tmap[ti + h]]) if (ti + h) in tmap else float("nan")) for h in HORIZONS
        }
        if not all(np.isfinite(futures[h]) for h in HORIZONS):
            continue
        rows.append(fvec)
        for h in HORIZONS:
            tgt[h].append(futures[h])
        base.append(float(ror[i]))
        idx.append(i)
    if not rows:
        empty = np.empty((0, len(FEATURE_NAMES)), dtype=np.float64)
        return (
            empty,
            {h: np.empty(0, dtype=np.float64) for h in HORIZONS},
            np.empty(0, dtype=np.float64),
        )
    x = np.array(rows, dtype=np.float64)
    targets = {h: np.array(tgt[h], dtype=np.float64) for h in HORIZONS}
    return x, targets, np.array(base, dtype=np.float64)


def _tick_indices(r: Roast) -> list[int]:
    """Reproduce the row->tick-index mapping used by :func:`build_features`."""
    t = r.t
    ror = r.ror
    n = len(t)
    tmap = {int(v): k for k, v in enumerate(t)}
    idx: list[int] = []
    for i in range(n):
        ti = int(t[i])
        if any((ti - lag) not in tmap for lag in (20, 40)):
            continue
        ok = bool(np.isfinite(float(ror[i])))
        for lag in ROR_LAGS:
            ok = ok and bool(np.isfinite(float(ror[tmap[ti - lag]])))
        ok = ok and bool(np.isfinite(float(r.bt[i])))
        for lag in HEAT_LAGS:
            ok = ok and bool(np.isfinite(float(r.heat[tmap[ti - lag]])))
        for lag in FAN_LAGS:
            ok = ok and bool(np.isfinite(float(r.fan[tmap[ti - lag]])))
        if not ok:
            continue
        if not all((ti + h) in tmap and np.isfinite(float(ror[tmap[ti + h]])) for h in HORIZONS):
            continue
        idx.append(i)
    return idx


def _rmse(errs: list[float]) -> float:
    """Root-mean-square of an error list (NaN on empty)."""
    if not errs:
        return float("nan")
    arr = np.array(errs, dtype=np.float64)
    return float(np.sqrt(np.mean(arr**2)))


def _lstsq(x: NDArray[np.float64], y: NDArray[np.float64]) -> NDArray[np.float64]:
    """Deterministic least-squares solve, returning the coefficient vector."""
    beta, _res, _rank, _sv = np.linalg.lstsq(x, y, rcond=None)
    return np.asarray(beta, dtype=np.float64)


def loro_cv(roasts: list[Roast]) -> dict[str, Any]:
    """Leave-one-roast-out CV: full ARX, no-heat ARX, and naive baselines.

    The no-heat ablation (drop every heat/fan column) is the honest control test:
    if the full model does not beat it, the ARX is only autoregressive
    trend-following and knows nothing about the heat->RoR response.

    Args:
        roasts: The pooled roasts.

    Returns:
        Overall and mid/late-roast (BT>=150) RMSE per horizon for each model, plus
        per-tick full/no-heat predictions for the heat-step counterfactual.
    """
    built = {r.rid: build_features(r) for r in roasts}
    idx_map = {r.rid: _tick_indices(r) for r in roasts}
    models = ("arx", "arx_noheat", "persist", "extrap")
    resid: dict[str, dict[int, list[float]]] = {m: {h: [] for h in HORIZONS} for m in models}
    resid_mid: dict[str, dict[int, list[float]]] = {m: {h: [] for h in HORIZONS} for m in models}
    records: list[dict[str, Any]] = []

    c0 = FEATURE_NAMES.index("ror_lag0")
    c20 = FEATURE_NAMES.index("ror_lag20")
    cbt = FEATURE_NAMES.index("bt")

    for held in roasts:
        x_te, y_te, base_te = built[held.rid]
        if len(x_te) == 0:
            continue
        x_tr_list: list[NDArray[np.float64]] = []
        y_tr_list: dict[int, list[NDArray[np.float64]]] = {h: [] for h in HORIZONS}
        for r in roasts:
            if r.rid == held.rid:
                continue
            x_tr, y_tr, _ = built[r.rid]
            if len(x_tr) == 0:
                continue
            x_tr_list.append(x_tr)
            for h in HORIZONS:
                y_tr_list[h].append(y_tr[h])
        x_tr = np.concatenate(x_tr_list, axis=0)
        mid = x_te[:, cbt] >= 150.0
        ticks = idx_map[held.rid]

        betas: dict[int, NDArray[np.float64]] = {}
        betas_nh: dict[int, NDArray[np.float64]] = {}
        for h in HORIZONS:
            y_tr_h = np.concatenate(y_tr_list[h])
            beta = _lstsq(x_tr, y_tr_h)
            beta_nh = _lstsq(x_tr[:, _NOHEAT_COLS], y_tr_h)
            betas[h] = beta
            betas_nh[h] = beta_nh
            slope = (x_te[:, c0] - x_te[:, c20]) / 20.0
            pred: dict[str, NDArray[np.float64]] = {
                "arx": np.asarray(x_te @ beta, dtype=np.float64),
                "arx_noheat": np.asarray(x_te[:, _NOHEAT_COLS] @ beta_nh, dtype=np.float64),
                "persist": base_te,
                "extrap": x_te[:, c0] + slope * float(h),
            }
            for m in models:
                err = pred[m] - y_te[h]
                resid[m][h].extend([float(e) for e in err])
                resid_mid[m][h].extend([float(e) for e in err[mid]])

        for row in range(len(x_te)):
            rec: dict[str, Any] = {
                "rid": held.rid,
                "tick": int(held.t[ticks[row]]),
                "bt": float(x_te[row, cbt]),
                "base_ror": float(base_te[row]),
            }
            for h in HORIZONS:
                rec[f"arx_{h}"] = float(x_te[row] @ betas[h])
                rec[f"nh_{h}"] = float(x_te[row, _NOHEAT_COLS] @ betas_nh[h])
                rec[f"act_{h}"] = float(y_te[h][row])
            records.append(rec)

    rmse: dict[str, dict[str, float]] = {}
    rmse_mid: dict[str, dict[str, float]] = {}
    for m in models:
        rmse[m] = {}
        rmse_mid[m] = {}
        for h in HORIZONS:
            rmse[m][f"t+{h}"] = _rmse(resid[m][h])
            rmse[m][f"n_t+{h}"] = float(len(resid[m][h]))
            rmse_mid[m][f"t+{h}"] = _rmse(resid_mid[m][h])
            rmse_mid[m][f"n_t+{h}"] = float(len(resid_mid[m][h]))
    return {
        "n_features": len(FEATURE_NAMES),
        "feature_names": FEATURE_NAMES,
        "rmse": rmse,
        "rmse_mid": rmse_mid,
        "preds": {"records": records},
    }


def heat_step_counterfactual(roasts: list[Roast], preds: dict[str, Any]) -> dict[str, Any]:
    """Score full vs no-heat ARX (and persistence) on ticks just after a heat step.

    Args:
        roasts: The pooled roasts.
        preds: Per-tick predictions captured during LORO.

    Returns:
        Step counts per corpus and RMSE-on-step-response for each model.
    """
    steps: dict[str, list[int]] = {}
    step_counts = {"artisan": 0, "store": 0}
    for r in roasts:
        st: list[int] = []
        for i in range(1, len(r.t)):
            if abs(float(r.heat[i] - r.heat[i - 1])) >= HEAT_STEP_MIN:
                st.append(int(r.t[i]))
        steps[r.rid] = st
        step_counts[r.corpus] += len(st)

    err: dict[str, dict[int, list[float]]] = {
        m: {h: [] for h in HORIZONS} for m in ("arx", "arx_noheat", "persist")
    }
    n_scored = 0
    records: list[dict[str, Any]] = preds["records"]
    for rec in records:
        rid = str(rec["rid"])
        tk = int(rec["tick"])
        near = any(STEP_EVAL_LO <= (tk - s) <= STEP_EVAL_HI for s in steps.get(rid, []))
        if not near:
            continue
        n_scored += 1
        for h in HORIZONS:
            err["arx"][h].append(float(rec[f"arx_{h}"]) - float(rec[f"act_{h}"]))
            err["arx_noheat"][h].append(float(rec[f"nh_{h}"]) - float(rec[f"act_{h}"]))
            err["persist"][h].append(float(rec["base_ror"]) - float(rec[f"act_{h}"]))
    out: dict[str, Any] = {"step_counts": step_counts, "n_step_response_ticks": n_scored}
    for model in ("arx", "arx_noheat", "persist"):
        out[model] = {f"t+{h}": _rmse(err[model][h]) for h in HORIZONS}
    return out


def _sha256(path: Path) -> str:
    """Return the hex sha256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_store_readonly(store: Path, tmp: Path) -> Path:
    """Copy the store DB into ``tmp`` and verify the copy matches the source.

    The harness only ever reads the copy; the operator's live DB is never opened
    read-write. Raises if the copy's sha256 diverges from the source.
    """
    src_sha = _sha256(store)
    dst = tmp / "store_copy.sqlite3"
    shutil.copyfile(store, dst)
    if _sha256(dst) != src_sha:
        raise RuntimeError("store copy sha256 mismatch -- read-only copy is corrupt")
    return dst


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------


def _write_landmarks_csv(roasts: list[Roast], out_dir: Path) -> None:
    lines = ["rid,corpus,turnaround_bt,dry_end_bt,fc_bt,drop_bt,fc_t,drop_t"]
    for r in roasts:
        m = r.landmarks
        lines.append(
            f"{r.rid},{r.corpus},{m['turnaround_bt']:.1f},{m['dry_end_bt']:.1f},"
            f"{m['fc_bt']:.1f},{m['drop_bt']:.1f},{r.fc_t:.0f},{r.drop_t:.0f}"
        )
    (out_dir / "landmarks.csv").write_text("\n".join(lines) + "\n")


def _write_rmse_csv(rmse: dict[str, Any], rmse_mid: dict[str, Any], out_dir: Path) -> None:
    lines = ["segment,model,horizon_s,rmse_c_per_min,n"]
    for seg, table in (("all", rmse), ("bt_ge_150", rmse_mid)):
        for model in ("arx", "arx_noheat", "persist", "extrap"):
            for h in HORIZONS:
                lines.append(
                    f"{seg},{model},{h},{table[model][f't+{h}']:.4f},{int(table[model][f'n_t+{h}'])}"
                )
    (out_dir / "loro_rmse.csv").write_text("\n".join(lines) + "\n")


def _write_step_traces_csv(roasts: list[Roast], preds: dict[str, Any], out_dir: Path) -> None:
    lines = ["rid,corpus,tick,base_ror,arx_20,act_20,arx_30,act_30,arx_40,act_40"]
    rmap = {r.rid: r for r in roasts}
    records: list[dict[str, Any]] = preds["records"]
    for rec in records:
        r = rmap[str(rec["rid"])]
        tk = int(rec["tick"])
        near = False
        for i in range(1, len(r.t)):
            if abs(float(r.heat[i] - r.heat[i - 1])) >= HEAT_STEP_MIN and (
                STEP_EVAL_LO <= (tk - int(r.t[i])) <= STEP_EVAL_HI
            ):
                near = True
                break
        if not near:
            continue
        lines.append(
            f"{rec['rid']},{r.corpus},{tk},{float(rec['base_ror']):.2f},"
            f"{float(rec['arx_20']):.2f},{float(rec['act_20']):.2f},"
            f"{float(rec['arx_30']):.2f},{float(rec['act_30']):.2f},"
            f"{float(rec['arx_40']):.2f},{float(rec['act_40']):.2f}"
        )
    (out_dir / "step_response_traces.csv").write_text("\n".join(lines) + "\n")


def _write_report(summary: dict[str, Any], out_dir: Path) -> None:
    c = summary["corpora"]
    cal = summary["calibration"]
    r = summary["loro_rmse"]
    rm = summary["loro_rmse_mid"]
    cf = summary["counterfactual"]

    def row(model: str, table: dict[str, Any] | None = None) -> str:
        tbl = table if table is not None else r
        return " | ".join(f"{tbl[model][f't+{h}']:.2f}" for h in HORIZONS)

    def delta(h: int) -> str:
        best_naive = min(r["persist"][f"t+{h}"], r["extrap"][f"t+{h}"])
        d = best_naive - r["arx"][f"t+{h}"]
        pct = 100 * d / best_naive
        return f"{d:+.2f} ({pct:+.0f}%)"

    def cf_row(model: str) -> str:
        return " | ".join(f"{cf[model][f't+{h}']:.2f}" for h in HORIZONS)

    cal_lines: list[str] = []
    for k in ("turnaround_bt", "dry_end_bt", "fc_bt", "drop_bt"):
        v = cal[k]
        am = "-" if v["artisan_mean"] is None else f"{v['artisan_mean']:.1f}"
        asd = "-" if v["artisan_std"] is None else f"{v['artisan_std']:.1f}"
        smn = "-" if v["store_mean"] is None else f"{v['store_mean']:.1f}"
        ssd = "-" if v["store_std"] is None else f"{v['store_std']:.1f}"
        off_v = v["offset_store_minus_artisan"]
        off = "-" if off_v is None else f"{off_v:+.1f}"
        acol = f"{am} ± {asd} (n={v['artisan_n']})"
        scol = f"{smn} ± {ssd} (n={v['store_n']})"
        cal_lines.append(f"| {k} | {acol} | {scol} | {off} |")

    steps_a = cf["step_counts"]["artisan"]
    steps_s = cf["step_counts"]["store"]
    report = _REPORT_TEMPLATE.format(
        cal_table="\n".join(cal_lines),
        artisan_roasts=c["artisan_roasts"],
        store_roasts=c["store_roasts"],
        pooled_roasts=c["pooled_roasts"],
        modelled_ticks=c["modelled_ticks"],
        pre_fc=c["pre_fc_ticks"],
        post_fc=c["post_fc_ticks"],
        arx_all=row("arx"),
        nh_all=row("arx_noheat"),
        persist_all=row("persist"),
        extrap_all=row("extrap"),
        d20=delta(20),
        d30=delta(30),
        d40=delta(40),
        mid_n=int(rm["arx"]["n_t+20"]),
        arx_mid=row("arx", rm),
        nh_mid=row("arx_noheat", rm),
        persist_mid=row("persist", rm),
        extrap_mid=row("extrap", rm),
        step_lo=STEP_EVAL_LO,
        step_hi=STEP_EVAL_HI,
        step_min=HEAT_STEP_MIN,
        steps_a=steps_a,
        steps_s=steps_s,
        n_step=cf["n_step_response_ticks"],
        cf_arx=cf_row("arx"),
        cf_nh=cf_row("arx_noheat"),
        cf_persist=cf_row("persist"),
    )
    (out_dir / "phase1-arx-report.md").write_text(report)


def run_study(alog_dir: Path, store: Path, out_dir: Path) -> dict[str, Any]:
    """Run the full study, write artifacts to ``out_dir``, and return the summary.

    Args:
        alog_dir: Directory of Artisan ``.alog`` files.
        store: Path to the store SQLite DB (opened read-only via a temp copy).
        out_dir: Directory to write the report and CSV/JSON artifacts.

    Returns:
        The summary dict (also written as ``model_summary.json``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    artisan = load_artisan(alog_dir)
    with tempfile.TemporaryDirectory() as td:
        store_copy = _copy_store_readonly(store, Path(td))
        store_roasts = load_store(store_copy)
    pooled = artisan + store_roasts

    calib = calibration_report(artisan, store_roasts)

    total_ticks = 0
    pre_fc = 0
    post_fc = 0
    for rr in pooled:
        x, _y, _b = build_features(rr)
        total_ticks += len(x)
        for i in range(len(rr.t)):
            if np.isfinite(rr.fc_t):
                if float(rr.t[i]) >= rr.fc_t:
                    post_fc += 1
                else:
                    pre_fc += 1

    cv = loro_cv(pooled)
    counter = heat_step_counterfactual(pooled, cv["preds"])

    _write_landmarks_csv(pooled, out_dir)
    _write_rmse_csv(cv["rmse"], cv["rmse_mid"], out_dir)
    _write_step_traces_csv(pooled, cv["preds"], out_dir)

    summary: dict[str, Any] = {
        "corpora": {
            "artisan_roasts": len(artisan),
            "store_roasts": len(store_roasts),
            "pooled_roasts": len(pooled),
            "modelled_ticks": total_ticks,
            "pre_fc_ticks": pre_fc,
            "post_fc_ticks": post_fc,
        },
        "calibration": calib,
        "loro_rmse": cv["rmse"],
        "loro_rmse_mid": cv["rmse_mid"],
        "counterfactual": counter,
        "feature_names": FEATURE_NAMES,
    }
    (out_dir / "model_summary.json").write_text(json.dumps(summary, indent=2))
    _write_report(summary, out_dir)
    return summary


def emit_manifest(alog_dir: Path, store: Path, dest: Path) -> None:
    """Write the committable data-fingerprint manifest (no raw bytes).

    Records each ``.alog`` filename + sha256 + sample count and the completed
    store run ids, so the exact inputs are pinned without committing roast data.

    Args:
        alog_dir: Directory of Artisan ``.alog`` files.
        store: Path to the store SQLite DB (read-only).
        dest: Path to write the manifest markdown.
    """
    alog_rows: list[tuple[str, str, int]] = []
    for path in sorted(alog_dir.glob("*.alog")):
        try:
            profile = load_alog(path)
            extract_marks(profile)
        except (ValueError, SyntaxError):
            continue
        samples = len(list(profile.get("timex", [])))
        alog_rows.append((path.name, _sha256(path), samples))

    with tempfile.TemporaryDirectory() as td:
        store_copy = _copy_store_readonly(store, Path(td))
        store_sha = _sha256(store)
        run_ids = completed_store_run_ids(store_copy)
        used = load_store(store_copy)
    used_ids = {r.rid.split(":", 1)[1] for r in used}

    lines: list[str] = []
    lines.append("# Phase-1 plant-model study -- data manifest (fingerprint, not data)")
    lines.append("")
    lines.append(
        'This manifest is how the study\'s raw inputs are "committed" under the '
        "`AGENTS.md` no-roast-logs rule: the **fingerprint + provenance** are in the "
        "repo, the **bytes are not**. The raw `.alog` files, the SQLite DB, and raw "
        "per-tick telemetry live only at the documented local paths (and, in future, "
        "the Snowflake `roast_telemetry` table). Regenerate every study artifact with "
        "`scripts/plant_model_arx_study.py` against inputs that match these checksums."
    )
    lines.append("")
    lines.append("## Artisan `.alog` corpus")
    lines.append("")
    lines.append(f"- Source dir (local, not committed): `{DEFAULT_ALOG_DIR}`")
    lines.append(f"- Usable roasts (marked FC + drop, >= 120 s): **{len(alog_rows)}**")
    lines.append("")
    lines.append("| # | filename | sha256 | samples |")
    lines.append("|---|---|---|---|")
    for i, (name, sha, samples) in enumerate(alog_rows, start=1):
        lines.append(f"| {i} | `{name}` | `{sha}` | {samples} |")
    lines.append("")
    lines.append("## Store roasts (`roastpilot.sqlite3`)")
    lines.append("")
    lines.append(f"- Source DB (local, not committed): `{DEFAULT_STORE}`")
    lines.append(f"- DB file sha256 at study time: `{store_sha}`")
    lines.append(
        "  (advisory only -- the DB grows as new roasts are recorded; the run ids "
        "below pin the exact rows.)"
    )
    lines.append(f"- Completed runs in DB: **{len(run_ids)}**")
    lines.append(
        f"- Completed runs actually modelled (>= 60 usable telemetry rows): **{len(used_ids)}**"
    )
    lines.append("")
    lines.append(
        "Completed-run telemetry is immutable once the run's completion trigger has "
        "fired, so the run id pins the data. Runs with fewer than 60 usable "
        "`roasting_pre_first_crack`/`development` telemetry rows are skipped by the "
        "harness."
    )
    lines.append("")
    lines.append("| # | run_id | modelled |")
    lines.append("|---|---|---|")
    for i, rid in enumerate(run_ids, start=1):
        lines.append(f"| {i} | `{rid}` | {'yes' if rid in used_ids else 'no'} |")
    lines.append("")
    lines.append("## Exclusion note")
    lines.append("")
    lines.append(
        "Per `AGENTS.md`, raw roast logs (`.alog`, SQLite DBs, raw per-tick telemetry, "
        "`step_response_traces.csv`) are intentionally **not** in the repo. Only code, "
        "the aggregate outputs (`loro_rmse.csv`, `landmarks.csv`, `model_summary.json`, "
        "the report), and this fingerprint are committed. The raw artifacts are fully "
        "regenerable from the harness against the checksummed inputs above."
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alog-dir", type=Path, default=DEFAULT_ALOG_DIR, help="directory of Artisan .alog files"
    )
    parser.add_argument(
        "--store", type=Path, default=DEFAULT_STORE, help="path to the store SQLite DB (read-only)"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("plant-model-out"),
        help="directory to write the report and CSV/JSON artifacts",
    )
    parser.add_argument(
        "--emit-manifest",
        type=Path,
        default=None,
        help="write the committable data-fingerprint manifest to this path and exit",
    )
    args = parser.parse_args(argv)

    if args.emit_manifest is not None:
        emit_manifest(args.alog_dir, args.store, args.emit_manifest)
        print(f"wrote manifest -> {args.emit_manifest}")
        return 0

    summary = run_study(args.alog_dir, args.store, args.out_dir)
    print(json.dumps(summary["corpora"], indent=2))
    print(f"\nartifacts -> {args.out_dir}")
    return 0


_REPORT_TEMPLATE = """# Phase 1 -- Plant-model feasibility study: linear ARX for bean-RoR projection

Offline, deterministic, no network, no paid APIs. Two corpora (same Hottop, same
room) unified to a common 1 Hz charge-referenced schema. Question: does a
low-order **linear ARX** predict bean RoR at control-relevant horizons
(t+20 / t+30 / t+40 s, past the ~25-35 s thermocouple lag) well enough to
justify building a predictive controller?

Ambient/room temperature is **excluded entirely** (Artisan lacks it) -- a later phase.

---

## 1. Probe-calibration alignment (the load-bearing check)

Do Artisan-era BT readings and current-MCP BT readings live on the same scale?
If not, the corpora cannot be pooled. Compared characteristic BT-landmark
distributions between corpora:

| Landmark | Artisan mean ± sd | Store mean ± sd | Offset (store − artisan) |
|---|---|---|---|
{cal_table}

**Finding: no clean evidence of a probe-calibration offset that would block
pooling.** The apparent landmark offsets are all explained by
detection-method / policy differences, not a probe-scale shift:

- **FC BT ~+6.6 C (store higher).** Store FC is MCP audio detection, which lags
  the true crack ~12-21 s; BT keeps climbing during that lag, so the flagged BT
  reads higher. Artisan FC is operator-marked at the crack. This is detector lag,
  not calibration.
- **Drop BT ~-5.5 C (store lower).** The agent drops beans ~5 C cooler by policy
  (a deliberately conservative bitter-ceiling drop), not because the probe reads
  low.
- **Turnaround ~+21.6 C.** Confounded by charge conditions (batch mass / charge
  temp differ across the multi-year Artisan set) **and** the store sampling
  caveat below -- store telemetry is sparse (~5-6 s) and phase-gated, so the
  interpolated turnaround minimum is shallow. Not a reliable comparator.
- **Dry-end ~150 C in BOTH (offset ~0 C).** This is the one directly comparable
  region. Store fires `drying_end` at a 150 C threshold (pinned by construction),
  but Artisan operators *independently marked* dry-end at ~150 C on average --
  i.e. the two BT scales agree to within ~0.5 C where we can check them.

**Decision: pooled the two corpora directly, with no offset subtraction.** The
model target is RoR (dBT/dt), which is invariant to a constant BT offset anyway;
BT enters the model only as a coarse regime feature, where a small offset is
harmless. Data volume is not the blocker (see the verdict).

## 2. Corpus statistics

- Artisan roasts used: **{artisan_roasts}**
- Store roasts used (completed, usable telemetry): **{store_roasts}**
- Pooled roasts: **{pooled_roasts}**
- Modelled ticks (all features + all-horizon targets present): **{modelled_ticks}**
- Pre-FC ticks: {pre_fc} · Post-FC ticks: {post_fc}

RoR derived as a **trailing 30 s linear-fit slope** of BT (deg C/min), causal at
every tick (uses only samples up to and including t). Applied identically to both
corpora for fairness.

> **Store sampling caveat.** Store telemetry is recorded roughly every 5-6 s
> (~100-130 rows/roast), not at 1 Hz. It is linearly interpolated onto the 1 Hz
> grid, so store RoR is smoother than the underlying reality. Artisan logs are
> genuinely ~1 Hz.

## 3. Multi-horizon RMSE -- ARX vs naive baselines (leave-one-roast-out CV)

Never train and test on the same roast. RMSE in deg C/min. The **no-heat ARX**
(all heat/fan columns dropped) is the honest control ablation: if the full model
does not beat it, the ARX is only autoregressive trend-following.

**All ticks (n={modelled_ticks}, charge->drop):**

| Model | t+20 | t+30 | t+40 |
|---|---|---|---|
| **ARX (linear, full)** | {arx_all} |
| ARX, no heat/fan (ablation) | {nh_all} |
| persistence (RoR[t+h]=RoR[t]) | {persist_all} |
| linear RoR extrapolation | {extrap_all} |
| **ARX gain vs best naive** | {d20} | {d30} | {d40} |

(Positive gain = ARX beats the best naive baseline by that many deg C/min.)

**Mid/late roast only (BT >= 150 C -- where drop/RoR control actually happens,
n={mid_n}):**

| Model | t+20 | t+30 | t+40 |
|---|---|---|---|
| **ARX (linear, full)** | {arx_mid} |
| ARX, no heat/fan (ablation) | {nh_mid} |
| persistence | {persist_mid} |
| linear RoR extrapolation | {extrap_mid} |

## 4. Heat-step counterfactual (the control-relevant test)

Can the model predict the RoR **response to a heat change**? Isolated ticks
within {step_lo}-{step_hi} s **after** a heat setpoint step of
>= {step_min} % and scored there specifically. The decisive contrast is
**full ARX vs no-heat ARX**: only the heat columns can explain the RoR bend a
step induces.

- Heat steps found -- Artisan: **{steps_a}**, Store: **{steps_s}**
- Step-response ticks scored: **{n_step}**

| Model | t+20 | t+30 | t+40 |
|---|---|---|---|
| **ARX full (on step-response)** | {cf_arx} |
| ARX no-heat (on step-response) | {cf_nh} |
| persistence (on step-response) | {cf_persist} |

RMSE in deg C/min. If full ARX does not beat the no-heat ablation **here**, it
has not learned the heat->RoR dynamics -- only smooth coasting, useless for
control.

## 5. Verdict -- NEEDS MORE DATA (conditional no-go on building the controller yet)

A low-order linear ARX is the **right model class** and is **numerically
accurate** at t+20-40 (overall RMSE ~1.5-1.7 C/min, crushing the naive
baselines). But Phase 1 does **not** yet justify building a predictive
controller, for two reasons that the headline table hides:

**(a) The overall ARX-vs-naive win is inflated by the drying phase, not the
control regime.** Persistence looks terrible overall (~8-11 C/min) only because
early-roast RoR falls steeply from turnaround through dry-end -- an easy,
monotonic trend any autoregressive model nails. Restrict to the regime where
drop and RoR decisions actually live (**BT >= 150 C**, section 3, second table)
and persistence collapses to near-parity with the full ARX (the gap is on the
order of ~0.05-0.15 C/min at t+20-40). In the control-relevant window RoR is
slowly varying, so "RoR in 30 s ~= RoR now" is already a strong controller-grade
predictor. The ARX barely improves on it there.

**(b) The heat->RoR signal -- the entire reason to prefer a *predictive*
controller over a reactive one -- is real but small, and the current operating
regime barely excites it.** The no-heat ablation (drop all heat/fan columns) is
almost as good as the full model overall, and on the isolated heat-step-response
ticks the full model beats the no-heat model by only ~0.1-0.3 C/min (the edge
grows with horizon, as expected for a dead-time system). Worse, the **store
corpus (current MCP regime) supplies almost no excitation**: heat is pinned near
65 % through development (the advisor moves fan, not heat), giving only a few
dozen heat steps, mostly clustered at charge. Nearly all identifiable heat
dynamics come from the operator-driven Artisan logs. You cannot robustly
identify a plant gain + dead-time from data that never moves the input.

**What this means:**

- The corpora ARE poolable (section 1): the apparent landmark offsets are
  explained by detection-method and policy differences, not a probe-scale shift,
  and RoR is invariant to a constant BT offset regardless. Data volume is not the
  blocker.
- More *passive* roasts will not fix this -- they add more coasting, not more
  heat-response information. The blocker is **excitation**, not sample count.

**Recommended before any GO:**

1. **Designed excitation.** Run a handful of roasts with deliberate heat steps
   (a staircase or PRBS on the burner, within safe bounds) so the heat->RoR gain
   and dead-time are actually identifiable. This is the single highest-value next
   step.
2. **Prefer a grey-box FOPDT** (first-order-plus-dead-time) fit to those step
   responses over the pooled black-box ARX. It has 3 physically meaningful
   parameters (gain, time-constant, dead-time), extrapolates to unseen inputs far
   better than a regression that never saw input variation, and drops straight
   into a Smith-predictor / IMC controller.
3. **Re-evaluate the ARX against persistence *in the BT >= 150 regime only*** as
   the acceptance gate -- overall RMSE is the wrong yardstick here.

**Bottom line:** promising model class, adequate numerics, but the control-
relevant marginal value over trivial persistence is currently within noise and
the plant's heat channel is under-excited. **NEEDS MORE DATA -- specifically
designed heat-step excitation -- before committing to a predictive controller.**

---

*Artifacts alongside this report:* `model_summary.json` (all numbers),
`landmarks.csv` (per-roast landmarks), `loro_rmse.csv` (the RMSE table).
`step_response_traces.csv` (raw per-tick predicted-vs-actual) is regenerable but
not committed (raw roast data, per `AGENTS.md`).
"""


if __name__ == "__main__":
    raise SystemExit(main())
