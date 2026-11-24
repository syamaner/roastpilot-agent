# First crack: temperature threshold or roast-dependent target? (offline, 24 Nov 2026, #1138)

Outcome: the largest and most robust error is calibration. The fixed 176 °C target fires early on every dataset, and a crack temperature calibrated on the roaster's own runs removes most of that bias. Beyond calibration, this data cannot tell a temperature threshold from a dry-end timing model on the Artisan logs. The agent's own runs favour timing, but only under one heat schedule. A per-bean temperature adds nothing over a calibrated global value once whole roasting days are held out. Recommendation: recalibrate the default crack temperature from the agent's own runs, treat crack temperatures taken from Artisan logs as non-transferable, and log a timing prediction in shadow before any change to what the anchor aims at.

Reproduce: `scripts/extract_alog.py <alog_dir> <series_dir>` reads the Artisan logs, and `scripts/extract_store.py <sqlite> <series_dir>` reads the agent store read-only. Then `python -I scripts/study.py <series_dir>... <out_dir>` writes `tables.txt` and `results.json`; `python -I scripts/fan_check.py <alog_dir>` prints the fan and burner check. Roast series are not committed (repo rule); `results/` holds derived aggregates only, and a re-run from the same series reproduces them byte for byte.

## Question

The first study (`docs/research/fc-predictor`) scored predictors against T176, the moment the bean reaches 176 °C. This study scores against first crack itself. It asks whether a crack temperature, fixed or per bean, is the right target for the D239 anchor, or whether a roast-dependent target does better.

## Data

- Artisan: the operator's 47 Hottop logs (June 2024 to October 2025, 16 roasts from November to March). Bean labels are coarse and come from file names. Two same-session roasts with no origin in the name are assigned to Cuba by hand, and "unknown" counts as a label. Truth is the operator's marked first crack, by ear.
- Agent: 19 summer runs from the agent store (5 origins, about one row per 6 s). Truth is the controller's first-crack onset, recovered from the stored development percentage. The event payload has no backdate field, so a run is kept only when that onset sits more than 5 s before the detection event (16 runs, 20.6-27.9 s earlier) or the crack was operator-marked (3 runs). One run (21 Jun 2026, before the backdate feature) is excluded.
- Dry end is defined as the live controller defines it (#351): the first crossing of 150 °C after the turning point. On the Artisan data this sits a median 3 s from Artisan's own dry mark.

## Method

- Targets:
  - `fixed`: 176 °C.
  - `global`: the median crack temperature.
  - `bean`: the bean's median crack temperature, falling back to `global` when the bean has no other roast.
  - `time_global`: dry end plus the median dry-end-to-crack time.
  - `time_bean`: dry end plus the bean's median dry-end-to-crack time.
  - `blend`: the mean of the `bean` and `time_global` predictions.
  - `dry` and `bean_dry`: a crack temperature that rises linearly with time since dry end.
- The temperature targets feed the same live 30 s least-squares slope the anchor uses; the timing models do not use the slope.
- Every fitted value is fitted with the scored roast's whole roasting day left out, because same-day roasts share a lot and leaving out only the roast flatters the per-bean values.
- `freeze` is the decision moment. It replays the live anchor window (open at 155 °C with an ETA of at most 120 s; freeze at an ETA of 45 s or at 171 °C, moved by the target's offset from 176 °C) and scores each temperature target's own frozen anchor. The timing models and the blend are read at the per-bean target's freeze.
- The freeze replay differs from the live rule in two ways. On a temperature freeze it uses this tick's projection, where the live tracker keeps the previous tick's anchor. It also drops roasts that never freeze before the crack, which affects 3 Artisan roasts and 1 agent run, all with late targets. Emulating the live semantics moves the calibrated targets' medians by about 1 s or less, and moves `fixed` on the agent runs from 63.3 to 55.0 s. No conclusion changes.
- The fixed-lead rows (-120 s to -45 s before the marked crack) are anchored on the outcome. The -120 s row drops the roasts with the shortest dry-end-to-crack times, so it is shown only for reference.

## Results

Each cell is median |error| / median bias / p90 |error|, in seconds (predicted minus marked). `results/tables.txt` has every target, every point and the n per cell.

Artisan, all 47:

```
point   fixed            global           bean             time_global      blend
freeze  20.3/-13.4/38.6  12.1/ -1.1/38.8  12.2/ -2.0/38.8  13.0/ -1.0/37.5  14.7/ +1.0/33.9
-60s    17.9/-14.2/36.2  11.5/ +0.1/35.7  12.3/ +0.7/32.5  13.0/ +0.0/40.0  15.5/ +1.2/34.7
BT170   16.1/-14.0/33.1  12.7/ -2.0/35.6  11.9/ -1.0/38.9  13.0/ -1.2/37.0  14.4/ +0.7/25.6
```

Artisan, 16 winter roasts:

```
point   fixed            global           bean             time_global      blend
freeze  19.9/-16.9/41.3   7.2/ -4.8/30.2   7.0/ -2.2/26.6   9.0/ -9.0/26.0   2.5/ -0.4/25.1
-60s    17.8/-16.2/36.2   7.9/ -5.2/46.4   7.8/ +1.0/27.5  10.8/ -5.5/27.0   4.1/ -0.4/26.8
BT170   17.6/-15.3/32.3   8.5/ -4.8/31.1   6.0/ -1.4/26.6   9.0/ -9.0/26.0   2.7/ -0.0/25.6
```

Agent, 19 summer runs:

```
point   fixed             global           bean             time_global      blend
freeze  63.3/-63.3/118.5  18.6/ +1.6/54.1  30.8/ -8.6/60.1  11.5/ +2.3/30.9  19.1/ +0.7/41.0
-60s    49.1/-49.1/60.0   12.9/ -5.0/53.9  24.4/-11.3/52.1  11.2/ +1.5/30.9  18.0/ -1.3/40.6
BT170   51.7/-51.7/112.9  22.7/ -5.7/79.7  31.7/ -7.5/66.4  11.2/ +1.5/30.9  16.3/ -0.4/45.7
```

| Data | Crack °C median (sd) | Dry end to crack, s median (sd) |
|---|---|---|
| Artisan | 178.0 (3.9) | 154 (24) |
| Agent | 182.0 (3.0) | 227 (16) |

## Findings

- **Calibration dominates.** The fixed 176 °C target is early at the freeze by a median 13-17 s on the Artisan logs and 63 s on the agent runs. A calibrated global median removes most of that: 12.1 s on Artisan, 18.6 s on agent. The agent runs crack at a median 182 °C, so 176 °C is about 6 °C low for the agent's own profile.
- **Per-bean temperature adds nothing measurable here.** With whole days held out, `bean` ties `global` on the Artisan logs (12.2 against 12.1 at the freeze) and is worse on the agent runs (30.8 against 18.6). Same-bean roasts on different days crack at different temperatures, and most beans have very few roasts.
- **Temperature against timing is a tie on the Artisan logs and favours timing on the agent runs.** At the freeze, Artisan gives `global` 12.1 against `time_global` 13.0; agent gives 18.6 against 11.5. The agent result comes from one heat schedule: a 100 to 60-65 % trim shortly after dry end, with the fan fixed at 30 %.
- **The crack-temperature drift with time is not evidence of a time event.** Because dry end is the 150 °C crossing, crack temperature minus 150 is the mean rate of rise times the dry-end-to-crack time. So a strong correlation between the two (r 0.89 within bean on Artisan) follows largely from that identity. A late ear mark, which reads both later and hotter, would produce the same pattern. This study cannot separate the two explanations.
- **No detectable fan effect.** On the 40 Artisan roasts with a control track, the time-weighted fan between dry end and crack ranged from 0 to 87 % (median 20 %). It showed no detectable relation to crack temperature (r 0.12) or to dry-end-to-crack time (r 0.23); n is small. Burner was 97-100 % on every roast, so these logs say nothing about the effect of heat changes.
- **The agent/Artisan gap is confounded.** The agent cracks 4 °C hotter with 73 s more between dry end and crack. A pure rate-of-rise effect would predict about 10 °C, more than double what is observed. The datasets also differ in crack truth (ear against backdated audio), lots, years and heat schedule. Crack temperatures taken from Artisan logs should not be assumed to transfer to the agent.
- **The winter blend result is fragile.** The blend has the best median in the 15 winter roasts at the freeze (2.5 s), but it was picked after the fact from many candidates. Its p90 (25 s) is no better than the other calibrated targets, and across all roasts (n 45 at the freeze) it is behind `global` and `bean`.
- **A crack temperature that rises with time is rejected.** The target climbs at close to, and on the agent runs faster than, the bean's own rate of rise. The crossing is then unstable or does not exist, and `tables.txt` shows the shrinking n.

## Limits

- 47 + 19 roasts, one roaster, many beans with one or two roasts. The agent has no winter runs in this store.
- Artisan crack marks are by ear. Agent crack truth is the backdated audio onset for 16 runs and an operator mark for 3; those 3 are the hottest (186-188 °C against at most 184 °C), which suggests late marks. The agent median is unchanged without them.
- The timing models assume this roast follows the heat schedule of the roasts they were fitted on. Under the finish law the controller changes heat, so a duration fitted on the old trim schedule would be biased and would need refitting or conditioning on heat.
- Errors are pooled across beans; the #1138 rule's "within beans" condition is approximated by leave-one-day-out, not tested bean by bean.

## Recommendation

1. **Recalibrate the default crack temperature from the agent's own runs.** The agent cracks at a median 182 °C on its own probe and profile; 176 °C anchors about a minute early. That 182 °C was measured under the summer trim schedule, and crack temperature moves with the dry-end-to-crack time, so re-check it on winter and finish-law roasts. This is a config default with a plan decision, so it is proposed here, not changed.
2. **Treat Artisan-derived per-bean crack temperatures as non-transferable.** Leave the #1135 field blank and use the recalibrated global value, until per-bean values beat it with whole days held out on agent-recorded runs.
3. **Log a dry-end timing prediction in shadow** next to the anchor (the slice 2c pattern), refitted on the agent's runs and conditioned on, or at least labelled with, the heat schedule. Decide on any change to the anchor's target after winter roasts, when heat actually varies.
