"""Do fan or burner settings explain the crack-temperature spread? (#1138)

Usage: python -I fan_check.py <alog_dir>

Reads the Artisan control track (event type 0 = fan, 3 = burner, value
(raw - 1) * 10 %), takes the time-weighted setting between dry end (150 °C
after the turning point) and the marked crack, and prints correlations.
Read-only; prints aggregates only.
"""

import ast
import glob
import os
import statistics as st
import sys

DRY_C = 150.0
FAN, BURNER = 0, 3


def corr(a, b):
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else float("nan")


def residuals(y, columns):
    """OLS residuals of ``y`` on ``columns`` plus an intercept."""
    cols = [[1.0] * len(y), *columns]
    k = len(cols)
    a = [
        [sum(ci * cj for ci, cj in zip(cols[i], cols[j], strict=True)) for j in range(k)]
        for i in range(k)
    ]
    b = [sum(ci * yi for ci, yi in zip(cols[i], y, strict=True)) for i in range(k)]
    for i in range(k):
        p = max(range(i, k), key=lambda r, i=i: abs(a[r][i]))
        a[i], a[p] = a[p], a[i]
        b[i], b[p] = b[p], b[i]
        for r in range(k):
            if r != i and a[i][i]:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * z for x, z in zip(a[r], a[i], strict=True)]
                b[r] -= f * b[i]
    beta = [b[i] / a[i][i] for i in range(k)]
    return [y[r] - sum(beta[i] * cols[i][r] for i in range(k)) for r in range(len(y))], beta


def mean_setting(events, kind, dry, fc):
    """Time-weighted mean of a piecewise-constant setting over [dry, fc]."""
    pts = sorted((t, v) for t, k, v in events if k == kind)
    cur, acc, last, start = None, 0.0, dry, dry
    for t, v in pts:
        if t <= dry:
            cur = v
            continue
        if t >= fc:
            break
        if cur is None:
            start = t  # no setting known before this event: average from here
        else:
            acc += cur * (t - last)
        cur, last = v, t
    if cur is None:
        return None
    return (acc + cur * (fc - last)) / (fc - start)


rows = []
for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.alog"))):
    with open(path, encoding="utf-8", errors="replace") as fh:
        d = ast.literal_eval(fh.read())
    ti = d.get("timeindex") or []
    tx = d.get("timex") or []
    bt = d.get("temp2") or []
    if len(ti) < 3 or not ti[2]:
        continue
    t0 = tx[ti[0]] if ti[0] else 0.0
    fc = tx[ti[2]]
    tp = min(range(len(bt)), key=lambda i: bt[i] if t0 < tx[i] < fc else 1e9)
    dry = next((tx[i] for i in range(tp, len(bt)) if bt[i] >= DRY_C), None)
    if dry is None:
        continue
    events = [
        (tx[i], k, (v - 1) * 10)
        for i, k, v in zip(
            d.get("specialevents") or [],
            d.get("specialeventstype") or [],
            d.get("specialeventsvalue") or [],
            strict=False,
        )
        if i < len(tx)
    ]
    fan = mean_setting(events, FAN, dry, fc)
    heat = mean_setting(events, BURNER, dry, fc)
    crack = float((d.get("computed") or {}).get("FCs_BT") or bt[ti[2]])
    rows.append((crack, fc - dry, fan, heat))

ok = [r for r in rows if r[2] is not None and r[3] is not None]
temp = [r[0] for r in ok]
dur = [r[1] for r in ok]
fan = [r[2] for r in ok]
heat = [r[3] for r in ok]
print(f"roasts {len(rows)}, with fan and burner track {len(ok)}")
for name, x in (("fan", fan), ("burner", heat)):
    print(f"{name} dry->FC mean %: median {st.median(x):.0f}, range {min(x):.0f}-{max(x):.0f}")
    print(
        f"  r({name}, crack temp) {corr(x, temp):.2f}; r({name}, dry->FC time) {corr(x, dur):.2f}"
    )
print(f"r(dry->FC time, crack temp) {corr(dur, temp):.2f}")
rt, _ = residuals(temp, [fan, heat])
rd, _ = residuals(dur, [fan, heat])
print(f"partial r(time, temp | fan, burner) {corr(rd, rt):.2f}")
