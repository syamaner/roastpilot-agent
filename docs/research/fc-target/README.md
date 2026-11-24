# First crack: temperature threshold or roast-dependent target? (offline, 24 Nov 2026, #1138)

Outcome: first crack behaves more like a time event after dry end than like a fixed temperature. The fixed 176 °C target is consistently early, the per-bean temperature (#1135) removes most of that bias, and a dry-end timing model matches or beats it. Recommendation: log the dry-end timing prediction and a blend in shadow next to the D239 anchor, and decide on switching the anchor target from winter roasts, not from this study alone.

Reproduce: `scripts/extract_alog.py <alog_dir> <series_dir>` reads the Artisan logs, and `scripts/extract_store.py <sqlite> <series_dir>` reads the agent store read-only. Then `python -I scripts/study.py <series_dir>... <out_dir>` writes `tables.txt` and `results.json`. Roast series are not committed (repo rule); `results/` holds derived aggregates only, and a re-run from the same series reproduces them byte for byte.

## Question

The first study (`docs/research/fc-predictor`) scored predictors against T176, the moment the bean reaches 176 °C. This study scores against first crack itself, and asks whether a fixed crack temperature is the right target for the anchor.

## Data

- Artisan: the operator's 47 Hottop logs (June 2024 to October 2025, 14 coarse bean labels, 16 roasts November to March). Truth is the operator's marked first crack, so it carries ear-timing error.
- Agent: 20 summer runs from the agent store (6 bean origins, about one row per 6 s). Truth is the controller's backdated first-crack onset.
- Dry end is defined as the live controller defines it (#351): the first crossing of 150 °C after the turning point.

## Targets compared

Every target feeds the same live 30 s least-squares slope the anchor uses, except the time models, which do not use the slope. Every fitted value is leave-one-out.

- `fixed`: 176 °C.
- `bean`: the bean's median crack temperature, falling back to the global median for a bean seen once.
- `time_global`: dry end plus the median dry-end-to-crack time.
- `time_bean`: the same, using the bean's median duration.
- `blend`: the mean of the `bean` and `time_global` predictions.
- `dry` and `bean_dry`: a crack temperature that rises linearly with time since dry end.

## Results

Each cell is median |error| / median bias / p90 |error|, in seconds (predicted minus marked). The full table, including `global`, `dry` and `bean_dry`, is in `results/tables.txt`.

Artisan, all 47:

```
point   fixed            bean             time_global      time_bean        blend
-120s   19.7/-16.9/35.7  13.2/ -5.9/29.9  12.0/ -2.0/28.5  11.0/ -4.0/37.0  11.2/ -2.3/28.8
-90s    18.0/-12.2/38.7  12.2/ -4.1/40.5  13.0/ +0.5/40.5  14.0/ -2.0/55.0  13.0/ +0.8/37.1
-60s    17.9/-14.2/36.2  11.9/ -2.9/38.2  13.0/ +0.5/40.5  14.0/ -2.0/55.0  14.7/ +0.2/34.8
-45s    14.9/-12.7/37.4  10.3/ +0.4/39.3  13.0/ +0.5/40.5  14.0/ -2.0/55.0  10.4/ +0.8/32.8
BT170   16.1/-14.0/33.1   9.8/ -0.6/37.3  12.8/ -1.5/36.5  12.0/ -4.0/44.5   8.4/ +0.8/31.3
```

Artisan, 16 winter roasts:

```
point   fixed            bean             time_global      blend
-120s   22.5/-21.9/38.2  13.9/ -5.9/32.7   9.0/ -9.0/26.0  11.2/ -3.1/28.8
-60s    17.8/-16.2/36.2   8.2/ +1.3/38.2  10.8/ -5.5/27.0   4.5/ -0.5/29.1
-45s    16.4/-14.4/37.4  11.7/ -1.5/39.3  10.8/ -5.5/27.0   2.8/ -0.2/29.6
BT170   17.6/-15.3/32.3   8.1/ -1.5/26.6   9.0/ -9.0/26.0   3.3/ -0.1/26.3
```

Agent, 20 summer runs:

```
point   fixed            bean             time_global      blend
-120s   63.7/-63.7/101.6 27.9/-23.0/65.9  10.1/ +0.0/30.9  15.9/-11.7/48.4
-60s    50.1/-50.1/60.0  12.0/ -4.7/52.1  10.1/ +0.0/30.9  12.3/ +0.6/40.6
BT165   69.1/-69.1/106.1 34.8/-34.8/64.1  10.1/ +0.0/30.9  15.9/-14.6/46.6
BT170   53.1/-53.1/112.9 26.3/ -3.6/74.2  10.1/ +0.0/30.9  16.8/ +0.1/52.5
```

Crack temperature against the dry-end-to-crack time:

| Data | Crack °C median (sd) | Dry end to crack, s median (sd) | r overall | r within bean | Slope within bean |
|---|---|---|---|---|---|
| Artisan | 178.0 (3.9) | 154 (24) | 0.91 | 0.89 (10 beans, 43 roasts) | 8.8 °C/min |
| Agent | 182.0 (3.2) | 227 (18) | 0.39 | 0.74 (4 beans, 18 runs) | 7.7 °C/min |

## Findings

- The fixed 176 °C target is early everywhere: by 12-22 s on the Artisan roasts and 45-69 s on the agent runs, where the bean is still climbing when the anchor freezes.
- The per-bean temperature removes most of that bias on the Artisan data (median error about 10-13 s), which supports #1135.
- Crack temperature rises with the time spent after dry end, and the effect holds within beans (r 0.89 and 0.74). It is not a bean mix effect. A longer Maillard phase cracks hotter, at roughly 8-9 °C per extra minute.
- That explains most of the agent/Artisan gap. The agent's summer profile spends about 73 s longer between dry end and crack, and cracks about 4 °C hotter. So per-bean values taken from Artisan logs are likely to sit a few degrees low for the agent's own profile, and will anchor early.
- The simple timing model (dry end plus the median duration) matches the per-bean temperature on the Artisan data and clearly beats it on the agent data (10 s against 12-35 s, no bias). Per-bean durations are worse, because most beans have very few roasts.
- The blend is the best candidate near the freeze point in winter (2.8-4.5 s median error at 45-60 s out), and is never far behind elsewhere.
- A crack temperature that rises with time is badly conditioned: the target climbs at nearly the bean's own rate, so the crossing is unstable (p90 errors from about 60 s to over 500 s). It is rejected.

## Limits

- The timing model assumes this roast follows the heat pattern of the roasts it was fitted on. It cannot see a change of heat, while the slope-fed temperature targets can. Under the finish law, the controller's own heat changes would bias a pure timing model, which is why the blend and a shadow trial come before any switch.
- Artisan crack marks are by ear; agent crack truth is the backdated audio onset. Neither is exact.
- 47 + 20 roasts, one roaster. The agent has no winter runs in this store, and many beans have one or two roasts.
- The agent store samples about every 6 s, which coarsens the slope.

## Recommendation

Under the decision rule in #1138 (change the anchor target only if a model clearly beats the per-bean value, within beans), the timing model and the blend qualify on the agent data, and the effect holds within beans. But the open-loop caveat is real, so the proposed next step is observe-only:

1. Log a dry-end timing prediction and the blend in shadow every tick next to the anchor (the slice 2c pattern), with the duration fitted from the agent's own runs.
2. Treat per-bean temperatures taken from Artisan logs as provisional. They come from a faster profile and probably read 2-4 °C low for the agent's.
3. Decide on switching the anchor target after the first winter roasts, against the logged predictions.
