"""Extract charge-aligned bean series from Artisan .alog files (read-only).

Usage: python -I extract_alog.py <alog_dir> <out_dir>

Writes one CSV per roast (``t`` seconds from charge, ``bt`` °C) and ``runs.json``
with the marked first crack, a coarse bean label and the season. Roast IDs are
anonymised (``artisan-NN`` in file-name order); the logs themselves are never
copied. Artisan files are Python literals, so they are parsed with
``ast.literal_eval`` and never executed.
"""

import ast
import csv
import glob
import json
import os
import sys

SRC, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)

# Coarse bean label from the file name. Files from the same session with no
# origin in the name (the 16 Jul 2025 b1/b3) sit between Cuba roasts.
BEANS = [
    ("brasil-ferm", "brazil-ferment"),
    ("brasil-fm", "brazil-ferment"),
    ("brasil", "brazil"),
    ("brazil", "brazil"),
    ("cub", "cuba"),
    ("25-07-16_1854_b1", "cuba"),
    ("25-07-16_2009-b3", "cuba"),
    ("alisan", "taiwan"),
    ("nantou", "taiwan"),
    ("taiwan", "taiwan"),
    ("kona", "kona"),
    ("jamai", "jamaica"),
    ("costarica", "costa-rica"),
    ("vietnam", "vietnam"),
    ("indo", "indonesia"),
    ("ind2", "indonesia"),
    ("au-nica", "au-nica"),
    ("au-so", "au-so"),
    ("australia", "australia"),
    ("nica", "nicaragua"),
]


def bean_of(name: str) -> str:
    low = name.lower()
    for key, label in BEANS:
        if key in low:
            return label
    return "unknown"


runs = []
paths = sorted(glob.glob(os.path.join(SRC, "*.alog")))
for n, path in enumerate(paths, 1):
    with open(path, encoding="utf-8", errors="replace") as fh:
        d = ast.literal_eval(fh.read())
    ti = d.get("timeindex") or []
    tx = d.get("timex") or []
    bt = d.get("temp2") or []
    if len(ti) < 3 or not ti[2] or not tx:
        continue
    t0 = tx[ti[0]] if ti[0] else 0.0
    label = f"artisan-{n:02d}"
    with open(os.path.join(OUT, f"{label}.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "bt"])
        for t, b in zip(tx, bt, strict=False):
            if t >= t0 and b is not None and b > 0:
                w.writerow([round(t - t0, 3), b])
    iso = str(d.get("roastisodate") or "")
    month = int(iso[5:7]) if len(iso) >= 7 else 0
    runs.append(
        {
            "label": label,
            "source": "artisan",
            "bean": bean_of(os.path.basename(path)),
            "date": iso,
            "winter": month in (11, 12, 1, 2, 3),
            "fc_s": round(tx[ti[2]] - t0, 2),
            "fc_bt": float((d.get("computed") or {}).get("FCs_BT") or bt[ti[2]]),
            "artisan_dry_s": round(tx[ti[1]] - t0, 2) if ti[1] else None,
        }
    )

with open(os.path.join(OUT, "runs.json"), "w") as fh:
    json.dump(runs, fh, indent=1)
print(len(runs), "roasts")
