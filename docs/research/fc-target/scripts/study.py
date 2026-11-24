"""First crack: temperature threshold or roast-dependent target? (#1138)

Usage: python -I study.py <series_dir> [<series_dir> ...] <out_dir>

Scores the predicted first-crack time against the marked crack for several
targets, every target fed by the same live 30 s least-squares slope the D239
anchor uses. Every fitted quantity is leave-one-out: the scored roast never
contributes to its own target. Writes ``tables.txt`` and ``results.json``.
"""

import csv
import json
import math
import os
import statistics as st
import sys

*SERIES, OUT = sys.argv[1:]
os.makedirs(OUT, exist_ok=True)

FIXED_C = 176.0
DRY_C = 150.0
WINDOW_S = 30.0
LEADS_S = (120.0, 90.0, 60.0, 45.0)
TEMPS_C = (165.0, 170.0)


def load(series_dir):
    with open(os.path.join(series_dir, "runs.json")) as fh:
        runs = json.load(fh)
    out = []
    for r in runs:
        with open(os.path.join(series_dir, f"{r['label']}.csv")) as fh:
            pts = [(float(a["t"]), float(a["bt"])) for a in csv.DictReader(fh)]
        pts = [p for p in pts if p[0] >= 0.0]
        # Dry end as the live controller defines it (#351): first crossing of
        # 150 °C after the turning point.
        tp = min(range(len(pts)), key=lambda i: pts[i][1] if pts[i][0] < r["fc_s"] else 1e9)
        dry = next((t for t, b in pts[tp:] if b >= DRY_C), None)
        if dry is None or dry >= r["fc_s"]:
            continue
        out.append(r | {"pts": pts, "dry_s": dry, "d2fc": r["fc_s"] - dry})
    return out


def slope_at(pts, t_eval):
    """30 s least-squares slope (°C/s) and fitted BT at ``t_eval``."""
    w = [(t, b) for t, b in pts if t_eval - WINDOW_S <= t <= t_eval]
    if len(w) < 4:
        return None
    mt = st.mean(t for t, _ in w)
    mb = st.mean(b for _, b in w)
    den = sum((t - mt) ** 2 for t, _ in w)
    if den <= 0:
        return None
    s = sum((t - mt) * (b - mb) for t, b in w) / den
    return s, mb + s * (t_eval - mt)


def linfit(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den if den else 0.0
    return my - b * mx, b


def corr(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else float("nan")


def fitted(train, r):
    """Every leave-one-out quantity the targets need, from ``train`` only."""
    same = [x for x in train if x["bean"] == r["bean"]]
    g_temp = st.median(x["fc_bt"] for x in train)
    bean_temp = st.median(x["fc_bt"] for x in same) if same else g_temp
    a, b = linfit([x["d2fc"] for x in train], [x["fc_bt"] for x in train])
    # Bean intercepts with one shared dry-end slope (within-bean regression).
    by = {}
    for x in train:
        by.setdefault(x["bean"], []).append(x)
    xs, ys = [], []
    for grp in by.values():
        if len(grp) >= 2:
            md = st.mean(x["d2fc"] for x in grp)
            mt = st.mean(x["fc_bt"] for x in grp)
            xs += [x["d2fc"] - md for x in grp]
            ys += [x["fc_bt"] - mt for x in grp]
    bw = linfit(xs, ys)[1] if xs else 0.0
    if same:
        ab = st.mean(x["fc_bt"] - bw * x["d2fc"] for x in same)
    else:
        ab = st.mean(x["fc_bt"] - bw * x["d2fc"] for x in train)
    g_d2 = st.median(x["d2fc"] for x in train)
    bean_d2 = st.median(x["d2fc"] for x in same) if same else g_d2
    return {
        "fixed": FIXED_C,
        "global": g_temp,
        "bean": bean_temp,
        "dry": (a, b),
        "bean_dry": (ab, bw),
        "time_global": g_d2,
        "time_bean": bean_d2,
    }


def predict(kind, p, t0, bt0, s, dry):
    """Predicted crack time from charge, or ``None`` when it cannot project."""
    if kind == "blend":
        a = predict("bean", p, t0, bt0, s, dry)
        b = predict("time_global", p, t0, bt0, s, dry)
        return b if a is None else (a + b) / 2.0
    if kind.startswith("time"):
        return dry + p[kind]
    if kind in ("dry", "bean_dry"):
        a, b = p[kind]
        # Crossing of bt0 + s (t - t0) with a + b (t - dry); b is per second.
        if s - b <= 1e-6:
            return None
        t = (a - b * dry - bt0 + s * t0) / (s - b)
        return t if t >= t0 else t0
    target = p[kind]
    if s <= 1e-6:
        return None
    return t0 + max(0.0, (target - bt0) / s)


KINDS = ["fixed", "global", "bean", "dry", "bean_dry", "time_global", "time_bean", "blend"]


def score(runs):
    errs = {}
    for r in runs:
        train = [x for x in runs if x is not r]
        p = fitted(train, r)
        points = [(f"-{int(lead)}s", r["fc_s"] - lead) for lead in LEADS_S]
        for temp in TEMPS_C:
            t = next((t for t, b in r["pts"] if b >= temp and t > r["dry_s"]), None)
            if t is not None and t < r["fc_s"]:
                points.append((f"BT{int(temp)}", t))
        for name, t_eval in points:
            if t_eval <= r["dry_s"]:
                continue  # the dry-end models need a dry end already seen
            sl = slope_at(r["pts"], t_eval)
            if sl is None:
                continue
            s, bt0 = sl
            for k in KINDS:
                pred = predict(k, p, t_eval, bt0, s, r["dry_s"])
                if pred is not None:
                    errs.setdefault((name, k), []).append(
                        (pred - r["fc_s"], r["winter"], r["label"])
                    )
    return errs


def summary(es):
    a = sorted(abs(e) for e in es)
    return {
        "n": len(es),
        "median_abs": round(st.median(a), 1),
        "bias": round(st.median(es), 1),
        "p90_abs": round(a[min(len(a) - 1, int(0.9 * len(a)))], 1),
        "worst": round(max(es, key=abs), 1),
    }


def table(errs, subset):
    names = sorted({n for n, _ in errs}, key=lambda n: (n[0] != "-", n))
    lines = ["point     " + "".join(f"{k:>18}" for k in KINDS)]
    out = {}
    for n in names:
        cells = []
        for k in KINDS:
            es = [e for e, w, _ in errs.get((n, k), []) if subset(w)]
            if not es:
                cells.append(f"{'-':>18}")
                continue
            sm = summary(es)
            out[f"{n}/{k}"] = sm
            cells.append(f"{sm['median_abs']:>6}/{sm['bias']:>+6}/{sm['p90_abs']:>5}")
        lines.append(f"{n:<10}" + "".join(cells))
    return lines, out


def within_bean(runs):
    by = {}
    for r in runs:
        by.setdefault(r["bean"], []).append(r)
    xs, ys, beans = [], [], 0
    for grp in by.values():
        if len(grp) >= 2:
            beans += 1
            md = st.mean(x["d2fc"] for x in grp)
            mt = st.mean(x["fc_bt"] for x in grp)
            xs += [x["d2fc"] - md for x in grp]
            ys += [x["fc_bt"] - mt for x in grp]
    a, b = linfit(xs, ys)
    return {
        "beans": beans,
        "roasts": len(xs),
        "r": round(corr(xs, ys), 2),
        "slope_c_per_min": round(b * 60, 2),
    }


report, results = [], {}
for sdir in SERIES:
    runs = load(sdir)
    src = runs[0]["source"] if runs else os.path.basename(sdir)
    fc = [r["fc_bt"] for r in runs]
    d2 = [r["d2fc"] for r in runs]
    wb = within_bean(runs)
    head = {
        "roasts": len(runs),
        "winter": sum(r["winter"] for r in runs),
        "beans": len({r["bean"] for r in runs}),
        "fc_bt_median": round(st.median(fc), 1),
        "fc_bt_sd": round(st.stdev(fc), 2),
        "dry_to_fc_median_s": round(st.median(d2), 1),
        "dry_to_fc_sd_s": round(st.stdev(d2), 1),
        "r_fc_bt_vs_dry_to_fc_all": round(corr(d2, fc), 2),
        "within_bean": wb,
    }
    errs = score(runs)
    report.append(f"== {src}: {json.dumps(head)}")
    report.append("Cells: median |e| / median bias / p90 |e|, seconds, predicted - marked.")
    for title, sub in (("all", lambda w: True), ("winter", lambda w: w)):
        lines, out = table(errs, sub)
        report.append(f"-- {title}")
        report += lines
        results[f"{src}/{title}"] = out
    results[f"{src}/summary"] = head
    report.append("")

with open(os.path.join(OUT, "tables.txt"), "w") as fh:
    fh.write("\n".join(line.rstrip() for line in report) + "\n")
with open(os.path.join(OUT, "results.json"), "w") as fh:
    json.dump(results, fh, indent=1, sort_keys=True)
print("\n".join(report))
