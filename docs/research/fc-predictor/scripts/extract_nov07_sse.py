"""Decode the 7 Nov parent SSE capture into a 1 Hz charge-aligned CSV (data only)."""

import collections
import csv
import json
import sys

src, out = sys.argv[1], sys.argv[2]
msgs = []
cur = {"event": None, "data": []}
with open(src) as fh:
    for line in fh:
        r = json.loads(line)
        if r["kind"] != "sse_raw_line":
            continue
        raw = r["line"].rstrip("\n")
        if raw == "":
            if cur["data"]:
                msgs.append((cur["event"], "\n".join(cur["data"])))
            cur = {"event": None, "data": []}
        elif raw.startswith("event:"):
            cur["event"] = raw[6:].strip()
        elif raw.startswith("data:"):
            cur["data"].append(raw[5:].lstrip())
tel = [json.loads(d) for e, d in msgs if e == "telemetry"]
print("telemetry payloads", len(tel), collections.Counter(t["agent_phase"] for t in tel))
seen = set()
rows = []
for t in tel:
    ce = t.get("charge_elapsed_seconds")
    if ce is None or t["agent_phase"] not in ("roasting_pre_first_crack", "development"):
        continue
    if ce in seen:
        continue
    seen.add(ce)
    rows.append(t)
rows.sort(key=lambda t: t["charge_elapsed_seconds"])
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["t", "phase", "bt", "et", "heat", "fan"])
    for t in rows:
        w.writerow(
            [
                round(t["charge_elapsed_seconds"], 3),
                t["agent_phase"],
                t["bean_temp_c"],
                t["env_temp_c"],
                t["heat_percent"],
                t["fan_percent"],
            ]
        )
print("rows", len(rows), "keys", sorted(rows[0].keys()))
bt = [r["bean_temp_c"] for r in rows]
print("non-integer BT:", sum(1 for b in bt if b != int(b)))
first176 = next(r for r in rows if r["bean_temp_c"] >= 176)
print("first BT>=176 at", first176["charge_elapsed_seconds"])
trim = next(r for r in rows if r["heat_percent"] < 100)
print("first heat<100", trim["charge_elapsed_seconds"], trim["bean_temp_c"], trim["heat_percent"])
