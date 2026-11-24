"""Extract charge-aligned bean series from the agent's store (read-only).

Usage: python -I extract_store.py <sqlite_path> <out_dir>

One CSV per completed run with a first crack (``t`` seconds from charge, ``bt``
°C) plus ``runs.json``. First-crack truth is the controller's onset, recovered
from the stored development percent. The event payload carries no backdate
field, so a run counts as backdated only when that onset sits more than 5 s
before the event; otherwise the onset is the detection time, which lags the
true crack by 12-28 s, and the run is flagged ``truth_ok: false``. Operator
marks are taken as truth.
"""

import csv
import json
import os
import sqlite3
import sys

DB, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

runs = []
for n, (rid, started, profile_json) in enumerate(
    con.execute("select id, started_at_utc, profile_json from roast_runs order by started_at_utc"),
    1,
):
    ev = con.execute(
        "select monotonic_seconds, source from roast_events where run_id=? and kind='first_crack'"
        " order by id limit 1",
        (rid,),
    ).fetchone()
    start = con.execute(
        "select monotonic_seconds from roast_events where run_id=? and kind='run_started'"
        " order by id limit 1",
        (rid,),
    ).fetchone()
    if ev is None or start is None:
        continue
    rows = con.execute(
        "select elapsed_seconds, charge_elapsed_seconds, agent_phase, bean_temp_c,"
        " development_percent from telemetry_snapshots where run_id=? and"
        " charge_elapsed_seconds is not null and agent_phase in"
        " ('roasting_pre_first_crack','development') order by id",
        (rid,),
    ).fetchall()
    if len(rows) < 20:
        continue
    offs = sorted(r[0] - r[1] for r in rows)
    off = offs[len(offs) // 2]
    fc_event = (ev[0] - start[0]) - off
    dev = [(r[1], r[4]) for r in rows if r[2] == "development" and r[4] and r[1] > 0]
    fc = dev[-1][0] * (1 - dev[-1][1] / 100.0) if dev else fc_event
    lag = fc_event - fc
    truth_ok = ev[1] == "operator" or lag > 5.0
    label = f"agent-{n:02d}"
    with open(os.path.join(OUT, f"{label}.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "bt"])
        for r in rows:
            if r[3] is not None:
                w.writerow([round(r[1], 3), r[3]])
    bt_fc = min(rows, key=lambda r: abs(r[1] - fc))[3]
    name = (json.loads(profile_json or "{}").get("bean_origin") or "unknown").lower()
    name = name.replace("el salvador", "el-salvador")
    month = int(started[5:7])
    runs.append(
        {
            "label": label,
            "source": "agent",
            "bean": name.split()[0] if name else "unknown",
            "date": started[:10],
            "winter": month in (11, 12, 1, 2, 3),
            "fc_s": round(fc, 2),
            "fc_bt": bt_fc,
            "fc_source": ev[1],
            "event_minus_onset_s": round(lag, 1),
            "truth_ok": truth_ok,
        }
    )

with open(os.path.join(OUT, "runs.json"), "w") as fh:
    json.dump(runs, fh, indent=1)
print(len(runs), "runs")
