"""Extract tidy per-run charge-aligned series (read-only DB access)."""

import csv
import json
import os
import sqlite3
import sys

OUT = sys.argv[1]
SUMMER_DB = "/Users/sertanyamaner/roasts/roastpilot.sqlite3"
NOV_DB = sys.argv[2]
os.makedirs(OUT, exist_ok=True)


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def extract(con, run_id, ambient, label):
    ev = {}
    for kind, src, mono, pl in con.execute(
        "select kind, source, monotonic_seconds, payload_json from roast_events"
        " where run_id=? order by id",
        (run_id,),
    ):
        ev.setdefault(kind, []).append((src, mono, json.loads(pl or "{}")))
    start = ev["run_started"][0][1]
    fc_src, fc_mono, fc_pl = ev["first_crack"][0]
    rows = con.execute(
        "select elapsed_seconds, charge_elapsed_seconds, agent_phase, bean_temp_c, env_temp_c, "
        "heat_level_percent, fan_level_percent, development_percent from telemetry_snapshots "
        "where run_id=? and charge_elapsed_seconds is not null and agent_phase in "
        "('roasting_pre_first_crack','development') order by id",
        (run_id,),
    ).fetchall()
    offs = [r[0] - r[1] for r in rows]
    off = sorted(offs)[len(offs) // 2]
    fc_charge = (fc_mono - start) - off  # event rebased onto run clock, then onto charge clock
    # controller's backdated FC from stored development_percent (dev = (t-fc)/t)
    dev = [(r[1], r[7]) for r in rows if r[2] == "development" and r[7] and r[1] > 0]
    fc_backdated = None
    if dev:
        t, d = dev[-1]
        fc_backdated = t * (1 - d / 100.0)
    path = os.path.join(OUT, f"{label}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "phase", "bt", "et", "heat", "fan"])
        for r in rows:
            w.writerow([round(r[1], 3), r[2], r[3], r[4], r[5], r[6]])
    bt_at_fc = min(rows, key=lambda r: abs(r[1] - fc_charge))[3]
    return dict(
        label=label,
        run_id=run_id,
        ambient_c=ambient,
        fc_source=fc_src,
        fc_charge_s=round(fc_charge, 2),
        fc_event_bt=fc_pl.get("bean_temp_c"),
        bt_at_fc_telemetry=bt_at_fc,
        fc_backdated_s=None if fc_backdated is None else round(fc_backdated, 2),
        clock_offset_s=round(off, 3),
        clock_offset_spread=round(max(offs) - min(offs), 3),
        n=len(rows),
    )


meta = []
con = ro(SUMMER_DB)
for rid, ts, amb in con.execute(
    "select id, started_at_utc, ambient_temp_c from roast_runs"
    " where ambient_temp_c is not null order by started_at_utc"
).fetchall():
    meta.append(
        extract(con, rid, amb, f"{ts[:10]}_{rid[:8]}")
        | {
            "started_at_utc": ts,
            "profile": json.loads(
                con.execute("select profile_json from roast_runs where id=?", (rid,)).fetchone()[0]
            ).get("name"),
        }
    )
con2 = ro(NOV_DB)
m = extract(con2, "ef6d89f0e72a4b94959a406c4224d656", None, "2026-11-07_ef6d89f0")
m["profile"] = json.loads(
    con2.execute(
        "select profile_json from roast_runs where id=?", ("ef6d89f0e72a4b94959a406c4224d656",)
    ).fetchone()[0]
).get("name")
meta.append(m)
with open(os.path.join(OUT, "runs.json"), "w") as fh:
    json.dump(meta, fh, indent=1)
for x in meta:
    print(x)
