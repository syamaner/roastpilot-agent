# First-crack predictor study (offline, 23 Nov 2026)

Outcome: D239 (roastpilot-plan) anchors control on the 30 s slope (B) and runs the two-node model (C) in shadow, logged every second, until the first winter roast provides a cold-room calibration point.

Reproduce: `scripts/extract.py` reads the local store read-only (path at the top of the script) and writes per-run series; `scripts/extract_nov07_sse.py` extracts the 7 Nov SSE capture; `python -I scripts/study.py <series_dir> <out_dir>` writes `tables.txt`, `fitted_params.json` and `results.json`. Roast series are not committed (repo rule); `results/` holds the derived outputs only, and a re-run from the same series reproduces them exactly (`tables.txt` has trailing whitespace stripped for the repo whitespace check).

Scope: offline only; no repo, production code or hardware touched. Scripts: `scripts/extract.py`, `scripts/extract_nov07_sse.py`, `scripts/study.py`. Results: `out/fitted_params.json`, `out/results.json`, `out/tables.txt`.

## Data

- 15 summer runs with recorded room temperature (23.05–31.61°C), read-only from `~/roasts/roastpilot.sqlite3`. The store keeps roughly one row per 6 s, so predictor A is emulated at 1 Hz (`A_store` applies the literal 5-sample rule to the stored rows).
- 7 Nov (`ef6d89f0`): the D227-live02 database and summary, plus 723 SSE payloads (about 1 Hz, integer BT).
- Clocks: telemetry `elapsed_seconds − charge_elapsed_seconds` is constant per run. Events are rebased as monotonic minus `run_started` monotonic.
- Truth 1 (T176): the first BT ≥ 176°C after the turning point, with no detector lag. Truth 2: the FC event (13 audio, 2 operator), which lags 12–28 s.
- Heat and fan come from telemetry. Every summer run trimmed 100→60/65% at 368–480 s, and fan was 30% throughout, so the fan effect cannot be identified.

## Predictors

- A: the current 5-sample slope toward 176°C.
- B: a 30 s least-squares slope.
- C: a two-node grey-box model, dB/dt = kb(E−B) − cb; dE/dt = g·Q − ka(E−Ta) − ke(E−B); dQ/dt = (heat − Q)/τ. It is fitted to the BT and ET trajectories, then simulated forward holding the current heat. C_noamb is the same model without the room term.

## Leave-one-out error against T176

Each cell is median |e| / median bias / worst, in seconds (predicted − actual).

```
point   A            B            C             C_noamb
BT155   60/-60/-84   19/-16/-42   6.5/-2/-21    9.4/-5/-23
BT160   50/-50/-74   24/-24/-54   6.3/-4/-21    7.7/-6/-20
BT165   43/-43/-64   12/-12/-43   4.0/-1/-23    3.9/0/-22
BT170   24/-24/-36   9.5/-6/-18   2.8/-2/-8     2.7/-2/-8
-120s   54/-54/-70   14/-12/-42   7.2/-1/-21    8.4/-5/-20
-90s    42/-42/-68   12/-12/-39   6.3/-1/-20    5.5/-1/-20
-60s    31/-31/-48   6.9/-2/-24   3.6/0/+12     3.3/0/+12
```

- Coolest five runs (23.05–24.08°C), median |e| at 120 s before: A 56, B 24, C 5.2, C_noamb 6.1.
- Against the FC event, at 120 s before: A 71, B 30, C 15.5. With each run's own FC BT used as an oracle: C 8.3, with −8 s bias.

## 7 Nov winter check (C fitted on all 15)

- At the trim moment (+421.99 s, BT 157°C), the observed time to 176°C was 123.99 s.
- A predicted 43.09 s. This matches the audit, cross-check only.
- B predicted 130.99 s.
- C, holding 65% heat, predicted 147.5 / 148.2 / 149.0 s at a room of 21 / 19.5 / 18°C. C without ambient predicted 146.8 s.
- C open loop: BT RMS 1.15°C, reaching 176°C at 558.9 s against 546.0 s observed.
- Across the trace: C runs +10 to +21 s late, B −22 to +13 s, A −71 to −16 s.

## Findings

- C beats both slopes in summer leave-one-out, but not because of room temperature. Room temperature moves C's prediction by only about 0.5 s/°C, probably because the measured ET already reflects the room. C's gain comes from modelling the heater lag and from using ET.
- On 7 Nov, both B and C predict the slow approach. B is closer (+7 s against +24 s).
- A is 40–60 s early everywhere and should not anchor control.

## Fitted parameters (all 15 runs)

kb 0.00258, cb −0.0146, g 1.011, ka 0.00332, ke 0.00557, τ 270 s.

## Limits

- 15 runs, one machine, 6 bean lots. Winter sits 2–5°C below the fitted range, and its room temperature was unmeasured.
- The parameters are not well identified: across folds g runs 0.65–1.30 and τ 177–345 s. cb < 0 is a non-physical bias term.
- T176 is a temperature landmark, not FC.
- C assumes the current heat holds.
- Closed-loop behaviour is untested.
