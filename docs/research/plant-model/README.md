# Plant-model research: Phase 1 (linear ARX for RoR projection)

Phase 1 of the roast RoR-projection experiment. It asks a single feasibility
question: does a low-order **linear ARX** predict bean rate-of-rise (RoR) at
control-relevant horizons (t+20 / t+30 / t+40 s, past the ~25-35 s thermocouple
lag) well enough to justify building a predictive controller?

The harness is `scripts/plant_model_arx_study.py`. It is offline, deterministic,
uses no network and no paid APIs (numpy least-squares only).

## What is here

| File | What it is |
|---|---|
| `phase1-arx-report.md` | The full report: calibration finding, corpus stats, multi-horizon RMSE (ARX vs naive baselines), the heat-step counterfactual, and the GO / NO-GO / NEEDS-MORE-DATA verdict. |
| `loro_rmse.csv` | Aggregate leave-one-roast-out RMSE table (per model, per horizon, overall + BT>=150 segment). |
| `landmarks.csv` | Per-roast BT landmarks (turnaround / dry-end / FC / drop) used for the calibration check. Aggregate, one row per roast. |
| `model_summary.json` | Every number in the report, machine-readable. |
| `data-manifest.md` | The **data fingerprint**: sha256 + sample count of each `.alog`, and the store run ids used. This is how the inputs are "committed" without committing roast data. |

## Verdict (summary)

**NEEDS MORE DATA** — a conditional no-go on building the controller yet. The ARX
is the right model class and is numerically accurate, but in the control-relevant
regime (BT >= 150 C) it barely beats trivial persistence, and the heat->RoR
signal a predictive controller would exploit is under-excited in the current
operating regime (heat is pinned through development). The unlock is **designed
heat-step excitation**, not more passive roasts, and likely a grey-box FOPDT
model rather than the pooled black-box ARX. See `phase1-arx-report.md` for the
full reasoning.

## Reproduce

From the repo root, with the project venv (`.venv`) set up per `AGENTS.md`:

```bash
# Regenerate the report + aggregate artifacts (writes to --out-dir)
.venv/bin/python scripts/plant_model_arx_study.py \
    --alog-dir "$HOME/Library/Mobile Documents/com~apple~CloudDocs/roasting" \
    --store    "$HOME/roasts/roastpilot.sqlite3" \
    --out-dir  docs/research/plant-model

# Regenerate the data-fingerprint manifest
.venv/bin/python scripts/plant_model_arx_study.py \
    --emit-manifest docs/research/plant-model/data-manifest.md
```

Both `--alog-dir` and `--store` default to the paths above, so on the operator's
machine the flags are optional. The run also writes `step_response_traces.csv`
(raw per-tick predicted-vs-actual on the heat-step segments) into `--out-dir`;
that file is **regenerable and intentionally not committed** (see below).

## Guarantees

- **Deterministic.** numpy least-squares + leave-one-roast-out CV, no `np.random`.
  Re-running on the same inputs yields byte-identical `model_summary.json` and
  `loro_rmse.csv`.
- **Read-only on the store.** The harness copies the SQLite DB to a temp
  directory, verifies the copy's sha256 matches the source, and only ever reads
  the copy. The operator's live DB is never opened read-write.
- **Inputs are checksummed.** `data-manifest.md` pins the exact `.alog` files
  (sha256 + sample count) and store run ids. Regenerate against inputs that match
  those checksums to reproduce the committed numbers.

## Why the raw data is not in the repo

Per `AGENTS.md`, raw roast logs are never committed: no `.alog` files, no SQLite
DB or DB copy, no raw per-tick telemetry (so `step_response_traces.csv` is
excluded too). Only code, the aggregate outputs, and the fingerprint live here.
The raw inputs stay at the documented local paths (and, in future, the Snowflake
`roast_telemetry` table) and every artifact is fully regenerable from the harness.

## Snapshot note

The committed numbers are a snapshot at manifest time. The store DB grows as new
roasts are recorded, so a later re-run may pool one or two more completed roasts
and shift the aggregate numbers by a fraction of a degree; the qualitative
finding (and the verdict) is stable. The run ids in `data-manifest.md` identify
the exact set behind the committed report.
