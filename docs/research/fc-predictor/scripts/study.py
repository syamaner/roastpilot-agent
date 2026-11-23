"""FC-time predictor study: A (5-sample slope), B (30 s LS slope), C (two-node grey box).

Offline research only. Reads the tidy CSVs written by extract.py / extract_nov07_sse.py.
Usage: python -I study.py <series_dir> <out_dir>
"""

import csv
import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

SER, OUT = sys.argv[1], sys.argv[2]
TARGET = 176.0
with open(os.path.join(SER, "runs.json")) as _fh:
    META = {m["label"]: m for m in json.load(_fh)}
NOV = "2026-11-07_ef6d89f0"


def load(label, fname=None):
    with open(os.path.join(SER, (fname or label) + ".csv")) as fh:
        rows = list(csv.DictReader(fh))
    a = {k: np.array([float(r[k]) for r in rows]) for k in ("t", "bt", "et", "heat", "fan")}
    a["pre"] = np.array([r["phase"] == "roasting_pre_first_crack" for r in rows])
    return a


def tp_index(a):
    """Turning point: BT minimum within the first 180 s after charge.

    The charge probe reads the hot drum first.
    """
    m = np.nonzero(a["t"] <= 180)[0]
    return int(m[np.argmin(a["bt"][m])])


def first_cross(a, temp):
    i = np.nonzero(a["bt"] >= temp)[0]
    i = i[i > tp_index(a)]
    return float(a["t"][i[0]]) if len(i) else None


# ---------------- predictors (each returns predicted absolute time of BT>=target, or None)
def pred_A(t, bt, target, n=5):
    """estimate_first_crack_eta_seconds, verbatim logic, on the samples given."""
    if len(t) < n:
        return None
    span = t[-1] - t[-n]
    if span <= 0:
        return None
    s = (bt[-1] - bt[-n]) / span
    rem = target - bt[-1]
    if s <= 0 or rem <= 0:
        return None
    return t[-1] + rem / s


def ls_line(t, y, win):
    m = t >= t[-1] - win
    if m.sum() < 3:
        return None
    p = np.polyfit(t[m] - t[-1], y[m], 1)
    return p  # slope, value at t[-1]


def pred_B(t, bt, target, win=30.0):
    p = ls_line(t, bt, win)
    if p is None or p[0] <= 0:
        return None
    rem = target - p[1]
    if rem <= 0:
        return None
    return t[-1] + rem / p[0]


# ---------------- model C
# states: B (bean), E (env/drum air), Q (heater output, first-order lag on heat%)
# dB/dt = kb*(E-B) - cb
# dE/dt = g*Q - ka*(E-Ta) - ke*(E-B)
# dQ/dt = (h - Q)/tau
# fan was 30 % on every run, so its effect is folded into ka (not identifiable).
PN = ["kb", "cb", "g", "ka", "ke", "tau"]
P0 = np.array([0.004, 0.0, 1.0, 0.003, 0.005, 60.0])
LO = np.array([1e-4, -0.5, 0.01, 1e-5, 1e-5, 1.0])
HI = np.array([0.1, 0.5, 10.0, 0.1, 0.2, 400.0])


def simulate(p, B0, E0, Q0, heat_fn, Ta, t0, t_end, dt=1.0, stop_bt=None):
    kb, cb, g, ka, ke, tau = p
    B, E, Q, t = B0, E0, Q0, t0
    ts, Bs, Es = [t], [B], [E]
    while t < t_end:
        h = heat_fn(t)
        dB = kb * (E - B) - cb
        dE = g * Q - ka * (E - Ta) - ke * (E - B)
        Q += dt * (h - Q) / tau
        B += dt * dB
        E += dt * dE
        t += dt
        ts.append(t)
        Bs.append(B)
        Es.append(E)
        if stop_bt is not None and stop_bt <= B:
            # linear interpolate crossing
            return t - dt * (B - stop_bt) / max(dB * dt, 1e-9)
    if stop_bt is not None:
        return None
    return np.array(ts), np.array(Bs), np.array(Es)


def heat_zoh(a):
    t, h = a["t"], a["heat"] / 100.0

    def f(x):
        i = np.searchsorted(t, x, side="right") - 1
        return h[max(i, 0)]

    return f


def fit_segment(a):
    """Fit window: pre-FC rows from BT>=120 C to the last pre-FC row."""
    m = a["pre"] & (a["bt"] >= 120) & (np.arange(len(a["t"])) > tp_index(a))
    i0 = np.nonzero(m)[0][0]
    idx = np.nonzero(m)[0]
    return i0, idx


def residuals(p, runs):
    res = []
    for a, Ta in runs:
        i0, idx = fit_segment(a)
        t0 = a["t"][i0]
        out = simulate(
            p,
            a["bt"][i0],
            a["et"][i0],
            a["heat"][i0] / 100.0,
            heat_zoh(a),
            Ta,
            t0,
            a["t"][idx[-1]] + 1,
        )
        ts, Bs, Es = out
        tt = a["t"][idx]
        res.append(np.interp(tt, ts, Bs) - a["bt"][idx])
        res.append(0.5 * (np.interp(tt, ts, Es) - a["et"][idx]))
    return np.concatenate(res)


def fit(runs):
    r = least_squares(residuals, P0, bounds=(LO, HI), args=(runs,), x_scale="jac", max_nfev=400)
    return r.x, float(np.sqrt(np.mean(r.fun**2)))


def state_at(a, k, win=30.0):
    """Smoothed (B, E) at sample k from a 30 s LS line on data up to k only."""
    t, bt, et = a["t"][: k + 1], a["bt"][: k + 1], a["et"][: k + 1]
    pb, pe = ls_line(t, bt, win), ls_line(t, et, win)
    if pb is None or pe is None:
        return None
    return pb[1], pe[1]


def heater_state(p, a, k):
    """Replay the heater lag on applied heat% up to sample k (inputs only)."""
    tau = p[5]
    Q = a["heat"][0] / 100.0
    f = heat_zoh(a)
    t = a["t"][0]
    while t < a["t"][k]:
        Q += (f(t) - Q) / tau
        t += 1.0
    return Q


def pred_C(p, a, k, Ta, target, hold_heat=None):
    st = state_at(a, k)
    if st is None:
        return None
    B0, E0 = st
    if target <= B0:
        return None
    Q0 = heater_state(p, a, k)
    h = a["heat"][k] / 100.0 if hold_heat is None else hold_heat
    return simulate(p, B0, E0, Q0, lambda _t: h, Ta, a["t"][k], a["t"][k] + 900, stop_bt=target)


# ---------------- evaluation
BT_PTS = [155, 160, 165, 170]
LEAD_PTS = [120, 90, 60]


def query_indices(a, truth):
    q = {}
    for x in BT_PTS:
        i = np.nonzero(a["bt"] >= x)[0]
        i = i[i > tp_index(a)]
        q[f"BT{x}"] = int(i[0]) if len(i) else None
    for L in LEAD_PTS:
        i = np.nonzero(a["t"] <= truth - L)[0]
        q[f"-{L}s"] = int(i[-1]) if len(i) else None
    return q


def a_1hz_emulation(a, k):
    """Runtime-like 1 Hz integer BT (linear interp of stored samples, rounded) up to sample k."""
    tt = np.arange(a["t"][0], a["t"][k] + 1e-9, 1.0)
    return tt, np.round(np.interp(tt, a["t"][: k + 1], a["bt"][: k + 1]))


def hold_last_valid(fn, a, k, lookback=60):
    """D239 semantics: an invalid sample keeps the previous estimate."""
    for j in range(k, max(k - lookback, 0), -1):
        v = fn(j)
        if v is not None:
            return v, j != k
    return None, True


def evaluate(label, a, Ta, p, target, truth, native_1hz=False):
    q = query_indices(a, truth)
    out = {}
    for name, k in q.items():
        if k is None:
            out[name] = None
            continue
        t, bt = a["t"][: k + 1], a["bt"][: k + 1]
        r = {}
        if native_1hz:
            r["A"] = pred_A(t, bt, target)
        else:
            r["A_store"] = pred_A(t, bt, target)
            et_, eb_ = a_1hz_emulation(a, k)
            r["A"] = pred_A(et_, eb_, target)
        r["B"] = pred_B(t, bt, target)
        r["C"] = pred_C(p, a, k, Ta, target)
        out[name] = {kk: (None if v is None else round(v - truth, 1)) for kk, v in r.items()}
        out[name]["t"] = round(float(a["t"][k]), 1)
    return out


def summarise(rows, keys, preds):
    lines = []
    hdr = f"{'point':7s} " + " ".join(f"{pp:>22s}" for pp in preds)
    lines.append(hdr)
    lines.append(" " * 8 + " ".join(f"{'med|abs| / medbias / worst':>22s}" for _ in preds))
    for key in keys:
        cells = []
        for pp in preds:
            e = np.array([r[key][pp] for r in rows if r[key] and r[key].get(pp) is not None])
            if len(e) == 0:
                cells.append(f"{'n/a':>22s}")
                continue
            w = e[np.argmax(np.abs(e))]
            cells.append(
                f"{np.median(np.abs(e)):6.1f} /{np.median(e):+6.1f} /{w:+6.0f} n{len(e):<2d}"
            )
        lines.append(f"{key:7s} " + " ".join(f"{c:>22s}" for c in cells))
    return "\n".join(lines)


def main():
    os.makedirs(OUT, exist_ok=True)
    labels = sorted(lab for lab in META if lab != NOV)
    data = {lab: load(lab) for lab in labels}
    amb = {lab: META[lab]["ambient_c"] for lab in labels}
    fc_ev = {lab: META[lab]["fc_charge_s"] for lab in labels}
    fc_bt = {lab: META[lab]["bt_at_fc_telemetry"] for lab in labels}
    t176 = {lab: first_cross(data[lab], TARGET) for lab in labels}

    # full fit
    p_all, rms_all = fit([(data[lab], amb[lab]) for lab in labels])
    # ablation: ambient fixed at the mean (no room information)
    mean_amb = float(np.mean(list(amb.values())))
    p_noamb, rms_noamb = fit([(data[lab], mean_amb) for lab in labels])

    results = {"T176": [], "FCev": []}
    loo_params = {}
    for lab in labels:
        train = [x for x in labels if x != lab]
        p, rms = fit([(data[x], amb[x]) for x in train])
        loo_params[lab] = dict(zip(PN, map(float, p), strict=True)) | {"train_rms": rms}
        r1 = evaluate(lab, data[lab], amb[lab], p, TARGET, t176[lab])
        # ablation: same model, room temperature replaced by the training-set
        # mean (no room information)
        ta_m = float(np.mean([amb[x] for x in train]))
        p_na, _ = fit([(data[x], ta_m) for x in train])
        r1na = evaluate(lab, data[lab], ta_m, p_na, TARGET, t176[lab])
        for kk in r1:
            if r1[kk] and r1na[kk]:
                r1[kk]["C_noamb"] = r1na[kk]["C"]
        r1["label"] = lab
        r1["ambient"] = amb[lab]
        results["T176"].append(r1)
        tgt = float(np.mean([fc_bt[x] for x in train]))
        r2 = evaluate(lab, data[lab], amb[lab], p, tgt, fc_ev[lab])
        # oracle: run's own FC temperature (C only)
        r2o = evaluate(lab, data[lab], amb[lab], p, fc_bt[lab], fc_ev[lab])
        for kk in r2:
            if r2[kk] and r2o[kk]:
                r2[kk]["C_own"] = r2o[kk]["C"]
        r2["label"] = lab
        r2["ambient"] = amb[lab]
        r2["target"] = tgt
        results["FCev"].append(r2)
        print(
            lab,
            amb[lab],
            {k: v for k, v in r1.items() if k.startswith("BT") or k.startswith("-")},
            flush=True,
        )

    keys = [f"BT{x}" for x in BT_PTS] + [f"-{L}s" for L in LEAD_PTS]
    rep = []
    rep.append("=== Truth = first BT>=176 C (target 176), LOO, all 15 runs")
    rep.append(summarise(results["T176"], keys, ["A", "A_store", "B", "C", "C_noamb"]))
    cool = [r for r in results["T176"] if r["ambient"] < 24.5]
    rep.append(f"\n=== Same, coolest runs only (ambient < 24.5 C, n={len(cool)})")
    rep.append(summarise(cool, keys, ["A", "A_store", "B", "C", "C_noamb"]))
    warm = [r for r in results["T176"] if r["ambient"] >= 24.5]
    rep.append(f"\n=== Same, ambient >= 24.5 C (n={len(warm)})")
    rep.append(summarise(warm, keys, ["A", "A_store", "B", "C", "C_noamb"]))
    rep.append(
        "\n=== Truth = FC event (audio/operator, lagged), target = LOO mean FC-event BT;"
        " C_own = run's own FC BT (oracle)"
    )
    rep.append(summarise(results["FCev"], keys, ["A", "B", "C", "C_own"]))

    # ---------- winter check on 7 Nov (native ~1 Hz SSE stream)
    nov = load(NOV, NOV + "_1hz")
    k_trim = int(np.nonzero(nov["heat"] < 100)[0][0])  # first sample with heat 65
    k_pre = k_trim - 1
    t176n = first_cross(nov, TARGET)
    wint = {
        "t_trim": float(nov["t"][k_trim]),
        "bt_trim": float(nov["bt"][k_trim]),
        "observed_s_to_176": round(t176n - nov["t"][k_trim], 2),
        "fc_mark_s": META[NOV]["fc_charge_s"],
    }
    # A as the controller saw it: previous five samples, evaluated before appending the trim sample
    tA = pred_A(nov["t"][:k_trim], nov["bt"][:k_trim], TARGET)
    wint["A_eta_s"] = round(tA - nov["t"][k_trim - 1], 2)  # measured from the last sample used
    tB = pred_B(nov["t"][:k_trim], nov["bt"][:k_trim], TARGET)
    wint["B_eta_s"] = round(tB - nov["t"][k_trim - 1], 2)
    for Ta in (18.0, 19.5, 21.0, mean_amb, 31.6):
        for hold in (0.65, 1.0):
            tc = pred_C(p_all, nov, k_pre, Ta, TARGET, hold_heat=hold)
            wint[f"C_eta_s_Ta{Ta:.1f}_hold{int(hold * 100)}"] = (
                None if tc is None else round(tc - nov["t"][k_pre], 2)
            )
    tc = pred_C(p_noamb, nov, k_pre, mean_amb, TARGET, hold_heat=0.65)
    wint["C_noamb_eta_s_hold65"] = round(tc - nov["t"][k_pre], 2)
    # whole-trace replay on 7 Nov at Ta 19.5 (C fitted on all 15)
    rn = evaluate(NOV, nov, 19.5, p_all, TARGET, t176n, native_1hz=True)
    wint["eval_points_vs_T176"] = rn
    # fit residual on 7 Nov (open-loop simulation) for each ambient
    for Ta in (18.0, 19.5, 21.0):
        i0, idx = fit_segment(nov)
        ts, Bs, Es = simulate(
            p_all,
            nov["bt"][i0],
            nov["et"][i0],
            1.0,
            heat_zoh(nov),
            Ta,
            nov["t"][i0],
            nov["t"][idx[-1]] + 1,
        )
        eb = np.interp(nov["t"][idx], ts, Bs) - nov["bt"][idx]
        wint[f"openloop_bt_rms_Ta{Ta}"] = round(float(np.sqrt(np.mean(eb**2))), 2)
        tcross = simulate(
            p_all,
            nov["bt"][i0],
            nov["et"][i0],
            1.0,
            heat_zoh(nov),
            Ta,
            nov["t"][i0],
            nov["t"][i0] + 900,
            stop_bt=TARGET,
        )
        wint[f"openloop_t176_Ta{Ta}"] = None if tcross is None else round(tcross, 1)
    wint["observed_t176"] = t176n

    # ambient sensitivity of the fitted model: s to 176 from a fixed summer-like state
    sens = {}
    for Ta in (18, 21, 24, 28, 32):
        tc = simulate(p_all, 157.0, 209.0, 0.65, lambda _t: 0.65, Ta, 0.0, 900.0, stop_bt=TARGET)
        sens[Ta] = None if tc is None else round(tc, 1)

    params = {
        "model": (
            "dB/dt=kb(E-B)-cb; dE/dt=g*Q-ka(E-Ta)-ke(E-B); dQ/dt=(heat-Q)/tau;"
            " dt=1 s, Celsius, heat fraction"
        ),
        "fit_all_15": dict(zip(PN, map(float, p_all), strict=True)) | {"rms_weighted": rms_all},
        "fit_all_15_ambient_fixed_at_mean": dict(zip(PN, map(float, p_noamb), strict=True))
        | {"rms_weighted": rms_noamb, "Ta": mean_amb},
        "loo": loo_params,
        "s_to_176_from_157C_heat65_by_ambient": sens,
    }
    with open(os.path.join(OUT, "fitted_params.json"), "w") as fh:
        json.dump(params, fh, indent=1)
    with open(os.path.join(OUT, "results.json"), "w") as fh:
        json.dump({"loo": results, "winter": wint}, fh, indent=1, default=float)
    txt = "\n".join(rep)
    Path(OUT, "tables.txt").write_text(
        txt
        + "\n\nWINTER\n"
        + json.dumps(wint, indent=1, default=float)
        + "\n\nPARAMS\n"
        + json.dumps(params["fit_all_15"], indent=1)
        + "\nNOAMB\n"
        + json.dumps(params["fit_all_15_ambient_fixed_at_mean"], indent=1)
        + "\nSENS\n"
        + json.dumps(sens)
    )
    print(txt)
    print(json.dumps(wint, indent=1, default=float))
    print(params["fit_all_15"], params["fit_all_15_ambient_fixed_at_mean"], sens)


if __name__ == "__main__":
    main()
