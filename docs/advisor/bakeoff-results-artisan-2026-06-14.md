# Advisor bake-off — real-roast replay scorecard (#172/#173, D20)

> **Read first — what these numbers mean.** The ground truth is a known-GOOD roast, *not* a provably optimal one. Every metric below measures **agreement with a known-good roast**, NOT absolute correctness: a capable model may legitimately differ from what the human did and still roast well, and high agreement is not proof of quality. Drop F1 = 1.0 means *matched this one good roast*, not *correct*. Use these as a quantitative aid to the operator's judgement (the advice samples + the latency gate), never a replacement for it.

Test set (known-good 7-Jun Hottop roasts): .artisan-fixtures/artisan-01, .artisan-fixtures/artisan-02, .artisan-fixtures/artisan-03, .artisan-fixtures/artisan-04, .artisan-fixtures/artisan-05, .artisan-fixtures/artisan-06, .artisan-fixtures/artisan-07, .artisan-fixtures/artisan-08, .artisan-fixtures/artisan-09, .artisan-fixtures/artisan-10, .artisan-fixtures/artisan-11, .artisan-fixtures/artisan-12, .artisan-fixtures/artisan-13, .artisan-fixtures/artisan-14, .artisan-fixtures/artisan-15, .artisan-fixtures/artisan-16, .artisan-fixtures/artisan-17, .artisan-fixtures/artisan-18, .artisan-fixtures/artisan-19, .artisan-fixtures/artisan-20, .artisan-fixtures/artisan-21, .artisan-fixtures/artisan-22, .artisan-fixtures/artisan-23, .artisan-fixtures/artisan-24, .artisan-fixtures/artisan-25, .artisan-fixtures/artisan-26, .artisan-fixtures/artisan-27, .artisan-fixtures/artisan-28
Drop = should_drop agreement over ticks (F1/precision/recall) + first-drop timing error (s and °C vs the real drop). Heat/Fan = MAE (percentage points) + directional agreement (did the model move the lever the way the human did). Latency = median per phase, FC tightest. NO auto-pick.

Confusion matrices below are derived purely from the per-tick replay data (no extra calls). The 2×2 drop matrix is consistent with the F1/P/R above but is heavily class-imbalanced — almost every tick is no-drop, so TN dominates; read it WITH the drop-timing error, never alone. The 3×3 heat-direction matrix (cut/hold/raise) is the more informative view of control behaviour and anticipatory-cut agreement.

## prompt_version = v2

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.73 dir=0.857; fan MAE=12.5 dir=0.476; latency pre=1.57s preFC=1.13s FC=1.54s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     1 |     0 |     2 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.8 dir=0.875; fan MAE=13 dir=0.417; latency pre=1.17s preFC=1.18s FC=1.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=24; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=12.59 dir=0.846; fan MAE=11.85 dir=0.615; latency pre=1.0s preFC=1.13s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    19 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.846 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=1.9 dir=0.95; fan MAE=7.14 dir=0.6; latency pre=1.41s preFC=1.27s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.95 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.93 dir=0.923; fan MAE=14.44 dir=0.423; latency pre=0.84s preFC=1.13s FC=1.4s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    19 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=15.83 dir=0.783; fan MAE=12.08 dir=0.522; latency pre=1.03s preFC=1.22s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=4.76 dir=0.9; fan MAE=16.67 dir=0.35; latency pre=1.42s preFC=1.22s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=2.86 dir=0.95; fan MAE=7.62 dir=0.6; latency pre=1.25s preFC=1.17s FC=1.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.95 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=2.86 dir=0.9; fan MAE=13.1 dir=0.4; latency pre=0.78s preFC=1.12s FC=1.07s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.7 dir=0.818; fan MAE=16.09 dir=0.318; latency pre=1.28s preFC=1.25s FC=1.54s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=22     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.818 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=6.82 dir=0.952; fan MAE=5.45 dir=0.619; latency pre=1.24s preFC=1.05s FC=1.06s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.952 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.4 dir=0.833; fan MAE=10.8 dir=0.583; latency pre=0.99s preFC=1.29s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    16 |     2 |
    |        raise |     0 |     0 |     2 |
    (n=24; diagonal agreement=0.833 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=5 dir=0.905; fan MAE=18.18 dir=0.286; latency pre=1.18s preFC=1.24s FC=1.24s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=5.45 dir=0.857; fan MAE=16.14 dir=0.429; latency pre=1.08s preFC=1.21s FC=1.44s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=17.5 dir=0.783; fan MAE=15.62 dir=0.391; latency pre=0.94s preFC=1.23s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    15 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.2 dir=0.917; fan MAE=5.2 dir=0.75; latency pre=0.94s preFC=1.2s FC=1.38s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    18 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.57 dir=0.85; fan MAE=8.33 dir=0.6; latency pre=1.33s preFC=1.14s FC=1.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.77 dir=0.88; fan MAE=4.62 dir=0.88; latency pre=0.97s preFC=1.17s FC=1.4s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.69 dir=0.84; fan MAE=18.65 dir=0.28; latency pre=3.11s preFC=1.2s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     4 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/+0.0°C; heat MAE=10 dir=0.913; fan MAE=5.42 dir=0.652; latency pre=0.92s preFC=1.17s FC=1.52s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    16 |     0 |
    |        raise |     1 |     0 |     2 |
    (n=23; diagonal agreement=0.913 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=10 dir=0.84; fan MAE=13.46 dir=0.48; latency pre=1.05s preFC=1.28s FC=1.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    18 |     1 |
    |        raise |     0 |     0 |     2 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.46 dir=0.92; fan MAE=10.38 dir=0.52; latency pre=0.95s preFC=1.2s FC=1.52s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=7.42 dir=0.933; fan MAE=4.52 dir=0.767; latency pre=1.0s preFC=1.1s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    25 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.933 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.25 dir=0.913; fan MAE=11.88 dir=0.478; latency pre=1.13s preFC=1.12s FC=1.25s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    18 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=23; diagonal agreement=0.913 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.21 dir=0.963; fan MAE=9.82 dir=0.593; latency pre=1.04s preFC=1.11s FC=1.32s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     0 |    22 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.963 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=17.5 dir=0.778; fan MAE=13.93 dir=0.407; latency pre=1.01s preFC=1.35s FC=1.03s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    19 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.778 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.33 dir=0.87; fan MAE=18.33 dir=0.391; latency pre=1.44s preFC=1.13s FC=1.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=1.85 dir=1.0; fan MAE=5.93 dir=0.692; latency pre=0.94s preFC=1.24s FC=1.23s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     0 |    22 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=1.0 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123°C, which is below the target charge range of 180°C-200°C. I am maintaining heat at 0% and fan at 0% to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 131C. With 100% heat and 0% fan, the RoR is healthy (14.6 C/min). I am maintaining full heat to ensure momentum toward first crack, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the drying/Maillard transition.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just occurred (7 seconds ago). The RoR is currently 11 C/min, which is healthy but needs to be managed to ensure a controlled development phase. I am reducing heat to 60% to prevent a spike and increasing fan to 60% to shift toward convective heat transfer, ensuring even development and smoke evacuation. This will help stretch the development time toward the 20% target.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=80% fan=60% drop=False conf=0.9 — 'Development time is 67s out of 561s total (11.9%). To reach the 20.5% target, I need ~115s total development time (approx 48s more). RoR is 8C/min, which is healthy. I will maintain heat and fan to ensure a steady, controlled finish without stalling, aiming to drop in about 45-50 seconds.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 143°C and rising toward the target charge range (180°C-200°C). No heat or fan is required yet as the drum continues to soak.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the drying/early Maillard phase. With an RoR of 12 C/min and no fan, the environment is building heat well. I will maintain full heat to ensure momentum toward first crack, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=90% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR. Maintaining current settings to ensure momentum into the development phase without stalling. Will prepare to adjust heat and fan once first crack is confirmed.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=60% fan=80% drop=False conf=0.95 — 'Current development time is 86s out of 654s total (13.1%). To reach the 17.9% target, I need ~31s more development. The RoR is stable at 7.3C/min. I am reducing heat to 60% to prevent a spike and increasing fan to 80% to shift toward convective heat and ensure clean development as we approach the drop. I will hold for another 30 seconds before re-evaluating the drop.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-14.3 C/min) despite 100% heat. This indicates a stall or a measurement error, but given the current bean temperature (97 C), the roast is in danger of stalling. I am maintaining 100% heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further heat loss.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 131°C with no airflow. To prepare for the approach to first crack and ensure even heat distribution, I am increasing the fan to 25% to introduce convective heat transfer while maintaining full power to keep the RoR healthy as we approach the drying/Maillard transition.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=60% drop=False conf=0.9 — 'First crack occurred 17 seconds ago. The RoR is currently 10 C/min, which is healthy but needs to be managed to ensure a smooth development phase. I am reducing heat to 70% and increasing fan to 60% to transition toward convective heat transfer and prevent a spike in RoR, aiming for a controlled development duration of approximately 100-110 seconds total.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=60% fan=90% drop=False conf=0.9 — 'Development is at 12.4% (77s / 619s total). With RoR at 8.6 C/min and temp at 183.3 C, we have room to develop further toward the 19% target. The recent spike to 100% heat is aggressive; I am pulling heat back to 60% to maintain a gentle, controlled glide through the final development phase without stalling, while keeping fan high for convective consistency.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C). It is ready to be charged.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'RoR is healthy (13.5 C/min) and the bean temperature is approaching the transition to the Maillard phase. I am maintaining full heat to ensure momentum through the end of drying/early Maillard, but increasing fan to 30% to begin clearing moisture and preparing for the convective heat transfer needed as we approach first crack.'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 11C/min. Heat is currently at 100% and fan is very low (10%). To prepare for the exothermic reaction of first crack and ensure a clean development phase, I am increasing the fan to 30% to improve convective heat transfer and prepare for smoke evacuation, while maintaining heat to keep momentum until crack begins.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.9 — 'Development time is 52s out of 502s total (10.3%). To reach the 15.7% target, I need ~27s more development. RoR is 10C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop in about 30 seconds.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156°C and rising toward the target charge range (180°C-200°C). No heat or fan is needed yet as the drum continues to soak.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is still well before first crack. With bean temperature at 134.7C and a positive RoR, I am maintaining full heat to ensure sufficient momentum into the Maillard phase. Fan remains at 0% to maximize conductive heat transfer during this stage.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just started (5 seconds ago). The RoR is currently 9.7 C/min, which is healthy but needs to be managed to ensure a controlled development phase. I am reducing heat to 70% to prevent a spike and increasing fan to 50% to shift toward convective heat and clear smoke, ensuring a smooth development period toward the 19.5% target.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=100% fan=70% drop=False conf=0.9 — "The roast is currently at 13.4% development (95s / 711s). To reach the 19.5% target, I need approximately 45-50 more seconds of development. The RoR is low (6.0 C/min) and stable. I will maintain heat to ensure the roast doesn't stall while increasing fan to 70% to improve convective heat transfer and ensure clean development as we approach the drop. I will monitor closely to drop in about 45-50 seconds."
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The telemetry shows a significant drop in bean temperature, likely due to a sensor reading anomaly or a very rapid initial charge response. Since the heat is already at 100% and fan at 0%, I will maintain maximum heat to recover momentum and keep the fan off to ensure conductive heat transfer during this early stage.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 134°C. With heat at 100% and fan at 0%, the RoR is healthy (14°C/min). I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for smoke evacuation as we approach the Maillard phase and eventually first crack. Heat remains at 100% to maintain momentum.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack occurred 10 seconds ago. The RoR is currently 10 C/min, which is healthy but needs to be managed to ensure a controlled development phase. I am reducing heat to 70% to prevent a spike and increasing fan to 50% to shift toward convective heat and manage the development duration. The goal is to reach the target development ratio without stalling the roast.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=60% fan=70% drop=False conf=0.9 — 'Development time is 70s out of 581s total (12%). The RoR is stable at 7.3 C/min. To reach the 20% development target, I need about 75-80 more seconds of development. I am reducing heat to 60% to prevent a spike and increasing fan to 70% to shift toward convective heat and ensure clean development as we approach the drop.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase, waiting for the charge. The current bean temperature is 188°C, which is within the target charge range (180-200°C). I am setting heat and fan to 0 as the machine is ready to receive the beans.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With an RoR of ~11.8 C/min and no fan usage yet, it is time to introduce airflow to begin transitioning toward convective heat transfer and to prepare for the smoke generated during first crack. Maintaining full heat for now to keep momentum, but introducing 25% fan to improve heat distribution.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just occurred. The RoR is healthy (10 C/min) but heat is at 100% with no fan. To manage the development phase and prevent a spike, I am cutting heat significantly and introducing airflow to shift toward convective heat transfer, ensuring a controlled development duration.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.9 — "Development is at 12.2% (65s / 532s). To reach the 17.2% target, I need ~26 more seconds of development. RoR is healthy at 9.7 C/min. I will maintain current settings to allow a steady, controlled finish toward the target development time, avoiding a crash while ensuring we don't overshoot the roast profile."
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature is dropping rapidly in the telemetry, likely due to a sensor artifact or a very early charge, but the heat is at 100% and fan is at 0%. I will maintain maximum heat to drive the roast forward and keep the fan off to build momentum for the Maillard phase.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has dropped significantly (12 C/min) and the bean temperature is stalling at 143 C despite 100% heat. The environment temperature is also plateauing. I need to maintain heat to push through the end of the drying/Maillard phase toward first crack, but I will increase the fan slightly to improve convective efficiency and ensure the bean mass is ready for the upcoming exothermic reaction.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 482s. Current development time is 29s (511-482), which is ~5.7% of total time. To reach the 16.6% target (approx 96s total development), I need to extend development by about 67 more seconds. RoR is stable at 10C/min. I am reducing heat to 60% and increasing fan to 60% to manage the momentum, shift toward convective heat, and ensure a smooth, controlled development without stalling.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=50% fan=70% drop=False conf=0.9 — 'Total roast time is 530s, development time is 48s (9% ratio). To reach the 16.6% target (approx. 105s development), I need about 57 more seconds of development. RoR is 10.6 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 193-195 C.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183°C). It is ready to be charged. No heat or fan is currently applied.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily but the RoR is beginning to decline. At 143°C, we are approaching the mid-roast transition. I am maintaining full heat to ensure we don't stall before first crack, but I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke that will accompany first crack."
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 10.3 C/min. Maintaining full heat to ensure momentum into first crack, while keeping fan at 30% to preserve drum heat. Will prepare to reduce heat and increase fan once first crack is confirmed to manage the exothermic reaction.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.9 — 'Current development time is 52s out of 533s total (9.7%). To reach the 15.3% target, I need approximately 30 more seconds of development. The RoR is stable at 10C/min. I will reduce heat to 60% to gently manage the momentum and increase fan to 70% to shift toward convective heat for the final stretch, ensuring a clean finish without stalling. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (110C), but the bean temperature is dropping rapidly despite 100% heat. This indicates a potential sensor error or a significant stall. I am maintaining 100% heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 129C. With heat at 100% and fan at 0%, the RoR is healthy (13.77 C/min). I will maintain full heat to continue the momentum toward first crack, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 481s. Development is very early (14s). RoR is 10.3 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% and increasing fan to 60% to transition to convective heat, ensuring a controlled development phase that hits the target duration without stalling.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=60% fan=50% drop=False conf=0.9 — 'The roast is currently at 17.7% development (104s / 585s). The RoR is stable at 9C/min, and the bean temperature is 185C. To reach the target development of ~20-24% without overshooting the temperature or stalling the roast, I need to reduce heat to manage the momentum while increasing fan to shift toward convective heat for a clean finish. Dropping heat to 60% and increasing fan to 50% will gently taper the RoR as we approach the drop. I will aim to drop in approximately 30-45 seconds.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing extremely rapidly (151C at 12 seconds is physically impossible for a standard Hottop charge, suggesting a sensor error or a very unusual start). Given the current telemetry shows a sharp drop in bean temperature despite 100% heat, I am maintaining maximum heat to stabilize the drum and ensure the beans are actually absorbing energy. I will keep the fan at 0% to maximize conductive heat transfer until the bean temperature stabilizes and begins a consistent upward trend.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 142°C with a healthy RoR of 12.3°C/min. We are approaching the Maillard phase. I am maintaining full heat to ensure momentum into first crack while slightly increasing the fan to 20% to begin preparing for better convective heat transfer and smoke management as we approach the crack.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=50% fan=80% drop=False conf=0.9 — 'First crack occurred at 511s. With 13s of development, we are very early. The RoR is 11 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 50% to moderate the momentum and setting fan to 80% to ensure convective heat transfer and smoke evacuation. This balance will allow for a controlled development toward the 14% target (approx 73s development time).'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=60% fan=75% drop=False conf=0.9 — 'Current development time is 43s out of 554s total (7.7%). To reach a 14% development ratio, I need approximately 35-40 more seconds of development. The RoR is 10.3 C/min, which is healthy but needs to be managed to avoid overshooting the target temperature too quickly. Reducing heat to 60% and increasing fan to 75% will gently taper the RoR while ensuring convective heat maintains development.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roast is approaching first crack with significant momentum (RoR 18.3 C/min). Heat is currently off, which is appropriate to prevent a spike. I am maintaining fan at 0% to preserve thermal energy for the transition into first crack, but I am prepared to increase airflow immediately once first crack begins to manage smoke and transition to convective heat. No drop yet.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR is healthy (12.3 C/min) and the roast is progressing well toward first crack. I am maintaining full heat to ensure sufficient momentum for the upcoming endothermic phase, while keeping fan low to maximize conductive heat transfer through the drum. No changes needed yet.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just occurred. The RoR is currently 11 C/min, which is healthy but needs to be managed to ensure a smooth development phase. I am cutting heat significantly to prevent a spike and increasing fan to 50% to shift toward convective heat, ensuring even development and smoke evacuation. This will help stretch the development time to reach the 14.4% target without overshooting the temperature too quickly.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=30% fan=70% drop=False conf=0.9 — 'Development is at 36s (approx 7% of total time). To reach the 14.4% target, I need about 38 more seconds of development. RoR is stable at 10C/min. I will reduce heat to 30% to gently coast toward the finish and increase fan to 70% to enhance convective heat and clear smoke, ensuring a clean finish without stalling. Drop when development time reaches ~75s.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (143C to 127C in 5 seconds), which is highly abnormal and suggests a sensor error or a massive stall. Given the heat is at 100% and the environment temperature is also dropping, I need to maintain maximum heat to recover the momentum. I will keep heat at 100% and fan at 0% to minimize convective loss while the drum recovers.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily toward first crack. With an RoR of 12.7 C/min and bean temperature at 140 C, we are in the late drying/early Maillard phase. Maintaining full heat is appropriate to ensure sufficient momentum for the upcoming crack, but I am introducing 20% fan now to begin clearing moisture and preparing for the convective phase, ensuring the environment doesn't become too stagnant."
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just occurred. The RoR is currently 10C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to moderate the energy input and increasing fan to 50% to transition to convective heat, which will help manage the development phase and ensure even roasting. I am aiming for a controlled, steady development over the next 90-120 seconds.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=50% fan=70% drop=False conf=0.9 — 'Development time is 63s out of 559s total (11.3%). To reach the 19.9% target, I need ~111s total development time, meaning ~48s more. The RoR is stable at 8.7C/min. I will reduce heat to 50% to gently manage the final climb and increase fan to 70% to improve convective efficiency and clear smoke as we approach the drop. This should allow for a controlled, steady finish.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (174C is close to the 180-200C guidance). I am setting heat to 0 and fan to 0 to stabilize before the charge. Once the beans are added, I will initiate the roast.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has dropped significantly to 11.7 C/min, indicating the roast is losing momentum as it approaches first crack. I need to maintain heat to ensure we reach first crack without stalling, but I will introduce moderate airflow (25%) to begin transitioning toward convective heat transfer and prepare for the smoke/chaff management needed during development.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=40% fan=60% drop=False conf=0.9 — 'First crack occurred 8 seconds ago. The RoR is currently 10 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am cutting heat significantly to 40% to prevent overshooting the target temperature too quickly, and increasing fan to 60% to transition to convective heat, which will help manage the development phase and clear smoke. Development is just beginning; I will aim for a total development time of ~90 seconds.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=30% fan=100% drop=False conf=0.9 — 'Development time is 69s out of 563s total (12.2%). To reach the 17.7% target, I need ~30 more seconds of development. RoR is 9C/min, which is healthy. I will maintain current heat and fan to ensure a steady, controlled finish without stalling, aiming to drop around 194-195C in about 30-40 seconds.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=60% fan=20% drop=False conf=0.9 — 'The roast has just begun (1 minute in) and the heat is currently off. To establish a steady drying phase and build momentum toward first crack, I am applying moderate heat and low airflow to manage the initial moisture release.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 130°C. With an RoR of 14°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just begun. The current heat (100%) is too high for the development phase and will cause the RoR to spike, leading to an uneven roast or tipping. I am cutting heat significantly to 40% to manage the momentum and increasing fan to 50% to shift toward convective heat transfer, ensuring a controlled development period.'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'Development is currently at ~15% (92s / 604s). To reach the 22% target, we need approximately 40-50 more seconds of development. The RoR is stable at 8C/min. I am cutting heat significantly to prevent a temperature spike while increasing fan to 60% to shift toward convective heat, ensuring even development without stalling the roast. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The user has not yet charged the beans. I am setting heat to 100% to ensure the drum reaches the target charge temperature (180-200C) efficiently. Fan is kept at 0% to allow the drum to heat up without unnecessary heat loss.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'RoR is healthy (12.3 C/min) and the bean temperature is approaching the transition to first crack. Maintaining full heat is appropriate to ensure sufficient momentum through the end of the drying/Maillard phases, while keeping fan low (10%) preserves conductive heat for the drum. No changes needed yet.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack just started. RoR is 9C/min, which is healthy. I need to reduce heat and increase fan to manage the development phase and prevent a runaway RoR. Dropping heat to 60% and increasing fan to 60% will shift the roast toward convective heat and ensure a controlled development duration.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=60% fan=80% drop=False conf=0.95 — 'Total roast time is ~659s, development time is 63s (9.5% ratio). To reach the 13.4% target, I need ~25 more seconds of development. RoR is 11C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 193-194C in about 30 seconds.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 176°C, which is approaching the charge guidance range (180-200°C). I am setting heat and fan to 0 to allow the roaster to stabilize at the target charge temperature before the beans are added.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With an RoR of 12.7 C/min and bean temp at 138.7 C, we are in a good position. I am maintaining full heat to ensure sufficient momentum for the upcoming endothermic phase, while slightly increasing the fan to 20% to begin preparing for better airflow management as we approach first crack.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack occurred 12 seconds ago. The RoR is currently 10C/min, which is healthy but needs to be managed to avoid overshooting the development target. I am reducing heat to 70% and increasing fan to 50% to transition toward convective heat and gently taper the RoR for a controlled development phase.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=50% fan=80% drop=False conf=0.9 — 'The current development ratio is ~19% (102s / 534s). With a RoR of 10.7 C/min, the roast is progressing well. To reach the target development ratio of 25.5% (approx. 136s total development time), I need about 34 more seconds of development. I am reducing heat to 50% to manage the momentum and increasing fan to 80% to shift toward convective heat and ensure clean development as we approach the drop.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156°C and rising toward the target charge range (180-200°C). No heat or fan is required yet as the machine continues to soak and stabilize.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'The RoR has dropped significantly to 11.3 C/min while at maximum heat and fan. The roast is stalling before reaching first crack. I need to maintain maximum heat to push through the end of the drying/Maillard phase and reach first crack, as the current energy input is insufficient to maintain momentum.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just begun. The RoR is healthy (10 C/min), but heat is at 100%. To manage the development phase and avoid a runaway roast, I am reducing heat to 70% and increasing fan to 50% to shift toward convective heat and ensure steady, controlled development toward the 13.6% target.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=60% fan=90% drop=False conf=0.9 — 'Development time is 62s out of 684s total (9.1%). To reach the 13.6% target, I need ~31s more development. RoR is 9.7 C/min, which is healthy. I will reduce heat to 60% to manage the momentum and increase fan to 90% to ensure convective airflow for the final stretch, aiming to drop in about 30-40 seconds.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early drying phase. The bean temperature drop (99C to 90C) is likely a sensor artifact or stabilization after charge. Heat is at maximum to build momentum for the drying phase. Fan remains at 0% to retain heat and moisture during the initial soak. No changes needed yet.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The bean RoR has dropped significantly to 10.7 C/min, and the bean temperature has stalled at 137 C despite 100% heat. The roast is losing momentum before reaching first crack. I am increasing fan to 25% to improve airflow and convective efficiency, while maintaining 100% heat to push through the stall and regain momentum toward first crack.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=50% drop=False conf=0.9 — 'Approaching first crack with high heat and no fan. Need to prepare for the exothermic reaction by increasing airflow to manage smoke and transition to convective heat, while keeping heat high for now to maintain momentum until crack begins. Will increase fan to 50% to clear the drum and prepare for the upcoming crack.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.9 — 'Current development ratio is 7.9% (54s / 680s). To reach the 12.4% target, I need ~30 more seconds of development. RoR is stable at 10C/min. I am cutting heat to 60% and increasing fan to 60% to manage the momentum and ensure a gentle finish without stalling, aiming to drop around 193-194C in about 30-40 seconds.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (from 108C to 97C in 5 seconds), indicating a potential sensor error or a severe stall. Given the heat is already at 100%, I will maintain maximum heat to recover momentum and keep the fan low to minimize convective cooling while the bean temperature is this low.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With a bean RoR of 13.3 C/min and the bean temperature at 139 C, we are in a good position. I am maintaining full heat to ensure momentum through the drying/Maillard transition, while keeping the fan low (10%) to preserve drum heat and build the necessary energy for a strong first crack. No changes are needed yet.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 522s. Development is very early (6s). RoR is currently stable at ~9.8 C/min. To achieve a ~19-20% development ratio, I need to extend the development phase to roughly 100-110 seconds total. I am maintaining moderate heat and increased fan to transition to convective heat, ensuring a controlled, steady development without stalling the RoR.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=30% fan=100% drop=False conf=0.95 — 'Development time is 66s out of 588s total (11.2%). To reach the 19.6% target (approx 115s total development), I need about 50 more seconds of development. RoR is 9C/min, which is steady. I will reduce heat to 30% to gently coast toward the finish and increase fan to 100% to maximize convective airflow for the final stage, ensuring a clean finish without stalling. Drop when development time reaches ~115s.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR is negative and dropping rapidly, indicating a stall or measurement error immediately following charge. Heat must remain at maximum to recover momentum. Fan is kept at 0 to maximize conductive heat transfer during this early drying phase.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 135°C. With heat at 100% and fan at 0%, I need to introduce airflow to manage the environment and prepare for the upcoming transition to first crack. Increasing the fan to 25% will improve convective heat transfer and clear moisture/chaff, while maintaining 100% heat to keep the RoR healthy as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just begun (3 seconds in). The RoR is healthy at 10C/min. To manage the development phase and avoid a runaway roast, I am reducing heat to 60% and increasing fan to 50% to transition toward convective heat transfer and maintain a controlled, steady development. This will help stretch the development time to reach the 16.5% target ratio.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=60% fan=90% drop=False conf=0.95 — 'Development time is 63s out of 652s total (9.6%). To reach the 16.5% target, I need ~128s total development time, meaning ~65s more. RoR is 9.0 C/min, which is healthy. I will maintain current settings to allow a steady, controlled climb toward the drop temperature without stalling, as the current heat/fan balance is effectively managing the post-crack momentum.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. I am setting the heat to 100% to reach the target charge temperature (180-200°C) efficiently. Fan is kept at 0% to allow the drum to heat up effectively.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has flattened significantly (11 C/min and stalling at 137 C). The current heat/fan settings are insufficient to maintain momentum toward first crack. I am increasing heat to 100% (already there) and increasing fan to 30% to improve airflow and heat distribution, while monitoring for a recovery in RoR. If the RoR does not pick up, the roast may be stalling.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just occurred (3 seconds ago). The RoR is currently 10.7 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to begin the glide toward the drop and increasing fan to 40% to shift toward convective heat and clear smoke, ensuring a controlled development phase.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=60% fan=80% drop=False conf=0.9 — 'Development time is 63s out of 683s total (9.2%). To reach the 14.6% target, I need ~37s more development. RoR is 9.3 C/min, which is healthy. I will maintain current heat/fan to allow a steady, controlled finish without stalling, aiming to drop around 196-197C in about 30-40 seconds.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly despite 100% heat. This indicates a stall or a measurement error, but given the current low bean temperature (97C), the roast is in danger of stalling. I am maintaining 100% heat to recover momentum and keeping fan low to minimize convective heat loss until the bean temperature stabilizes and begins to climb again.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 135°C. With an RoR of 10.3°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but increasing the fan slightly to 20% to begin preparing for better airflow management as we approach the end of the drying/Maillard phase.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=60% fan=50% drop=False conf=0.95 — 'First crack has just begun. The RoR is healthy (11.3 C/min) but the heat is at 100%. To manage the development phase and prevent a runaway RoR, I am reducing heat to 60% and increasing fan to 50% to transition to convective heat and ensure even development. This will help stretch the development time toward the 12.7% target.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=50% fan=100% drop=False conf=0.9 — 'Current development time is 64s out of 676s total (9.5%). To reach the 12.7% target, I need approximately 22-25 more seconds of development. The RoR is 13 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 194-195 C in about 25 seconds.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'Preheating phase. The roaster is currently at 99C bean temp (likely residual heat from a previous roast or sensor calibration). I am setting heat to 100% to reach the target charge temperature range (180-200C) efficiently. Fan remains off to allow the drum to soak heat.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early stages (125C). Heat is at 100% and fan is at 0%. To prepare for the upcoming Maillard phase and ensure consistent heat transfer, I am introducing a low level of airflow (25%) to begin clearing moisture and transitioning toward convective heat, while maintaining full heat to keep the RoR healthy as we approach the mid-roast.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 12C/min. Maintaining current settings to ensure momentum into first crack. Will prepare to reduce heat and increase fan once first crack is confirmed to manage the exothermic reaction.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=90% fan=50% drop=False conf=0.9 — 'The roast is currently at 14.3% development (85s dev / 595s total). The target is 24.1%, which is quite high for a standard roast, but I need to extend development. The RoR is 12 C/min, which is healthy. I will maintain heat to ensure we reach the target development time without stalling, while increasing fan to 50% to improve convective heat transfer and manage the roast profile as we approach the end.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is approaching the target charge range (180-200C). I am setting heat to 0 and fan to 0 to allow the drum to stabilize at the desired charge temperature before the beans are added.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has flattened significantly (9.3 C/min) while at 100% heat and 0% fan. To maintain momentum toward first crack without stalling, I need to introduce airflow to improve heat transfer efficiency and prevent potential scorching as the beans darken. Increasing fan to 25% while maintaining heat will help push the roast forward.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.9 — 'First crack occurred at 637s. Total roast time is 656s, with 19s of development (approx 2.9%). To reach a 14% development ratio, I need a total roast time of ~135s post-crack (approx 772s total). I have plenty of time to coast. RoR is 11.7 C/min, which is healthy. I am reducing heat further to 50% to ensure a gentle, controlled finish and increasing fan to 60% to shift toward convective heat and clear smoke as development progresses. Not ready to drop yet.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=0% fan=60% drop=False conf=0.9 — 'Current development time is 49s out of 686s total (7.1%). To reach a 14% development ratio, I need a total roast time of ~350s (which is impossible) or, more realistically, to extend development to ~110s total. With RoR at 12.7 C/min and temp at 189 C, I have room to coast. Dropping heat to 0% and maintaining fan at 60% will gently glide the roast toward the target development duration without stalling.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-7.37 C/min) despite 100% heat. This indicates a potential sensor error or a severe stall. Given the environment temperature is stable at 167C, I am maintaining maximum heat to recover momentum and keeping fan low to minimize convective cooling while the bean temperature is still low.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With an RoR of 13.3 C/min and bean temp at 134 C, we are in a good position. I am maintaining full heat to ensure sufficient momentum toward first crack, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.95 — 'First crack has just begun. The RoR is healthy (11 C/min), but heat is at 100% with no fan, which will lead to a spike and potential scorching. I am cutting heat significantly and introducing airflow to transition to convective heat, ensuring a controlled development phase to reach the 20% development ratio target.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.9 — 'Development time is 66s out of 583s total (11.3%). RoR is 10C/min, which is healthy but needs to be managed to reach the 20% development target without overshooting the drop temperature. Reducing heat to 60% and increasing fan to 60% will shift toward convective heat, slowing the RoR gently to allow for a longer development phase.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (drying/yellowing). The bean temperature is dropping rapidly despite 100% heat, likely due to a sensor anomaly or a very cold charge. Maintaining 100% heat is necessary to recover momentum and drive the roast forward. Fan remains at 0% to maximize conductive heat transfer during this initial phase.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 132C with 100% heat and 0% fan. To prepare for the approach to first crack and ensure even heat distribution, I am introducing 25% fan to shift toward convective heat transfer while maintaining high heat to keep the RoR healthy as we approach the drying/Maillard transition.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just started (4 seconds ago). The RoR is healthy at 10.7 C/min. To manage the development phase and prevent a runaway roast, I need to reduce heat and introduce airflow to transition to convective heat transfer. Dropping heat to 60% and increasing fan to 50% will stabilize the RoR and ensure even development. I am far from the target drop temperature and development time, so I will continue roasting.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.9 — 'Current development time is 64s out of 621s total (10.3%). To reach the 17.8% target, I need approximately 134s total development time (70s more). The RoR is 9.0 C/min, which is healthy. I will reduce heat to 60% to prevent a runaway finish and increase fan to 70% to shift toward convective heat, ensuring a smooth, controlled development to the target duration. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-16 C/min) despite 100% heat. This indicates a stall or a measurement error, but given the low bean temperature (86 C) and high environment temperature (158 C), the bean probe is likely not reading correctly or the beans are not absorbing heat. I am maintaining 100% heat to recover momentum and keeping fan low to minimize convective cooling while the beans are still in the drying/early yellowing phase.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'RoR is healthy (10.3 C/min) and the bean temperature is approaching the transition to first crack. I am maintaining full heat to ensure momentum through the end of the drying/Maillard phase, but increasing fan to 40% to begin transitioning toward convective heat transfer and preparing for the smoke/chaff management needed during first crack.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=60% fan=70% drop=False conf=0.9 — 'First crack occurred at 638s. Development is very early (12s). RoR is healthy at 9.7 C/min. To manage the development phase and ensure a smooth finish toward the 13.6% target, I am reducing heat to prevent a spike and increasing fan to 70% to transition to convective heat, ensuring even development without stalling.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'Development time is 72s out of 710s total (10.1%). To reach the 13.6% target, I need ~25 more seconds of development. RoR is 10.3 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 196-197 C in about 30 seconds.'

### openai/gpt-5.4-nano (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.05 dir=0.333; fan MAE=19.36 dir=0.286; latency pre=3.29s preFC=1.4s FC=1.52s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    12 |     5 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.333 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=23.12 dir=0.375; fan MAE=22.84 dir=0.125; latency pre=2.31s preFC=1.38s FC=1.78s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    14 |     6 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.375 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.93 dir=0.385; fan MAE=21.22 dir=0.231; latency pre=1.42s preFC=1.42s FC=1.51s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    12 |     7 |     3 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.385 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=16.67 dir=0.4; fan MAE=11.24 dir=0.2; latency pre=1.33s preFC=1.37s FC=1.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    11 |     4 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.4 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=13.81 dir=0.5; fan MAE=24.7 dir=0.115; latency pre=1.35s preFC=1.41s FC=1.55s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    12 |     9 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.5 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=24.5 dir=0.261; fan MAE=22.04 dir=0.217; latency pre=1.25s preFC=1.51s FC=1.57s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    16 |     4 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.261 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.95 dir=0.35; fan MAE=32.67 dir=0.1; latency pre=1.37s preFC=1.5s FC=1.38s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     4 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.35 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=13.14 dir=0.35; fan MAE=13.67 dir=0.25; latency pre=2.62s preFC=1.43s FC=1.34s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    13 |     3 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.35 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15.29 dir=0.35; fan MAE=20.62 dir=0.2; latency pre=1.32s preFC=1.38s FC=1.5s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     4 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.35 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=22.09 dir=0.273; fan MAE=23.17 dir=0.136; latency pre=2.31s preFC=1.35s FC=1.46s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=22     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    16 |     3 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.273 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=11.95 dir=0.429; fan MAE=16.18 dir=0.238; latency pre=1.25s preFC=1.38s FC=1.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    12 |     6 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.429 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.44 dir=0.292; fan MAE=22.44 dir=0.167; latency pre=1.29s preFC=1.5s FC=1.35s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    12 |     3 |     5 |
    |        raise |     0 |     0 |     2 |
    (n=24; diagonal agreement=0.292 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=16.5 dir=0.476; fan MAE=33.73 dir=0.095; latency pre=1.22s preFC=1.56s FC=1.62s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    10 |     6 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.476 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.05 dir=0.381; fan MAE=29.09 dir=0.095; latency pre=1.51s preFC=1.37s FC=1.56s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     4 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=21; diagonal agreement=0.381 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=27.42 dir=0.217; fan MAE=26.88 dir=0.13; latency pre=1.17s preFC=1.4s FC=1.53s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    17 |     2 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.217 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.12 dir=0.25; fan MAE=16.44 dir=0.167; latency pre=1.23s preFC=1.45s FC=1.52s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    17 |     2 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.25 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.38 dir=0.35; fan MAE=15.33 dir=0.25; latency pre=1.09s preFC=1.39s FC=1.54s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    13 |     3 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.35 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15.96 dir=0.32; fan MAE=12.81 dir=0.44; latency pre=1.6s preFC=1.43s FC=1.62s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    17 |     6 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.32 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.38 dir=0.4; fan MAE=31.04 dir=0.08; latency pre=1.23s preFC=1.39s FC=1.4s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |    15 |     9 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.4 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.5 dir=0.391; fan MAE=11.25 dir=0.261; latency pre=1.74s preFC=1.44s FC=1.58s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    12 |     5 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=23; diagonal agreement=0.391 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.5 dir=0.32; fan MAE=24.65 dir=0.16; latency pre=1.33s preFC=1.44s FC=1.68s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    15 |     6 |     1 |
    |        raise |     0 |     1 |     1 |
    (n=25; diagonal agreement=0.32 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=13.81 dir=0.4; fan MAE=16.85 dir=0.28; latency pre=1.24s preFC=1.42s FC=1.54s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    15 |     5 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=25; diagonal agreement=0.4 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=25.48 dir=0.233; fan MAE=14.68 dir=0.167; latency pre=1.25s preFC=1.4s FC=1.48s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    17 |     3 |     6 |
    |        raise |     0 |     0 |     2 |
    (n=30; diagonal agreement=0.233 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.58 dir=0.435; fan MAE=25 dir=0.087; latency pre=1.43s preFC=1.48s FC=1.57s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     6 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=23; diagonal agreement=0.435 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=24.14 dir=0.222; fan MAE=24.36 dir=0.222; latency pre=1.34s preFC=1.44s FC=1.58s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    19 |     1 |     2 |
    |        raise |     0 |     0 |     2 |
    (n=27; diagonal agreement=0.222 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=28.96 dir=0.259; fan MAE=25.57 dir=0.111; latency pre=1.54s preFC=1.47s FC=1.55s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    15 |     5 |     4 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.259 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.04 dir=0.435; fan MAE=30.71 dir=0.087; latency pre=1.38s preFC=1.46s FC=1.59s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     7 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.435 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=23 dir=0.192; fan MAE=10.56 dir=0.231; latency pre=1.35s preFC=1.43s FC=1.51s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    19 |     2 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.192 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=15% fan=35% drop=False conf=0.62 — 'Preheating: no charge/zero timeline yet (elapsed negative) and no RoR/first crack. Use modest heat with moderate fan to stabilize airflow and protect flavor while bringing bean temp toward charge guidance (180–200°C) without overshooting. No drop criteria met.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=85% fan=10% drop=False conf=0.62 — 'Pre–first crack: bean temp is only ~131°C while env is ~189°C and RoR is fairly high (14.6°C/min), so avoid overshooting with full heat; reduce heat moderately and add slight fan to stabilize airflow/smoke control without shifting too aggressively before first crack.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=55% fan=65% drop=False conf=0.72 — 'Post-crack development is only ~7s out of 501s (~1–2%), far below the ~10–20% duration goal (target ~20.5%). With RoR high (11°C/min) and bean temp ~173°C, keep development going but coordinate levers: cut heat decisively to avoid overshooting due to thermal lag, and raise fan toward convective transfer to control RoR and prevent baking while stabilizing flavor. Drop is not yet warranted by development ratio; aim to extend duration rather than chase the 189°C guide.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=55% fan=78% drop=False conf=0.73 — 'Post-crack development ratio is ~67/561 = 12%, still within the 10–20% target band and the bean is only ~181°C with RoR 8°C/min (not yet overheating). Keep developing a bit longer but manage thermal lag by reducing heat decisively and increasing fan to keep transfer convective, protect sweetness, and avoid baked flavors as you approach the 20% development duration.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=55% fan=30% drop=False conf=0.46 — 'Preheating: beans are far from charge guidance (121°C vs ~180–200°C), with no first crack yet. Apply moderate heat early while keeping fan moderate to avoid overshooting or flattening drum heat transfer. Plan to build toward charge and only ramp convection strongly once post-crack development timing becomes relevant.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=8% drop=False conf=0.62 — 'Pre–first crack: continue charging steadily. Bean RoR (12 °C/min) is strong and bean temp is still relatively low (135 °C), so keep full heat to overcome thermal lag. Keep fan low/moderate to avoid cooling or shifting transfer mode too early while still providing some air handling; focus is reaching proper first-crack approach rather than duration development.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=85% fan=40% drop=False conf=0.62 — 'Pre–first crack with bean temp ~173°C, RoR high (10.7°C/min) and no crack yet. To prevent overshoot while accounting for thermal lag, slightly reduce heat from current 90% and raise fan to move toward more even convective transfer and improve smoke/chaff removal. Stay below the 190°C guide; do not drop until first crack/target development window is reached.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=45% fan=90% drop=False conf=0.72 — 'Post-crack development ratio is ~86s/654s=13%, a bit short of the 17.9% target, and RoR is still high (7.3°C/min). To extend development without overshooting, cut heat decisively (thermal lag: change will show soon) and raise fan to shift toward more convective, even transfer while protecting flavor. Target drop temp guide (~190°C) is near, but don’t rush the drop—aim to keep developing modestly until duration catches up.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=78% fan=25% drop=False conf=0.74 — 'Pre–first-crack and bean temp is below the usual midrange with strongly negative RoR (cooling), indicating heat input isn’t translating yet. Reduce heat decisively from 100 to avoid overshoot once lag catches up, while adding modest fan to stabilize heat transfer and improve convection/smoke/chaff handling without crashing the system. No drop yet since first crack hasn’t occurred.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=90% fan=25% drop=False conf=0.62 — 'Pre–first-crack: beans at 131°C and RoR is rising (~12°C/min) while env is ~193°C. Keep substantial heat but slightly reduce to anticipate thermal lag and avoid overshooting. Fan low-to-moderate (not zero) to start promoting even convective transfer and smoke/chaff management without crashing RoR. Development target (10–20%) applies after first crack, so no drop now.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=35% fan=70% drop=False conf=0.66 — 'Post-crack development is still young (17s; ~19% of total). Bean temp (~175C) is well below the guide and RoR is high (~10C/min), suggesting energy is accumulating fast while lacking convective smoothing. Cut heat decisively (thermal lag) and increase fan to transition toward more even convective transfer, extending development duration without chasing temp; do not drop yet until development ratio is reached and RoR softens.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=55% fan=100% drop=False conf=0.66 — 'Post-crack development ratio is ~12.4% (77s/619s), slightly under the 19% duration goal, so extend development. Bean RoR is high (~8.6 C/min) and bean temp is already near the guide (183→183.3 C vs target 190 C), indicating heat is oversupplying. Reduce heat decisively for thermal-lag control while pushing fan higher for more convective, even development and smoke/chaff evacuation. Don’t drop yet—aim to hold ~10–20% development range rather than chase 190 C exactly.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=35% fan=15% drop=False conf=0.62 — 'Preheating with bean temp ~183°C and no first crack yet. Use moderate heat with low-to-moderate fan to build drum/bean energy without over-convecting or cooling; aim to reach the charge/development plan smoothly (development ~10–20% later). Fan elevated only enough to manage smoke/chaff and stabilize airflow.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=72% fan=18% drop=False conf=0.62 — 'Pre-first-crack: bean RoR is high (~13.5 C/min) while bean temp is still low (140.7 C). To avoid an overshoot before first crack, reduce heat from current 100% and raise fan moderately to smooth heat transfer and prevent scorching. Development/ drop not yet applicable (first crack not detected; elapsed 262 s).'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=85% fan=18% drop=False conf=0.62 — 'Pre–first crack and bean RoR is moderately high (~11 C/min) with bean temp still well below the 191 C guide; reduce heat from 100% to ~85% to avoid overshoot given thermal lag, and raise fan modestly to increase convective transfer and help stabilize roast progression without crashing RoR. No drop since first crack hasn’t occurred and development ratio target is not yet applicable.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=30% fan=78% drop=False conf=0.74 — 'Development phase: current development ratio ≈52s/502s = 10.4% (slightly under the 10–20% target). Bean RoR is still high (~10°C/min), and bean temp is below the guide (186°C vs 191.3°C), so continue developing but avoid overheating by cutting heat and shifting toward more convective transfer. Set heat down to ~30% and raise fan to ~78% to smooth the roast, protect flavor, and manage smoke/chaff; do not drop yet since duration looks on-target and temps can rise with thermal lag.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=25% drop=False conf=0.62 — 'Preheating only (no charge yet/elapsed negative). Bean temp is already ~125°C while element and fan are at 0, so use modest fan to stabilize airflow/evacuate heat gradients and hold heat low until proper charge timing. Aim to avoid overshoot before charge; no drop in preheating.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.62 — 'Pre–first crack: bean temp is still ~134.7°C with RoR 11.7°C/min and no first crack detected. Recent telemetry shows heat at 100% with fan at 0%, and bean temp is rising only slightly, suggesting inadequate airflow for efficient, even heat transfer. Increase fan moderately to ~30% (more convective transfer and smoke/chaff evacuation) while keeping heat high and decisive to continue climbing toward first crack; do not drop yet since first crack hasn’t occurred.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=65% fan=70% drop=False conf=0.74 — 'Post-first-crack development is far too short (about 5s at ~10.35 min roast elapsed → ~0.8% vs 10–20% goal). Bean is only ~173.7°C and RoR is high (~9.7°C/min), so cut heat substantially to reduce the heat-driven temp rise, while increasing fan to shift toward more convective heat transfer and stabilize flavor without overbaking. Keep extending development; don’t drop yet since the duration target hasn’t been reached.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=35% fan=70% drop=False conf=0.66 — 'Post-crack development ratio is ~13.4% (95s/711s), which is within the desired 10–20% band but slightly short for 19.5% target. Bean RoR 6.0 °C/min suggests heat is still driving warming; since bean temp is still below the 191.7°C guide and you’re not ready to drop yet, cut heat substantially to avoid overshoot (thermal lag) while raising fan toward more convective transfer for even development and smoke/chaff evacuation. Hold on the drop until development ratio reaches target/favor duration over temperature.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.36 — 'Pre–first crack: bean temp ~104°C and still climbing with 100% heat and 0% fan; no first crack yet, so keep heat on to overcome thermal lag. Add modest fan to start shifting toward more even convective transfer and improve smoke/chaff evacuation, without pulling too much heat away. No drop since target development/transition not reached.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=90% fan=35% drop=False conf=0.62 — 'Pre–first crack: bean temp is only ~134°C and first crack not detected; with RoR ~14°C/min (still rising), keep heat high but not max to avoid overshoot, and add moderate fan to stabilize heat transfer and protect from scorching as temperatures build. Drop is not yet warranted since post-crack development timing doesn’t apply and target development temperature is far ahead.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=35% fan=70% drop=False conf=0.66 — 'Post–first crack development is very short (10s of 521s elapsed) and RoR is still high (~10°C/min), so to stretch duration without overshooting darkness: cut drum energy substantially (heat ~35%) and increase fan to move toward convective transfer (~70%), which stabilizes RoR while flushing smoke/chaff. Target drop at ~193°C is a guide; with your duration ratio currently under target, continue development rather than rushing the drop.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=58% fan=72% drop=False conf=0.66 — 'Post-crack development is ~70s out of 581s total (~12%), within the ~10–20% window. Bean temp is still rising slowly (RoR 7.3) and you’re slightly below the guide; to stretch development duration and avoid darkening, cut heat noticeably for thermal lag while raising fan to shift toward more even convective transfer and smoke/chaff evacuation.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=55% fan=20% drop=False conf=0.62 — 'Preheating/charge not yet started; bean temp already in-range (~188°C) and no RoR/first crack. Use moderate heat to avoid overshoot and a low-to-mid fan to keep warm-up stable while ensuring some airflow for uniform transfer and smoke/chaff clearance.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=95% fan=25% drop=False conf=0.62 — 'Pre–first-crack: bean temp ~145.7°C with RoR ~11.8°C/min is rising but still far from the 193°C guide; keep heat near high to advance energy early, while adding modest fan to encourage steadier convective transfer and protect even drying. No drop before first crack.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=35% fan=75% drop=False conf=0.74 — 'Post–first crack development: bean temp is still ~178 C and RoR is high (~10 C/min), so the drum is likely still ramping energy. To hit the ~17% development duration without overshooting into dark roast, cut heat decisively now (accounting for thermal lag) while raising fan toward a more convective transfer mode for even drying/development and smoke/chaff evacuation.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=40% fan=75% drop=False conf=0.66 — 'Development is on track (~65s out of 532s elapsed → ~12% vs target ~17%). Bean RoR is still high (9.7 C/min), suggesting heat is still building faster than desired. To stretch and smooth development without overshooting into dark flavors, cut heat moderately (anticipating thermal lag) and raise fan to shift toward more convective transfer and protect flavor while continuing development.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.62 — 'Pre–first crack at 19s: beans are still ~125°C and RoR is not available; maintain strong heat with only light fan to avoid over-convective cooling and to climb steadily toward first crack. No drop: development timing only applies after first crack.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=75% fan=35% drop=False conf=0.63 — 'Pre–first crack and bean temp is still ~143°C with fast RoR (~12°C/min) while environment is ~201°C. To keep the roast climbing smoothly without overshooting the later guide temperature, reduce heat from 100% to ~75% and raise fan to ~35% to promote more even convective transfer and smoke/chaff evacuation. No drop: first crack not detected and targets are for development timing after it.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=55% fan=75% drop=False conf=0.74 — 'Post–1C development is only ~3.6% (18s/500s) so you’re at risk of finishing too early/too dark. With RoR ~10°C/min, cut heat decisively to avoid overshooting the ~193°C guide, and raise fan to keep transfer more convective and stretch development duration while protecting flavor.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=35% fan=85% drop=False conf=0.68 — 'Development ratio ~48s/530s = 9.1% (below the 16.6% target), while RoR is high (bean 10.6°C/min). To stretch development without chasing bean temp, cut heat decisively and shift to more convective transfer by raising fan. Keep running past 193°C guide; drop based on achieving duration/flavor rather than the temp guide alone.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=35% fan=25% drop=False conf=0.62 — 'Preheating: current bean temp (183°C) is within charge guidance (180–200°C) and first crack not detected. Hold moderate heat to reach/track near the guide without overshooting (thermal lag), and run modest fan to establish airflow and protect flavor once roasting begins; no drop since development phase hasn’t started.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=95% fan=35% drop=False conf=0.62 — 'Pre–first crack: bean is ~143°C and climbing very slowly (RoR 11.7°C/min) while fan has been at 0% with heat at 100% for several samples. To overcome thermal lag and keep a steady, even rise, reduce heat slightly (from 100%) and introduce moderate fan (convective mixing) to avoid uneven heating and to prepare for a controlled transition. No drop since first crack has not occurred.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=78% fan=40% drop=False conf=0.62 — 'Pre–first crack and beans are still below the ~193°C guide (176°C) with moderate RoR (10.3°C/min). Increase airflow to prepare for an even, convective-driven rise while slightly reducing heat to manage thermal lag and avoid overshooting the temperature target before first crack. No drop yet because first crack hasn’t occurred and development-ratio guidance for drop isn’t applicable.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=55% fan=78% drop=False conf=0.74 — 'Post-crack development so far: 52s out of 533s ≈ 9.8% (below the ~10–20% target and trending short). Bean temp is ~186°C and RoR is high (~10°C/min), so we should act early: cut heat substantially to slow energy input and protect from overshooting, while raising fan toward convective heat transfer to stretch development time and avoid baking/uneven scorching. Targeting continued development rather than an immediate drop at the 193°C guide.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=88% fan=18% drop=False conf=0.56 — 'Pre–first crack at 13s with bean temp ~110°C and heat stuck at 100% while fan is 0: reduce heat slightly to avoid an overshoot before crack (thermal lag), and add modest airflow to begin shifting toward more even convective transfer and smoke/chaff evacuation without crashing the RoR too early. Continue roasting; no drop since first crack not detected and development hasn’t started.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=85% fan=55% drop=False conf=0.46 — 'Pre-first-crack: bean is still ~129°C and RoR is high (~13.8°C/min). Reduce heat from prior 100% to control ramp and avoid scorching, while raising fan toward more convective heat transfer to keep flavor even and manage smoke/chaff. Keep drop false since first crack not detected yet; guide is pre-development, not duration.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=55% fan=68% drop=False conf=0.72 — 'Post-crack development is very short vs the 24% target (14s dev elapsed at 495s roast ≈2.8%), while RoR is high (~10.3°C/min). Because the drum/element has thermal lag, cut heat now and shift to more convective airflow to slow bean temp rise and stretch development without baking; drop is premature—hold through duration until target profile time is reached and temp approaches the guide gradually.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=45% fan=65% drop=False conf=0.74 — 'Development ratio is ~104/585=17.8% (on the low side vs 24% target), so extend development slightly rather than dropping. Bean RoR is still high (~9 C/min) and bean temp is only ~185 C; due to thermal lag, reduce heat modestly (not too timid) and increase fan to shift toward convective transfer and keep flavor even while stretching time.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=85% fan=30% drop=False conf=0.62 — 'Pre–first crack with bean temp ~151°C and falling RoR implied by the last samples; heat has likely overshot but thermal lag means act early. Reduce heat from 100% to ~85% to recover a steadier climb while keeping modest fan (~30%) to prevent stalling and to improve even convective transfer without collapsing RoR. Drop not considered until after crack and development-ratio target is measurable.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=85% fan=22% drop=False conf=0.72 — 'Pre–first crack and bean temp ~142°C with RoR 12.3°C/min (still rising slowly) suggests we should continue charging heat to reach first crack soon; increase fan modestly for more even convective transfer without stalling RoR. Targeting slightly higher heat with a gentle airflow rise also helps prepare for the later development phase. No drop: first crack not detected and development ratio/goal is not applicable yet.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=55% fan=95% drop=False conf=0.74 — 'Post-crack development is only ~13% (near 14% target) but bean RoR is high (11 C/min), so heat should be reduced to slow the temperature rise while increasing fan for more convective transfer and more even development. Recent samples show bean temp ~182–183 C while heat stayed at 80%; lowering heat anticipates thermal lag, and high fan helps avoid baking. Keep roasting (do not drop yet) until development ratio and texture finish align; target drop temp guide ~193.7 C is not yet reached.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=55% fan=80% drop=False conf=0.66 — 'Development is ~7.8% (43s of 554s) which is below the 10–20% target, and RoR is high (~10.3 C/min). To stretch duration safely without overshooting, cut heat decisively to anticipate thermal lag while raising fan toward more convective transfer to smooth heat uptake and protect flavor. Keep roasting; drop is not yet warranted despite the guide temp ~193.7 C not being hit yet.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=70% fan=30% drop=False conf=0.62 — 'Pre–first crack and bean temp ~180°C with high RoR (~18.3°C/min) suggests heat is already driving effectively; apply moderate heat and keep fan moderate to avoid over-convection before crack while still supporting stable heating. No drop since first crack not reached and development ratio is not applicable yet.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=95% fan=25% drop=False conf=0.62 — 'Pre–first crack and bean RoR is moderate (~12.3°C/min) while bean temp is still low (141°C). Increase heat only slightly to keep an orderly rise and preempt thermal lag, and raise fan from the current low level to improve heat transfer consistency and smoke/chaff evacuation without overcooling (fan/heat balance). Drop is not appropriate yet since first crack has not occurred and development targets apply after post-charge/first crack.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=55% fan=65% drop=False conf=0.71 — 'Development ratio is ~14.4% (in the 10–20% window) but bean RoR is high (~11 C/min), so hold off the drop and prevent overshoot. Reduce heat decisively (thermal lag) to bring RoR down while increasing fan for a more convective, even transfer and better smoke/chaff evacuation; this should extend development slightly without baking beyond the ~194 C guide.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=35% fan=70% drop=False conf=0.72 — 'Post-crack development is low-risk but slightly short: 36s development on 513s total ≈7% (below 10–20% target 14.4%). Bean RoR ~10°C/min is high, and bean temp is still ~187°C vs guide 194°C, so ease heat (accounting for thermal lag) and push fan higher to keep convection and slow/shape the energy delivery. This should stretch development toward the target without chasing temp; hold off dropping until development ratio is met/flavor deepens.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=85% fan=25% drop=False conf=0.46 — 'Pre–first crack: bean temp is still rising slowly and heat is currently at 100% with 0% fan; add fan for airflow/protection while easing heat slightly to avoid overshoot once thermal lag catches up. Keep roasting until first crack; no drop signal yet (development ratio not applicable).'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=35% drop=False conf=0.62 — 'Pre–first-crack: maintain strong drum energy to accelerate toward crack since bean temp is only ~140°C and RoR is high (~12.7°C/min). Recent samples show 100% heat with 0% fan and bean temp rising slowly, so introduce moderate fan to promote more even convective transfer and help smoke/chaff evacuation without starving heat input. No drop: first crack not detected; prioritize reaching crack and establishing a stable post-crack RoR profile.'
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=55% fan=85% drop=False conf=0.74 — 'Post-crack development ratio ~2s/498s=0.4% is far below the 10–20% target, and bean RoR is high (10°C/min). Cut heat decisively to let the lagged drum energy unwind while raising fan toward convective transfer to keep the roast in a controlled development regime and avoid tipping into dark/baked flavor. Continue roast; drop is not yet warranted.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=55% fan=75% drop=False conf=0.78 — 'In development: elapsed 559s, post-crack RoR ~8.7°C/min is high; to hit ~10–20% development duration (~19.9% target) without overshooting, reduce heat decisively (thermal lag means temp will keep rising) and raise fan to shift toward convective transfer for even energy and smoke/chaff control. Bean temp is ~186°C and below the 194°C guide, so continue roasting rather than dropping early; coordinate heat+fan balance to avoid a RoR stall/baked flavor.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=55% fan=35% drop=False conf=0.62 — 'Preheating/not yet charged properly to bean-temp target window (180–200 C). Apply moderate heat early to overcome thermal lag toward the 194 C guide, while keeping fan mid-range to ensure stable airflow and smoke/chaff handling without overly convecting and stalling drum heating.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=85% fan=35% drop=False conf=0.62 — 'Pre–first crack: bean temp is ~141°C with RoR ~11.7°C/min—still rising but not near the drop guide. Keep heat high but slightly reduced to manage thermal lag and avoid overshoot as you approach ~190–195°C later; add moderate fan to keep convective transfer stable and prevent scorching while maintaining an even climb toward first crack.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=55% fan=85% drop=False conf=0.74 — 'In development (8s into dev; elapsed 502s total) with high post-crack RoR ~10°C/min and beans ~179°C, you’re still below the 194°C guide. Use the coupled control: cut heat from 100% to slow further temperature rise, and raise fan toward convective mode to keep transfer even and to stretch development duration. Drop is not yet indicated because the development ratio target (~17.7%) is near/approaching but not clearly complete; continue developing modestly past the guide if needed, avoiding chasing temperature.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=25% fan=95% drop=False conf=0.72 — 'Post-crack development is only ~69s out of 563s (~12%), slightly under the 10–20% target but approaching the guide temp. With RoR still high (9°C/min), cut heat substantially and push airflow toward convective heat transfer to slow bean temperature rise and preserve flavor while extending duration. Do not drop yet; leave headroom to reach the duration window rather than reacting to bean temp alone.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=55% fan=35% drop=False conf=0.62 — 'Pre–first crack early ramp: bean temp is only ~117°C and rising slowly with heat/fan currently at 0, so start heat decisively (accounting for thermal lag) while keeping moderate fan for even convective transfer without overly stalling RoR.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=85% fan=55% drop=False conf=0.62 — 'Pre–first-crack at ~302s with bean temp ~130°C and RoR ~14°C/min; keep heat strongly but not max to avoid overshoot/lag, while raising fan to begin shifting toward more even convective transfer and smoke/chaff evacuation. No drop: first crack not yet detected.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=35% fan=78% drop=False conf=0.74 — 'Post-crack development is very short (≈1s of development, ~22% target but current RoR is high ~11.3°C/min), and bean temp is still well below the guide (173°C vs 194.7°C). To stretch duration and avoid over-baking, cut heat substantially (thermal lag) and raise fan toward convective transfer for even color and flavor. Not yet drop: continue development until duration target is met and smoke/texture cues indicate.'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=45% fan=70% drop=False conf=0.72 — 'Post-crack development elapsed 92s over 604s total ≈15% development ratio (within/near the desired 10–20%), but current RoR is still high (bean 8°C/min). Bean temp is only ~185°C and drifting slowly, so to stretch development toward the upper end without scorching, cut heat substantially (thermal lag) and shift to more convective airflow to control bake risk and drive even heat transfer. Do not drop yet; continue developing until the roast hits the duration goal, dropping close to when RoR slows further and development ratio approaches target.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=85% fan=35% drop=False conf=0.62 — 'Preheating: beans are ~112°C and rising slowly with heat/fan currently at 0. Raise heat decisively to recover thermal lag and reach charge guidance (~180–200°C) without overshooting; keep fan moderate to begin establishing convective mixing (more even heat transfer and smoke/chaff management) but not so high that it excessively cools and stalls the RoR. No drop in preheating.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=85% fan=18% drop=False conf=0.62 — 'Pre-first-crack: bean RoR is high (12.3 C/min) and bean temp is still well below the 195 C guide. Hold heat slightly below max (thermal lag) while modestly increasing fan to keep convective cooling/smoke control without crashing RoR. No drop since first crack hasn’t occurred and development ratio target is only relevant after crack.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=45% fan=70% drop=False conf=0.66 — 'Post-crack development ratio is ~3s/599s ≈13%, close to target. Bean temp is flat at 179°C with RoR ~9°C/min, suggesting thermal lag and that heat is already high; to avoid baking/darkening while stretching the final part of development, cut heat decisively and raise fan for more convective transfer and smoke/chaff control. Keep drop at this point until ratio/final RoR confirms; only drop if development runs long or RoR collapses.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=45% fan=92% drop=False conf=0.74 — 'Post–first crack development ratio is on target (~63s/659s = 9.6%, aiming ~10–20%). Bean temp is hovering ~189–190°C with RoR ~11°C/min, so avoid heating further to prevent overshoot/browning; instead shift toward more convective transfer (higher fan) while cutting heat moderately. Keep roasting (do not drop yet) to extend duration slightly and smooth RoR as you approach the ~195°C guide.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=45% fan=25% drop=False conf=0.78 — 'Preheating with no bean RoR/first crack yet: start with moderate heat to approach charge temp while avoiding overshoot from thermal lag; keep fan modest to maintain effective drum heat transfer and stable warming, not aggressive convection yet.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=14% drop=False conf=0.58 — 'Pre–first crack: bean temp is still ~138–139°C and RoR is moderate (~12.7°C/min) with stable environment. Keep heat high to continue climbing; only slightly raise fan from very low to improve mixing/evacuation without overly cooling and stalling the rise. No drop before first crack.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=55% fan=65% drop=False conf=0.74 — 'Post-crack development elapsed is 12s on 444s total (~2.7% so far), but bean RoR is high (~10°C/min), indicating rapid energy uptake risk. To stretch into a ~10–20% development window without overshooting darkening, cut heat substantially to counter high RoR and raise fan toward more convective, flavor-protecting heat transfer and smoke evacuation. Target is to hold development rather than drop yet (current bean temp 172°C below 195°C guide).'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=35% fan=85% drop=False conf=0.74 — 'Post-crack development: development ratio so far = 102s / 534s ≈ 19.1%, which is within/near the desired 10–20% band. Bean temp has stalled ~188°C while RoR is high (10.7°C/min) and heat+fan are both at 70%, indicating energy is still driving heat too aggressively without moving bean temp yet (lag). To stretch and smooth development without baking, cut heat substantially and raise fan toward more convective transfer to stabilize RoR and evacuate smoke/chaff. Do not force the drop yet; stay on duration control rather than the 195°C guide.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=65% fan=55% drop=False conf=0.62 — 'Preheating with bean temp only ~119°C and no RoR/first-crack data yet: raise heat decisively to reach charge guidance while keeping fan moderately high to stabilize airflow and prevent localized overheating. Drop is not applicable pre-crack.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=92% fan=100% drop=False conf=0.42 — 'Pre–first-crack and bean temp ~140°C with RoR 11.3°C/min while environment is ~195°C; despite fan already high, heat appears driving conservatively/late due to thermal lag. Reduce heat slightly from 100% but keep fan max to stabilize heating and preserve convection; no drop pre–FC.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=70% drop=False conf=0.74 — 'Post-crack development ratio is ~16% vs target ~13.6% and RoR is still high (bean RoR 10°C/min), so we should cut heat now to stretch time/avoid overshoot, while increasing fan to shift to more convective transfer and protect flavor. Continue roast (don’t drop yet) and manage toward the duration goal rather than the 195.3°C guide.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=55% fan=92% drop=False conf=0.72 — 'Post-crack development is ~9.1% (62/684) and currently near 190.7°C with high RoR (~9.7°C/min). To reach the ~10–20% development-duration target without baking/darkening, cut heat substantially (thermal lag/energy in the drum) and raise fan to shift to more convective, even heat transfer while controlling smoke/chaff. Stay in-development and do not drop yet.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.42 — 'Pre–first crack: bean temp is already near ~90–99°C and currently heat is high with fan near 0, so airflow may be too low to promote even convection and protect against uneven heating. Keep heat high to stay on track early (thermal lag), but raise fan modestly to improve mixing and smoke/chaff evacuation; do not drop yet (first crack not detected).'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.44 — 'Pre–first crack, bean temp is still ~137°C with RoR high (~10.7°C/min) while first crack not yet detected. Maintain strong heat to push through to first crack; fan should remain low to avoid cooling/overconvection before crack, and there is no basis to drop before first crack.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=72% fan=35% drop=False conf=0.66 — 'Pre–first-crack with bean RoR ~10.3°C/min and temp still below guide (179°C vs 195°C). Reduce heat from 100% to avoid overshooting once thermal lag catches up, while raising fan to improve convective heat transfer and smoke/chaff evacuation. Not yet at/approaching first crack, so continue roasting; don’t drop.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=55% fan=75% drop=False conf=0.72 — 'Development is ~54s of 680s roasted (~8% so far), slightly under the ~10–20% target. Bean temp is plateauing ~189°C with post-crack RoR ~10°C/min, so the safe move is to reduce heat to avoid overshooting while increasing fan to keep transfer convective and extend development duration. Target keeps development trending toward the 10–12% zone and holds off the drop until ratio is achieved; drop only if RoR/ratio overshoots.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=95% fan=12% drop=False conf=0.62 — 'Pre–first crack: bean temp is still ~97°C and stable while heat is already high (100%) with low fan (10%). To keep RoR building but avoid overshooting into early cracking, slightly reduce heat and add a touch more fan for more even convective transfer and smoke/chaff handling. No drop until first crack and development-duration criteria are active.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=78% fan=18% drop=False conf=0.62 — 'Pre–first crack and still climbing slowly (RoR ~13.3°C/min) with bean temp ~139°C. Keep heat moderately high but slightly reduced to avoid overshooting when thermal lag catches up; raise fan a bit to keep airflow uniform and start smoke/chaff evacuation early without driving RoR down prematurely. Drop not appropriate yet (no first crack).'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=70% fan=75% drop=False conf=0.74 — 'Development is early/mid (≈19.6% of total so far) but RoR is still high (~9.8 °C/min), suggesting the beans are climbing quickly. To steer toward the duration target without overshooting into too-dark territory, cut heat modestly (from recent ~80–100%) and raise fan to increase convective transfer and protect development flavor while venting smoke/chaff after first crack.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=38% fan=88% drop=False conf=0.66 — 'Post-crack development is on the long side (66s of 588s total ≈11% so far, target ~10–20%), but bean RoR is still high (~9°C/min) and bean temp is plateauing around 188–189°C while env is high. To safely stretch duration and avoid overshooting into dark development, cut heat moderately (thermal lag) and keep airflow high toward convective transfer (fan ~88) to stabilize RoR and evacuate chaff/smoke. No need to drop yet; continue development and reassess against duration target rather than the 196°C guide alone.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.62 — 'Pre–first crack: bean temp has been falling despite 100% heater, and fan has been 0%, indicating poor heat transfer/measurement lag. Keep heat high to re-establish drum energy, but add modest fan airflow to stabilize/raise bean temperature toward the upcoming drying/early development without overshifting to convective mode too early.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=78% fan=25% drop=False conf=0.62 — 'Pre–first crack: bean temp is only ~135°C and stable while heat is max and fan is 0, so convective mixing is lacking. Reduce heat slightly to avoid overshoot once thermal lag catches up, and raise fan moderately (convection + smoke/chaff handling) to bring the drum/bean energy transfer under control. No drop before first crack; aim to reach first crack smoothly without chasing the 196°C guide (development ratio only applies post-crack).'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=55% fan=60% drop=False conf=0.72 — 'Post–first crack development timing is low so far: only ~3s development on 592s elapsed (~0.5% vs target ~16.5%). With RoR ~10°C/min (high), bean temp is still near 178°C, so hold off the drop and re-balance: reduce heat to avoid over-accelerating, but raise fan toward more convective transfer to speed/flatten heat uptake and protect flavor as you extend development toward the duration target.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=45% fan=92% drop=False conf=0.66 — 'Post-crack development ratio is ~9.7% (63s of 652s elapsed), slightly below the 10–20% duration target, so don’t drop yet. Bean RoR is high (9.0°C/min), with bean temp lagging near 187–187°C and still under the ~196°C guide; coordinate by cutting heat decisively and raising fan to convective transfer to stretch development and avoid overshooting/darkening while maintaining even heat transfer. Target balance: lower drum energy, higher airflow to control RoR and protect flavor.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=22% fan=25% drop=False conf=0.62 — 'Preheating has not reached charge/flight conditions yet and bean temperature is only ~117°C; use modest heat to start warming while keeping fan low-moderate to avoid over-accelerating convective losses and to prepare for post-charge ramp coordination once RoR becomes available.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.42 — 'Pre–first crack and bean temp is ~137°C with RoR ~11°C/min. With no first-crack yet, keep heat steady (thermal lag means small reductions may not show immediately) and maintain low-to-moderate fan to avoid convective overcooling. Fan is held near current (10%) to preserve drum heat transfer while continuing to build toward first crack and avoid premature convection changes.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=55% fan=65% drop=False conf=0.72 — 'Post-crack development target is ~14.6%: at 623s roast elapsed with only ~3s since first crack, you’re running at the low end of the desired duration, and bean RoR (10.7 C/min) is still strong. Cut heat substantially to avoid overshooting/over-developing while raising fan toward more convective transfer to stretch time and prevent baked/scorched flavor. Continue roasting rather than dropping yet since you haven’t reached the duration window and bean temp is still below the ~196 C guide.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=55% fan=92% drop=False conf=0.74 — 'Post-crack development is ~9.2% (63s / 683s) and RoR is high (~9.3°C/min), with bean temp only ~189°C vs guide 196°C. To extend development without overshooting into late dark, cut heat decisively to slow energy input (thermal lag) and raise fan toward convection for more even heat transfer and smoke/chaff evacuation. Keep roasting (no drop yet) to reach the ~10–20% duration window rather than chasing the temperature guide.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=92% fan=20% drop=False conf=0.62 — 'Pre–first crack: bean temp is only ~97°C and RoR is falling negative (cooling vs environment), so increase energy slightly but not to max; raise airflow to maintain even heat transfer and protect flavor while climbing toward the ~12–20% development target later. No drop yet (not post–first crack and far below target drop temp).'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=85% fan=35% drop=False conf=0.62 — 'Pre–first-crack: bean RoR (~10.3 C/min) is solid and bean temp is only ~135 C with no crack yet. Reduce heat slightly from 100 to avoid overshooting once drum/element thermal lag catches up, while raising fan to improve convective heat transfer and smoke/chaff handling without collapsing RoR (keep heat dominant). Aim to reach first crack smoothly rather than chase the 196.3 C guide (not yet in development).'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=60% fan=85% drop=False conf=0.74 — 'Post-crack development is only ~4s into a 616s roast (~0.6% of total), while RoR is high (11.3°C/min) and bean temp is still rising quickly toward the guide (196.3°C). To stretch duration and avoid baking, cut heat substantially to reduce energy input (anticipating thermal lag) and raise fan toward convective transfer to cool the surface and keep development controlled. Not dropping yet because duration target (≈10–20% development) hasn’t been approached.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=35% fan=95% drop=False conf=0.74 — 'Post-crack elapsed ~64s with ~676s total so development ratio ≈9.5% (near the 10% target), while bean temp is ~191°C and RoR is high (13°C/min). To lengthen/steady development toward ~10–13% without overshooting darkness, cut heat early (thermal lag) and push airflow higher for more convective transfer and smoke/chaff control; don’t drop yet since the duration objective isn’t clearly satisfied and RoR is still elevated.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=65% fan=18% drop=False conf=0.74 — 'Preheating before charge/first crack: hold moderate heat to bring drum/bean temps up toward the 180–200°C charge window, while keeping fan low enough for efficient heat-up but still ensuring airflow/mix. No drop action pre–first crack.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=90% fan=25% drop=False conf=0.56 — 'Pre–first-crack, bean temp is lagging (125°C vs target-drop guide 197°C) while RoR is still healthy (~14°C/min). Keep heat high but slightly reduced to avoid overshooting later given thermal lag; introduce modest fan for steadier convective transfer and smoke/chaff management without crashing RoR.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=78% fan=35% drop=False conf=0.62 — 'Pre–first-crack: bean temp is still ~168°C and RoR is brisk (~12°C/min). Because heat has lag, reduce heat from 100% to avoid overshooting, while keeping fan moderate to maintain stable drying and airflow. No first crack yet and development ratio is not applicable, so don’t drop.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=60% fan=70% drop=False conf=0.67 — "In development: roast elapsed 595s with dev 85s ⇒ dev ratio ≈14%, slightly under a 15–20% target, and bean RoR is high (12°C/min). You're at ~185°C, below the 197°C guide, so don’t drop yet—reduce heat to cool RoR and extend duration, while raising fan to push toward convective transfer and keep development even/smoke/chaff moving out. Aim to progress duration without overshooting the ~195°C flavor-risk zone."
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=75% fan=35% drop=False conf=0.62 — 'Preheating/approach-to-charge: bean RoR is moderate-high (~29°C/min) and first crack not detected, so prioritize bringing beans toward charge guidance quickly but without aggressive overshoot. Set heat decisively (not max) to overcome thermal lag, and keep fan at a moderate level to stabilize heat transfer and clear smoke/chaff once airflow engages; no drop since still well below target and pre-crack.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=78% fan=35% drop=False conf=0.62 — 'Pre–first crack at 134°C with RoR (9.3°C/min) still healthy. Because bean temp change lags, keep heat fairly high but begin easing slightly and add fan to prepare for more even convective transfer (and smoke/chaff evacuation) without collapsing RoR. Target the eventual ~10–14% development later; don’t drop yet since first crack not detected and elapsed is well before the development window.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=62% fan=65% drop=False conf=0.74 — 'Post-crack development is only ~19s / 656s ≈ 2.9% vs target ~14%, and RoR is high (11.7°C/min), so to stretch duration without overheating, cut heat decisively and raise fan to shift toward more even convective transfer. Target is modest development beyond the 197°C guide as needed, so don’t drop yet.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=10% fan=75% drop=False conf=0.72 — 'Post-crack development ratio is ~49s/686s ≈7%, below the 10–14% aim; RoR is still relatively high (12.7 C/min). Temperature has not reached the 197 C guide yet and bean temp is lagging, so cut heat modestly (10%) to avoid overshoot while increasing fan for a more convective, even development and smoke/chaff handling—extend development to hit duration rather than dropping early.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=85% fan=18% drop=False conf=0.62 — 'Pre–first crack and bean temp is still rising but trending down in RoR (around -7 C/min) while heat is already maxed. Reduce heat slightly (thermal lag/overshoot management) and raise fan modestly to stabilize heat transfer mode and improve convection without stalling RoR. No drop since first crack not detected; focus on bringing beans toward the next phase smoothly toward ~10–20% development once crack begins.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=82% fan=45% drop=False conf=0.62 — 'Pre–first crack: bean temp is still low (~134°C) and RoR (13.3°C/min) suggests we should continue driving heat but avoid overshoot. Increase fan moderately to keep heat transfer even and start evacuating chaff/smoke buildup, while not going overly convective. Hold the drop (no first crack yet) and wait to reach/approach first crack before making major changes.'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=55% fan=80% drop=False conf=0.72 — 'Post-crack RoR is high (~11 C/min), beans are still below the 197 C guide (176 C) and development elapsed is only 6s; to lengthen development toward the 20% target without overshooting temperature, cut heat decisively to avoid accelerating bean temp and raise fan for more convective, even heat transfer and smoke/chaff evacuation. Drop is not warranted yet based on development progress.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=55% fan=70% drop=False conf=0.66 — 'Post–first crack development is running fast vs target: development ratio 66/583 ≈ 11% (below 20.5% target) but bean RoR is still high (~10°C/min), and bean temp is rising slowly near 186→186 (thermal lag/plateau). To avoid tipping into too-dark while extending duration toward ~10–20%, cut heat decisively (100%→55%) and raise fan to shift toward more even convective transfer and protect flavor (~70%). No drop yet because duration/flavor timing looks like it can still be stretched slightly before the ~197°C guide becomes a hard driver.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=72% fan=35% drop=False conf=0.62 — 'Pre–first-crack: bean temp has cooled from ~101→91°C while heat is pegged at 100% and fan is 0%. Add controlled airflow (more convective transfer, smoke/chaff management) and reduce heat to prevent overheating/overshoot once thermal lag catches up. Aim to hold stable warm-up until approaching first crack; no drop since it hasn’t started and development window isn’t relevant yet.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.63 — 'Pre–first crack and bean temp is lagging (RoR 12.3 C/min) with no crack detected; keep heat at/near current max to drive temperature up, while adding modest fan to maintain airflow/smoke evacuation without overcooling or shifting too hard into convective transfer. No drop yet because first crack hasn’t occurred and duration guidance applies post–crack.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=55% fan=75% drop=False conf=0.74 — 'Post-crack development target (~17.8%) is on track, but RoR is high (10.7 C/min) while bean temp is still only ~178 C and appears to be stalled with fan at 0%. Thermal lag means heat needs to be reduced now to avoid overshooting while increasing fan for more convective, even energy transfer and smoke/chaff evacuation. Keep roasting (no drop yet) to reach the intended duration; drop would be premature given only ~4s into development.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=45% fan=75% drop=False conf=0.74 — 'Post-crack development ratio is ~10.3% (64s/621s), slightly short of the ~10–20% sweet spot. With RoR ~9°C/min, bean temps are hovering ~187–187°C while bean temp lags element changes—so cut heat decisively and raise fan to more convective transfer to stretch development without chasing temperature. Hold drop until development time/ratio reaches target; don’t drop just because the ~197°C guide nears.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=55% fan=25% drop=False conf=0.58 — 'Pre–first crack (only 19s elapsed), bean temp is still ~86°C and RoR is negative (-16°C/min) while environment RoR is high. Reduce heat from full to avoid further lag-driven overshoot, and raise fan modestly to improve heat transfer stability/evacuate moisture/chaff. Keep dropping false; target drop/development guidance is for post–first-crack timing, not yet reached.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=82% fan=30% drop=False conf=0.62 — 'Pre–first crack. Bean temp (~138°C) is rising very slowly (RoR ~10.3°C/min) while env is ~193°C; with thermal lag, cut heat slightly from max to avoid overshoot as drum/air approach higher energy transfer, and raise fan modestly to keep heat transfer even and protect color as you approach the crack zone. Target remains pre-crack; no drop yet.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=70% fan=78% drop=False conf=0.74 — 'Post-crack development ratio ~12% is on-target but slightly low/rapid for given RoR (9.7°C/min). To extend development duration without overshooting dark, cut heat decisively (thermal lag) and raise fan to shift to more even convective transfer and smoke/chaff evacuation. Keep drop held until duration reaches ~13–15% and bean temp stabilizes below the ~195–200°C guide.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=28% fan=88% drop=False conf=0.74 — 'Post-crack duration is ~72s out of 710s (13.6% target). Bean is only ~193–194C and RoR is elevated (10.3C/min), so to avoid overshoot while still extending to hit the duration sweet spot, cut heat decisively and raise fan to shift toward convective, more even transfer and protect flavor. No need to drop yet; continue the development until RoR tapers and duration approaches target.'

### openai/gpt-4.1-mini (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.45 dir=0.524; fan MAE=10.45 dir=0.333; latency pre=1.94s preFC=1.87s FC=1.95s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     8 |     9 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.524 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=11.52 dir=0.542; fan MAE=11.6 dir=0.125; latency pre=1.51s preFC=1.76s FC=1.83s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    10 |    10 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.542 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.44 dir=0.577; fan MAE=10.93 dir=0.269; latency pre=1.62s preFC=1.64s FC=2.01s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     7 |    12 |     3 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.577 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.76 dir=0.5; fan MAE=10 dir=0.25; latency pre=2.17s preFC=1.82s FC=2.08s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     9 |     6 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.5 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.56 dir=0.615; fan MAE=13.89 dir=0.077; latency pre=1.82s preFC=1.83s FC=2.25s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    10 |    11 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.615 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.58 dir=0.478; fan MAE=11.46 dir=0.261; latency pre=1.7s preFC=1.82s FC=1.92s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    11 |     9 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.478 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10 dir=0.55; fan MAE=13.1 dir=0.25; latency pre=1.24s preFC=1.77s FC=1.88s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     9 |     8 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.55 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=7.38 dir=0.75; fan MAE=8.43 dir=0.35; latency pre=1.77s preFC=1.63s FC=1.76s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     5 |    11 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.75 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.48 dir=0.5; fan MAE=10.71 dir=0.25; latency pre=1.52s preFC=1.8s FC=1.84s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    10 |     7 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.5 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.61 dir=0.545; fan MAE=14.35 dir=0.182; latency pre=1.74s preFC=1.79s FC=2.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=22     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    10 |     9 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.545 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=9.86 dir=0.476; fan MAE=8.18 dir=0.286; latency pre=2.12s preFC=1.8s FC=2.12s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    11 |     7 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.476 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=23.8 dir=0.667; fan MAE=11 dir=0.25; latency pre=1.84s preFC=1.83s FC=2.36s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    12 |     5 |
    |        raise |     0 |     0 |     2 |
    (n=24; diagonal agreement=0.667 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=9.32 dir=0.714; fan MAE=15.68 dir=0.048; latency pre=1.62s preFC=1.75s FC=2.63s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     5 |    11 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=21; diagonal agreement=0.714 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.91 dir=0.714; fan MAE=13.41 dir=0.143; latency pre=1.58s preFC=1.81s FC=2.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     6 |    11 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=21; diagonal agreement=0.714 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=17.5 dir=0.391; fan MAE=12.92 dir=0.174; latency pre=1.55s preFC=1.72s FC=1.91s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     6 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.391 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=11.8 dir=0.667; fan MAE=9.2 dir=0.333; latency pre=2.26s preFC=1.87s FC=2.01s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     7 |    12 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.667 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=12.14 dir=0.6; fan MAE=9.05 dir=0.35; latency pre=2.04s preFC=1.68s FC=1.96s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     8 |     8 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.6 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=16.73 dir=0.28; fan MAE=21.92 dir=0.24; latency pre=1.55s preFC=1.86s FC=2.03s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    18 |     5 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.28 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.31 dir=0.36; fan MAE=14.62 dir=0.04; latency pre=1.3s preFC=1.81s FC=2.08s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |    16 |     8 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.36 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15 dir=0.435; fan MAE=7.29 dir=0.348; latency pre=1.71s preFC=1.78s FC=2.33s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    11 |     6 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=23; diagonal agreement=0.435 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=13.46 dir=0.6; fan MAE=12.88 dir=0.16; latency pre=2.04s preFC=1.69s FC=1.71s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     8 |    13 |     1 |
    |        raise |     0 |     1 |     1 |
    (n=25; diagonal agreement=0.6 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=8.27 dir=0.72; fan MAE=7.31 dir=0.28; latency pre=1.31s preFC=1.54s FC=1.83s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     7 |    13 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=25; diagonal agreement=0.72 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=25.81 dir=0.433; fan MAE=7.26 dir=0.367; latency pre=1.49s preFC=1.73s FC=2.1s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    10 |    10 |     6 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.433 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.5 dir=0.609; fan MAE=11.88 dir=0.217; latency pre=3.05s preFC=1.8s FC=2.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     9 |    10 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=23; diagonal agreement=0.609 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15 dir=0.519; fan MAE=9.64 dir=0.296; latency pre=1.58s preFC=1.88s FC=2.04s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     1 |
    |         hold |    10 |    10 |     2 |
    |        raise |     0 |     0 |     2 |
    (n=27; diagonal agreement=0.519 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=23.39 dir=0.593; fan MAE=13.21 dir=0.185; latency pre=1.8s preFC=1.71s FC=1.83s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     6 |    14 |     4 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.593 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.62 dir=0.609; fan MAE=16.88 dir=0.087; latency pre=1.26s preFC=1.67s FC=2.28s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     9 |    11 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.609 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.44 dir=0.462; fan MAE=6.85 dir=0.269; latency pre=1.45s preFC=1.76s FC=1.94s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    12 |     9 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.462 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=60% fan=15% drop=False conf=0.9 — "In preheating phase with bean temp at 123C and environment at 160C, setting moderate heat (60) to raise temps steadily within the charge range (180-200C). Fan at 15% to encourage even heat transfer and air flow without rapid cooling or smoke evacuation, preparing for charge without thermal shock. No drop as roast hasn't begun and development not started."
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'Pre-first crack stage with a high bean rate of rise (14.6 C/min) and current bean temp 131.3 C, below the 189 C target drop temp. Keep heat at max to maintain energy input early and decisive. Moderate slight fan at 10 to introduce some convective heat and chaff evacuation without cooling excessively or stalling RoR. Not time to drop yet, focus on pushing towards first crack.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=85% fan=50% drop=False conf=0.9 — 'The development phase is very early with only 7 seconds elapsed and the development ratio is below target (7/501 ~ 1.4%, target around 20.5%). The bean temperature is 173°C and rising with a good RoR (11°C/min). To control the RoR and stretch development time, reduce heat moderately from 100% to about 85% while increasing fan from 30% to 50% to promote convective heat transfer and smoke evacuation. This balanced adjustment anticipates thermal lag and maintains stable development without rushing the drop, aiming for the desired development ratio rather than hitting the target drop temp early.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=70% fan=75% drop=False conf=0.9 — 'In development phase with 67s elapsed and development ratio near target (67/561 ≈ 12%), the bean temp 181°C is still below target drop of 189°C. RoR is moderately high at 8°C/min. To extend development time without overshooting temperature, reduce heat from 80 to 70% and increase fan from 60 to 75% to shift towards convective heat, stabilizing RoR and ensuring even development without stalling.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=75% fan=10% drop=False conf=0.9 — "In preheating phase with bean temperature at 121C, heat should be set moderately high to bring beans quickly to charge temperature range (180-200C). Fan is kept low to maintain heat buildup while ensuring some airflow to prevent scorching. It's too early to drop; focus is on reaching charge temperature efficiently."
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'Pre-first crack phase with moderate bean RoR (12°C/min). Maintain high heat to continue temperature rise and accelerate development. Increase fan slightly to 10% to introduce some convective cooling and smoke evacuation without stalling the RoR or cooling too much. Do not drop yet as development phase not reached and bean temp below target drop temp.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=85% fan=40% drop=False conf=0.9 — 'In pre-first crack phase at 173C with RoR 10.7°C/min, maintain high but slightly reduced heat (85%) and moderately increase fan (40%) to balance thermal lag and increase convective cooling to avoid overshoot. No drop since first crack not detected and target development ratio not approached.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=70% fan=80% drop=False conf=0.9 — 'Development is early (13% of total time) with a moderate post-crack RoR (7.3) and current bean temp (185C) below target drop (190C). To stretch development and lower RoR, reduce heat moderately from 80 to 70 and increase fan from 70 to 80 for more convective cooling, balancing fan and heat to avoid stalling or baking. No drop yet as development ratio target not reached.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.9 — 'Pre-first crack phase, bean temp declining (-14.3 C/min RoR) despite max heat; environment RoR high (30.3 C/min). Increase fan modestly to promote convective heat transfer and even heating while maintaining max heat to recover bean temp rise aggressively. Not near drop or development phase, so no drop.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — "In pre-first crack phase with bean temperature rising at 12 C/min and environment temperature stable, maintain full heat to continue steady RoR. Slightly increase fan from 0 to 10 to begin managing airflow and chaff without cooling too much, preparing for transition to development phase. No drop yet because we're far from first crack and development time."
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=85% fan=50% drop=False conf=0.9 — 'Development just started (3% development ratio) with bean temp at 174.7C and RoR still high (10C/m). To extend development towards the 19% target, reduce heat decisively to slow RoR and raise fan to 50% to shift heat mode toward convection and evacuate smoke, preventing baked flavors while slowing temperature rise. Drop is not yet advised.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=55% fan=95% drop=False conf=0.85 — 'In development phase with 19% development ratio target and 12.4% actual, the bean temperature at 183.3°C is below the 190°C target drop temperature. The bean RoR is relatively high at 8.6°C/min, so to stretch development and slow RoR, reduce heat substantially from previous 100% to 55% and increase fan to 95% to promote convective heat transfer and prevent baking, aiming to extend development duration and hit the development ratio target without overshooting temperature.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=80% fan=30% drop=False conf=0.9 — "In preheating phase with bean temp at 183C near charge guidance, applying strong heat to bring beans up quickly is appropriate. Moderate fan at 30% balances heat transfer mode and airflow, avoiding scorching but promoting even heating. No drop since roast just started and development hasn't begun."
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=85% fan=20% drop=False conf=0.9 — "Bean temp is rising steadily with a high RoR, so reducing heat slightly from max to 85% to avoid overshoot is wise. Fan increased modestly to 20% to support gentle convective heat transfer and aid chaff and smoke evacuation without crashing RoR. It's early in roasting pre-first crack with no need to drop yet."
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.9 — 'Pre-first crack phase with bean temp at 176C and rising RoR at 11C/min. Since bean temp is still below target_drop_temp (191.3C) and first crack not detected, maintain high heat but reduce from full 100% to 80% to avoid overshoot and raise fan from 10% to 20% for better convective heat balance and smoke evacuation. This will promote steady temp rise and prevent scorching, aiming for controlled development duration.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=35% fan=75% drop=False conf=0.9 — 'In development phase with current bean temp 186C, approaching target drop temp 191.3C but development time ratio is 52/502 (~10.4%) below target 15.7%. RoR is moderately high at 10C/min. To stretch development time while preventing overheating, reduce heat decisively to 35% and increase fan to 75% to shift heat mode toward convection and avoid baking. Do not drop yet since development ratio is below target.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=80% fan=20% drop=False conf=0.9 — 'In preheating phase with bean temp at 125C and environment at 156C, heat should be set moderately high at 80% to quickly raise bean temperature toward charge guidance range (180-200C). Fan set at 20% to allow some convective heat transfer while preserving drum heat buildup. No drop as charge not reached.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=90% fan=15% drop=False conf=0.9 — "Pre-first crack phase with bean temp at 134.7°C and RoR still relatively high at 11.7°C/min. To avoid overshoot, reduce heat from max to 90% and introduce moderate fan (15%) to increase convective transfer and even heat without cooling too much, maintaining good RoR for steady rise. No drop as development hasn't started and target drop temp of 191.7°C is far."
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=60% fan=70% drop=False conf=0.9 — 'Development has just started with a high bean rate of rise (RoR 9.7°C/min). To slow down RoR and stretch development time toward the target 19.5%, reduce heat significantly and increase fan to 70% for more convective cooling and smoke evacuation. This coordinated adjustment prevents stalling and baking, allowing a longer, controlled development phase.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=80% fan=65% drop=False conf=0.9 — 'Development phase with current bean temp 183C and target drop temp 191.7C, development time 95s of 711s total (approx 13.4%). RoR moderate at 6C/min, environment stable. To extend development gradually toward target development percent 19.5%, reduce heat from full to 80% and increase fan from 50 to 65% to encourage convective cooling and stable RoR, balancing heat and fan to avoid stalling or baking. Drop not yet advised, development can continue with this coordinated adjustment.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.9 — 'Early in pre-first crack phase with bean temp rising slowly and no RoR data yet. Maintain full heat to build energy with modest fan (15%) to promote convective heat and prevent scorching, supporting even development. No drop recommended as first crack not detected and development phase not reached.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.85 — "In pre-first crack phase with bean temperature rising steadily and a high RoR, maintain maximum heat to continue energy input and start increasing fan slightly to introduce convective heat transfer and help manage the environment temperature while preventing scorching. No drop yet since development hasn't started and target development ratio not approached."
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.8 — 'Currently, the development is only about 2% (10 seconds development over 521 seconds total), below the target of 20.7%. Bean temp is 175.7C with RoR at 10C/min, indicating a strong rise in temperature. To extend development time, reduce heat from 100% to 70% and increase fan from 30% to 50% to shift toward convective heat and lower RoR moderately, avoiding a roast stall or baked flavor. No drop yet, as development is insufficient despite being under target drop temp (192.7C).'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=75% fan=65% drop=False conf=0.9 — 'Current development time is about 12% of total roast, within target range (10-20%). Bean temperature is approaching drop guide (183C vs 192.7C), RoR is moderate at 7.3C/min. To stretch development evenly, reduce heat from 90% to 75% and increase fan from 50% to 65% to shift more toward convective heat, controlling RoR and avoiding possible bake/scorch while allowing development time to continue.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=50% fan=10% drop=False conf=0.9 — 'Preheating phase with bean temp at 188°C (within charge guidance). Starting moderate heat and low fan to gently raise temperature and prepare for charge, considering thermal lag.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=90% fan=10% drop=False conf=0.9 — 'In pre-first crack with bean temp at 145.7°C and RoR at 11.8°C/min, the heat is high and fan off. To gently smooth temperature rise and avoid overshoot, reduce heat slightly from 100% to 90% and raise fan to 10% to increase convection and chaff removal without cooling too much. No drop indication yet, still in early roast stage.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=55% drop=False conf=0.85 — 'The roast is in early development with a very short development time (5s) and a current development ratio well below the 17.2% target. Bean temperature is 178°C and increasing, but the bean RoR is still high at 10°C/min. To stretch the development and avoid pushing temperature too quickly to the drop target of 193°C, heat should be cut moderately from 100% to 60% and fan raised to 55% to increase convective transfer, lower RoR gradually, and support flavor development. Drop is not recommended yet due to insufficient development time and ratio.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=35% fan=80% drop=False conf=0.9 — 'Development elapsed time is 65s with a development ratio around 12.2%, below the target 17.2%. Bean temp is 187.7°C, below target drop of 193°C, but ROR is still somewhat high at 9.7°C/min. To stretch development time without overshooting temperature, reduce heat decisively and increase fan to shift toward convective mode, controlling the RoR and encouraging even development without stalling.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'In pre-first crack phase with 19 seconds elapsed and a bean temp of 125C, maintain high heat to build energy early due to thermal lag. Slightly increase fan from 0% to 10% to improve convective heat transfer and protect against scorching while supporting steady rise. Not yet near development phase or target drop temp, so no drop now.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=85% fan=30% drop=False conf=0.9 — 'Pre-first crack phase with bean temp rising steadily and a moderate RoR of 12 C/min. To avoid overshooting and maintain a smooth roast curve, reduce heat from maximum to 85% while slightly increasing fan to 30% to improve convective heat transfer and prevent potential scorching. This balances energy input and airflow early to promote a controlled roast approaching first crack without rushing.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=70% fan=60% drop=False conf=0.9 — 'Currently in early development phase with a slightly high RoR of 10 C/min on beans and 7 C/min environment. To slow the rise and stretch development time toward the 10-20% target ratio, I suggest reducing heat moderately to 70% and increasing fan to 60% to enhance convective cooling and smoke evacuation without stalling the roast. Temperature is 180C, below the drop guide of 193C, so no drop yet. Coordinated heat fan adjustment balances thermal lag and flavor preservation.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=40% fan=85% drop=False conf=0.9 — "In development phase with a high bean RoR of 10.6 C/min and a current temperature of 185.3 C (below the target drop temp of 193.0 C), it's best to reduce heat decisively to slow the temperature rise and increase fan setting to shift toward convective heat for even development and smoke evacuation. The development ratio is still below the 16.6% target, so we should not drop yet but extend development with coordinated heat decrease and fan increase."
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=70% fan=20% drop=False conf=0.9 — "Preheating phase with bean temp at 183°C within charging guidance 180-200°C. Begin with moderate heat (70%) to raise bean temperature efficiently and low fan (20%) to maintain heat and start gradual airflow. No drop as development hasn't started and temperature is below target drop guidance."
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'Pre-first crack phase with bean temp rising steadily at a moderate RoR. Maintain full heat for energy input and slightly raise fan to 10% to prevent scorching and promote even convective heat transfer without cooling too much. Not near target drop temp or development phase yet, so no drop needed.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=90% fan=40% drop=False conf=0.9 — "Bean temp is 176C and RoR is moderate at 10.3C/min, below the target drop temp of 193C. We're pre-first crack and within development time goals. Slight reduction in heat from full to 90% with a modest increase in fan to 40% balances heating and airflow, controlling RoR while preventing too much build-up, preparing for first crack without rushing drop."
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=70% fan=65% drop=False conf=0.85 — 'In development phase with bean temp at 186C and target drop temp of 193C. Development time is about 52s on 533s total roast (approx 9.8%), slightly below target 15.3%. RoR is moderate at 10C/min post-crack. To stretch development safely, reduce heat from 80% to 70% and raise fan from 50% to 65% to shift heat mode to convective, moderate RoR, and prevent baking. Not yet time to drop since dev ratio is below target.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'Pre-first crack phase with bean temp rising but recent samples show cooling trend. Maintain max heat to overcome thermal lag, raise fan modestly to prevent scorching and aid even heat transfer. No drop needed; development not started and bean temp well below target drop temp.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=90% fan=15% drop=False conf=0.9 — 'Pre-first crack with bean temp rising steadily and a high RoR, reduce heat slightly from max to prevent overshoot. Increase fan slightly to introduce convection and protect flavor while stabilizing rise. No drop yet; development has not started.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=90% fan=70% drop=False conf=0.9 — 'In the development phase with a bean temp of 172°C and a high RoR (10.3°C/min), the development time is still low (14s on 495s total, about 2.8%), below the target 24.3%. To stretch development duration and avoid overshooting the drop temperature, reduce heat decisively to 90% and increase fan to 70% for more convective cooling, balancing heat to slow rate while preventing baking or scorching.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=50% fan=50% drop=False conf=0.9 — "In development phase with a relatively high bean RoR (9 C/min) and env RoR (12 C/min), it's important to slow the rise in bean temperature to stretch development duration. The current development ratio is 104/585 = 17.8%, close to the 24.3% target. The bean temp is below target drop temp of 193°C. Reducing heat to 50% and increasing fan to 50% will lower RoR through more convective cooling to prevent overshooting and extend development time, balancing heat and fan decisively to avoid stalling or baking."
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'Early in the pre-first crack phase with bean temp at 151°C and high heat at 100%, we maintain max heat to continue stable rise. Fan raised slightly to 10% to begin introducing convection and smoke/chaff control while preserving radiant heat for strong RoR. No drop should occur, as first crack not yet detected and development phase not started.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=90% fan=15% drop=False conf=0.9 — "In pre-first-crack phase with bean temp 142°C and a healthy RoR of 12.3°C/min, maintain high but slightly reduced heat to continue steady rise without overshoot. Slightly raise fan from 10% to 15% to moderate environment temperature (202°C) and support even convective heat transfer, preventing scorching. No drop yet as development phase hasn't started and target drop temp (193.7°C) is far away."
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=60% fan=80% drop=False conf=0.9 — 'Development elapsed is only 13 seconds (about 2.5% development ratio against 524s total). The bean temperature at 182.7°C is below the target drop temp of 193.7°C but RoR is a bit high at 11°C/min. To stretch development time toward the 14% target without pushing temperature too fast, reduce heat significantly and increase fan to about 80% to shift mode toward convective, avoiding baked flavors while controlling RoR.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=65% fan=75% drop=False conf=0.9 — 'In development phase with beans at 187.3 C, approaching target drop temperature but not reached yet. Roast elapsed 554s, development elapsed 43s (~7.8% development), below target 14%, so extend development. Bean RoR is moderately high (10.3 C/min). Lower heat from 80% to 65% to slow roast rate and increase fan from 60% to 75% to promote convective heat transfer and even development. Coordinated adjustment balances heat and fan to avoid stalling or baking. Do not drop beans yet as development ratio target not met.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=75% fan=35% drop=False conf=0.9 — 'Bean temp at 180C with high RoR (18.3C/min) in pre-first crack phase. Heat is set moderately high to continue energy input for steady ramp, fan raised to 35% to maintain convective balance, prevent uneven heat buildup and chaff accumulation. Development phase not started, so no drop now.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=15% drop=False conf=0.9 — 'Pre-first crack phase with bean temperature rising steadily and RoR moderate at 12.3 C/min. Maintain max heat to continue energy input, slightly increase fan from 10% to 15% to encourage convective heat and smoke evacuation, preventing potential scorching and supporting even development. No drop yet as first crack not detected and development phase not started.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=80% fan=40% drop=False conf=0.9 — 'In development phase shortly after first crack with high RoR (11 C/min). To stretch development time and avoid overheating, reduce heat from 100% to 80% and increase fan from 30% to 40% to promote convective heat transfer and control RoR. Development ratio is only about 1.24%, far below the 14.4% target, so do not drop yet.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=40% fan=60% drop=False conf=0.9 — 'Development is ongoing at 36s with a high bean RoR of 10 C/min, above ideal for development. Target development percent is 14.4%, still short of recommended range. Bean temp near target drop temp 194 C but development time suggests stretching development. Decrease heat moderately and increase fan to 60% to shift mode towards convective transfer, slowing RoR for even development without rushing drop yet.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'In pre-first crack phase at 17s with bean temp 127°C and a stable environment around 212°C, full heat is needed to build temperature steadily. Fan is raised slightly from 0 to 10 to start mild convection, evacuate chaff, and prevent scorching while maintaining high heat input. No drop yet, focus is on steady ramp up to first crack.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — "Pre-first crack phase with bean temperature rising steadily at a reasonable RoR. Maintain full heat to ensure development progress, but introduce a small fan (10%) to begin controlling airflow and smoke buildup, preventing scorched flavors. Drop is not appropriate as development hasn't started and target temp is not reached."
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.8 — 'The roast is just entering development (2 seconds elapsed), with bean temp at 177C and bean RoR still relatively high (10 C/min). To stretch development time toward the 19.9% target, reduce heat from 100% to 60% to prevent too rapid RoR, and increase fan to 40% to shift heat mode more toward convective transfer, protecting flavor and smoke evacuation. Not yet ready to drop as development time is very short.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=55% fan=65% drop=False conf=0.9 — 'Development time is about 63 seconds over 559 seconds (around 11%), which is within the desired 10-20% range. Current bean temperature is 186°C, below the target drop temp of 194°C. Bean rate of rise (RoR) is moderately high at 8.7°C/min, so reducing heat from 70% to 55% and increasing fan from 50% to 65% will moderate the RoR and move toward more convective heat transfer to preserve flavor and stretch development time without risking stalling or baking.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=75% fan=20% drop=False conf=0.9 — 'In preheating phase with bean temp below the charge guidance range, apply moderate heat (75%) to raise temperature into the target range early. Fan at 20% to start airflow and chaff removal without cooling too much. No drop since roast has not started and first crack not detected.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — "Pre-first crack phase with a high RoR of 11.7°C/min indicates rapid heating. To avoid overshoot and provide stable, controlled progression, maintain full heat but introduce a small fan airflow (10%) to enhance convective heat transfer and prevent scorching. No indication to drop yet since first crack is not reached and development hasn't started."
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=70% fan=40% drop=False conf=0.9 — 'Development phase just started with a high RoR (10 C/min). To stretch development duration and avoid too rapid rise, reduce heat from 100% to 70% and increase fan to 40% to promote convective cooling and steady development without baking. Target development ratio is 17.7%, and temp 179 C is below 194.3 C guide. No drop now to allow proper development.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=15% fan=100% drop=False conf=0.85 — 'In development phase at 17.7% development target, bean temp is 188C and high RoR at 9C/min suggests slowing temperature rise to extend development duration. Current fan max at 100% is appropriate to promote convective heat and smoke evacuation. Reducing heat from 30% to 15% will help prevent overshoot, stretch development, and avoid pushing temperature too close to or beyond 194.3C target drop temp. Drop not yet recommended due to development time needed.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=90% fan=15% drop=False conf=0.9 — 'In the first seconds of roasting pre-first crack with bean temp at 117.3°C, increasing heat decisively to 90% anticipates thermal lag and promotes steady temperature rise. A moderate fan level of 15% balances convective heat transfer and smoke evacuation without cooling too much, encouraging even roast development. No drop yet as first crack not reached and temperature well below target.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'Bean temp is rising steadily before first crack with a healthy RoR of 14 C/min. Heat is maxed out, so keep it high to maintain strong development. Fan currently off, increasing fan slightly to 10% will help even heat and prevent scorching while not cooling too much. Not near development drop or first crack yet, so no drop.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=60% fan=40% drop=False conf=0.9 — 'Development phase just started with a very low development time of 1 second. Bean temperature is 173C, below the target drop of 194.7C. The RoR is moderately high at 11.3C/min, indicating fairly quick temperature increase. To stretch development time and avoid overshooting temperature, reduce heat significantly from 100% to 60% and increase fan from 20% to 40% to encourage convective heat transfer and smoke/chaff evacuation. This will help slow RoR and elongate development to target about 22% development ratio before dropping.'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=75% fan=50% drop=False conf=0.9 — 'The roast is in development phase with a 22% target development ratio, currently at about 15%, so development time should be extended. Bean temp is 185°C with RoR of 8°C/min, a bit high for development stage. To stretch development, reduce heat from 100% to 75% and increase fan from 30% to 50% to switch to more convective heat transfer. This balances heat and fan to slow the RoR while avoiding baking or stalling. Do not drop yet as development ratio target is not met and temperature is below 194.7°C.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=85% fan=20% drop=False conf=0.9 — "Preheating phase with bean temp at 112C and environment temp at 143.3C. Heat is set high at 85% to build temperature quickly toward charge guidance (180-200C). Fan at 20% to support even heat transfer and chaff management without overcooling. No drop yet, as we're pre-heating before charge."
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=95% fan=20% drop=False conf=0.9 — 'In pre-first crack phase with a high but manageable RoR (12.3 °C/min), maintain high heat but reduce slightly to avoid overshoot. Increase fan moderately to shift heat mode toward convective, promoting even roasting and avoiding scorching while controlling RoR. Stay well below the target drop temperature guide and focus on development duration rather than temperature. No drop yet, development phase not started.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=80% fan=60% drop=False conf=0.9 — 'Beans are just entering development with a very short development time and current bean temp 179°C below the 195°C target drop temp. RoR is relatively high (9°C/min), so reducing heat from 100% to 80% and increasing fan from 30% to 60% will stretch development to reach the target development ratio (~13.4%) without pushing temperature too fast. This coordinated adjustment balances heat input and airflow to avoid baking and allows clean convective heat transfer.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=50% fan=90% drop=False conf=0.9 — 'Development phase bean temp is 190°C with RoR at 11°C/min, a bit high. Development time is about 9.6% (63/659), slightly under target 13.4%. To stretch development and reduce RoR, decrease heat to 50% and increase fan to 90% for more convective cooling, preventing baking and encouraging even development. Do not drop yet as development ratio not met.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=90% fan=20% drop=False conf=0.9 — 'In preheating phase, the bean temperature is near the lower charge guidance (176°C vs 180-200°C guidance) and environment is at 206°C. Applying high heat (90%) will help reach the charge temperature quickly. Fan at 20% will provide some airflow to stabilize environment temperature and prepare for smoke evacuation, without cooling too much. No reason to drop roast yet since roast has not started.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=95% fan=15% drop=False conf=0.9 — "Pre-first crack phase with bean temp rising steadily and RoR at 12.7 C/min, which is moderate and stable. Heat at full initially is good, but can be slightly reduced to avoid overshoot. Fan can be modestly raised to 15% to enhance convective heat transfer and smoke evacuation without cooling too much. Not time to drop as first crack hasn't started and development time is not applicable yet."
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=50% fan=50% drop=False conf=0.9 — "In development phase with only 12 seconds elapsed and a target development percent of 25.5%, it's too early to drop. The bean RoR is moderate (10 C/min) but we want to slow it to stretch development. Reducing heat to 50% and increasing fan to 50% shifts to more convective cooling and controls RoR without stalling. This balances heat and fan decisively to meet the development duration target while avoiding over-darkening the beans."
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=55% fan=85% drop=False conf=0.9 — 'Development phase shows development time is 102s on a total of 534s, about 19%, close to target 25.5%. Bean temp at 188C is below target drop 195C and ROR is relatively high at 10.7C/min. To extend development gently, reduce heat to 55% to slow RoR but increase fan to 85% to shift heat mode toward convective, protecting flavor and smoke. Not ready to drop yet given development ratio and temperature margin.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=20% drop=False conf=0.9 — 'In preheating phase with bean temp at 119C and environment at 156C, set heat high (100) to quickly reach charge temperature range (180-200C). Fan at moderate 20 to promote convective heat and prevent uneven heat without overcooling. No drop yet as roast is just starting.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=85% fan=65% drop=False conf=0.9 — 'Pre-first crack phase with bean temp 140C and high RoR (11.3 C/min) indicates heat is still driving temperature rise strongly. Reducing heat from 100% to 85% to prevent overheating but maintaining substantial heat input. Fan reduced from 100% to 65% to balance airflow and favor conductive/radiant heat while evacuating smoke. Not close to target drop temperature or development phase, so no drop yet.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'Development phase just started (2 seconds elapsed) with a current development ratio still very low. Bean temp is 181C, below the target drop of 195.3C, and RoR is moderate (10 C/min). To stretch development time toward the 13.6% target, heat is reduced from max to 60% and fan increased to 60% to shift to more convective transfer, preventing baking and controlling RoR while allowing gradual development without rushing drop.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=55% fan=90% drop=False conf=0.9 — 'Development phase with elapsed development time 62s vs total 684s gives development ratio ~9%. RoR of beans is high (9.7 C/min), current bean temp 190.7 C approaching target drop 195.3 C. To elongate development phase and achieve target development ratio (~13.6%), reduce heat significantly from 80% to 55%, increase fan from 80% to 90% for more convective cooling. This balances heat and fan to slow RoR without stalling roast or baking flavor. Dropping now is premature due to development ratio under target.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'Pre-first crack phase with bean temperature stable around 90C and environmental temperature rising steadily. Maintaining full heat is needed to keep the roast progressing. Fan increased slightly to 10% to improve convective heat transfer and airflow to manage smoke and chaff, without cooling excessively. No drop needed as first crack not detected and development phase not started.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=85% fan=20% drop=False conf=0.9 — 'In pre-first crack phase with current bean temp 137C and RoR 10.7 C/min, reduce heat from 100% to 85% to avoid overshoot due to thermal lag, while increasing fan modestly to 20% to introduce some convective heat transfer and smoke evacuation. This balances heat and fan to maintain steady bean temperature rise without rushing development. No drop is due since development has not started and target drop temperature is still far.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=90% fan=15% drop=False conf=0.9 — 'Pre-first crack at 179 C and RoR 10.3 C/min, keep high heat but slightly reduce from max to prevent overshoot. Increase fan slightly to introduce more convective heat and control environment temperature, preventing scorching. Not near drop temp or development phase, so no drop.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=75% fan=60% drop=False conf=0.9 — 'In development phase at 680s with 54s development time (about 7.9% development ratio, below target 12.4%). Beans are at 189C, below the drop temp of 195.3C. RoR is moderately high (10 C/min). To slow RoR and extend development duration, reduce heat from 100% to 75% and increase fan from 40% to 60% to increase convective heat transfer and protect flavor while managing temperature rise. No need to drop yet as the development ratio target is not met.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=90% fan=15% drop=False conf=0.8 — 'Roasting is in pre-first crack phase with bean temp at 97°C. Recent data shows bean temperature slightly dropping despite 100% heat at low fan. To build heat energy early and steadily, reduce heat slightly from max to avoid overshoot and raise fan modestly to improve convective heat and smoke clearance without stalling the roast. No drop advised since development is not yet started.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=90% fan=20% drop=False conf=0.9 — 'Pre-first crack phase with bean temp rising steadily and a high RoR. Lowering heat slightly and increasing fan to 20 to shift toward more convective heat, control RoR, avoid overshoot, and ensure good flavor development. No drop yet as first crack not detected and development time undefined.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=60% fan=60% drop=False conf=0.9 — 'Development just started with a very low development time (6s) and a high RoR (~9.8 C/min). To stretch development duration toward the target (19.6%) and avoid rushing the drop near temperature guide (196C), reduce heat decisively from 80% to 60% and increase fan from 50% to 60%. This coordinated move will lower RoR, balance heat and airflow for even convective transfer, and extend development time without baking or stalling.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=30% fan=90% drop=False conf=0.9 — 'In development phase with a high bean RoR (9.0 C/min) and the development ratio below target (19.6%), reduce heat substantially to slow rate and increase fan to 90% to shift to convective heat for even development and to protect flavors. Not yet time to drop as target development ratio is not fully reached and bean temp is below target drop temp of 196 C.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'Bean temperature is still low (85C) early in roasting and the rate of rise is negative, likely due to initial thermal lag or cooling. To anticipate first crack and accelerate temperature rise, keep heat at max and introduce moderate fan (20%) to encourage convective heat transfer and stabilize environment temp (148C) without cooling too much. No drop yet, we are pre-first crack and need to build temperature and RoR.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=90% fan=10% drop=False conf=0.9 — 'Pre-first crack phase with bean temp rising steadily but moderate RoR of 13°C/min suggests maintaining high heat to push toward first crack, but slightly reduced from max to avoid overshoot. Fan increased modestly to 10% to improve convective heat transfer and smoke evacuation without cooling too much. No drop now, development phase not started and target drop temp not yet reached.'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=65% fan=50% drop=False conf=0.9 — 'In early development (3 seconds post first crack) with bean temp 178C and RoR 10 C/min, reduction of heat from max to 65% to lower RoR is prudent. Increase fan to 50% to shift heat mode to convective and control development without stalling. Development ratio is very low (about 0.5%) vs target ~16.5%, so do not drop yet.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=45% fan=95% drop=False conf=0.9 — 'In development phase with 16.5% target development, current development is around 9.7% (63/652), slightly below target. Bean temp 187°C with ROR 9.0°C/min is somewhat high. To stretch development and avoid pushing temperature past 196°C, reduce heat significantly and increase fan to shift heat transfer to convection and lower ROR while extending development time.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.9 — "In the preheating phase with bean temp at 117 C and environment at 139 C, the heat should be set strong and fan off to rapidly and efficiently bring the drum and beans to charge temperature range (180-200 C). No drop needed as roasting hasn't started."
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'Pre-first crack phase with bean temperature at 137°C and RoR at 11°C/min indicates stable, efficient heat input. Increase fan from 10% to 20% to enhance convective heat transfer, prevent scorching, and ensure even development. Maintain full heat to continue stable rise toward first crack, without dropping yet.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=85% fan=30% drop=False conf=0.9 — 'Development has just started (3 seconds), with a target development percentage of 14.6%. Bean temperature is 179.7 C and rising steadily with high RoR (10.7 C/min). To avoid pushing temperature too fast and risking scorched flavor, reduce heat moderately from full and increase fan to improve convective heat and control RoR. No drop yet, focus on stretching development time towards target ratio.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=55% fan=85% drop=False conf=0.9 — 'In development phase with 14.6% development ratio aiming for around 10-20%. Current bean temp 189C is close to but below target drop temp 196.3C. RoR is moderate at 9.3C/min but recent heat was reduced from 70% to 60%. To avoid overshooting temperature while extending development, further reduce heat modestly to 55% and increase fan slightly to 85% for more convective heat transfer and smoke evacuation. This balances slowing RoR without stalling development. Not ready to drop yet as duration target not reached.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=15% drop=False conf=0.9 — "Bean temperature is decreasing with a strong negative RoR, indicating heat is not effectively transferring to beans despite high 100% heat level. Slightly increase fan from 10% to 15% to enhance convective heat transfer and improve temperature rise. Maintain full heat to anticipate thermal lag and avoid stalling the roast pre-first crack. No drop, as it's early in roast and first crack not detected."
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=85% fan=25% drop=False conf=0.9 — 'Pre-first crack phase, bean temp rising steadily with RoR around 10.3°C/min. To continue smooth rise without overshoot, reduce heat moderately from max to 85 and increase fan to 25 to enhance convective heat transfer and smoke evacuation. Avoid rushing drop as development has not started.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=85% fan=40% drop=False conf=0.9 — 'Development just started (4 sec) with a target development of 12.7%. Bean temp is 178C, below the drop guide (196.3C), with a high RoR (11.3 C/min). To stretch development duration, reduce heat decisively and increase fan to shift heat transfer mode toward convection, slowing RoR while preventing baking. Not ready to drop yet given short development time.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=35% fan=90% drop=False conf=0.9 — "Current development time is about 10% of total roast time, hitting the target development percent. Bean temperature is near but below the drop guide. RoR is moderate, but since we're early in development, reduce heat substantially and raise fan for convective transfer to extend development duration without overheating. Not yet time to drop."
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=80% fan=15% drop=False conf=0.9 — "In preheating phase with bean temp at 99C and environment at 130C, set heat to moderately high (80) to bring temperature into charge range (180-200C) efficiently. Fan at low-medium (15) to start airflow and control smoke without cooling too much. Not time to drop since charge hasn't occurred and development hasn't started."
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.9 — 'Bean temperature is 125.3°C in pre-first crack phase with a strong RoR of 14.3°C/min. Heating remains at max power to maintain momentum, but fan raised to 15% to introduce more convective airflow for even heat and smoke management. Development phase not reached, so no drop yet.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=85% fan=40% drop=False conf=0.9 — 'In pre-first crack phase with bean temp rising at 12 C/min, slightly reduce heat from 100% to 85% to avoid overshoot due to thermal lag, and increase fan from 30% to 40% to encourage convective heat transfer and prevent scorching. Not near drop temp or development phase yet, so no drop recommended.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=60% fan=50% drop=False conf=0.9 — 'Development ratio is about 14.3%, in the target 10-20% range, so do not drop yet. Bean RoR is moderately high at 12 C/min, indicating development is progressing quickly. To slow RoR and extend development without overshooting temperature, reduce heat from 90 to 60 and increase fan from 30 to 50 for more convective cooling and flavor protection. This coordinated change balances heat and fan to avoid stalling while controlling RoR.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=85% fan=15% drop=False conf=0.9 — 'Preheating phase with bean temp at 124C and heating off; ramp heat decisively to 85% to push temp into charge guidance range (180-200C). Fan at 15% to introduce slight airflow for convective heat without cooling too much. No drop - roast just started.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'In pre-first crack phase with moderate bean RoR (9.3 C/min) and steady temps. Maintain full heat to keep energy input, but introduce a low fan (10%) to start gently increasing convective airflow for even heat distribution and smoke removal. No drop yet; still early in roast and well below target drop temp. Heat and fan balanced to avoid stalling RoR.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=60% fan=60% drop=False conf=0.9 — 'In development at 19s with 14% target development, bean temp 182C rising with RoR 11.7C/min. Heat was recently 70-80% and fan 40%. To stretch development and reduce a high RoR, drop heat from 70% to 60% and increase fan from 40% to 60% for better convective cooling and controlled, even roast progression without rushing drop.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=10% fan=80% drop=False conf=0.9 — 'Currently in development phase with development time at 49s, about 7% of total roast time, below target 14%. Bean temp 189C is close to target 197C but priority is development ratio, not temperature alone. RoR is moderately high, so reduce heat to 10% and increase fan to 80% for convective transfer to stretch development time and avoid overshoot in temperature while controlling RoR.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=15% drop=False conf=0.9 — 'Bean temperature is dropping rapidly, indicating heat insufficiency or heat transfer lag. To counter this, maintain full heat to provide energy into the drum and increase fan moderately from 10% to 15% to improve convective heat transfer and stabilize environment temp while protecting flavor. It is still pre-first crack, so dropping is not appropriate. Early decisive action is needed to prevent further temperature drop and maintain roast progress.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'In pre-first crack phase with bean temp rising steadily at 13.3 C/min, keep max heat for energy input to maintain strong RoR. Fan raised slightly to 10 to start moderating environment, aiding heat transfer and chimney effect without cooling beans excessively. No drop as development phase not started and target drop temp not near.'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=50% fan=45% drop=False conf=0.9 — 'Development phase just started (6s elapsed) with a high RoR (11°C/min). To extend development duration and avoid overshooting the target drop temperature, reduce heat significantly from 100% to 50% and increase fan to 45% to enhance convective cooling and moderate RoR. Development ratio is far below target, so no drop yet.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=80% fan=70% drop=False conf=0.9 — 'Development time is 66s with a target of 20.5%, indicating more development is needed. Bean temp at 186C with RoR 10C/min high; reducing heat to 80% and increasing fan to 70% to shift heat mode to more convective, control RoR, and stretch development without overshooting temperature 197C. Not ready to drop yet.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.9 — "Early in the roast before first crack with bean temp rising steadily. Maintain max heat (100) to build energy into the drum. Introduce modest fan (15) to shift airflow mode toward convective heat, help even roasting and manage chaff and smoke. No drop advised as development hasn't started and temperature is well below target."
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.9 — 'Pre first crack stage with bean temp at 132.3 C and RoR 12.3 C/min indicates a strong temperature rise. Maintain max heat to continue progress but introduce modest fan (15%) to start increasing convective heat and evacuate smoke, preventing scorching. Not near target drop temp and development not started, so no drop now.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is in early development (4s) with a low development ratio relative to the target (~0.7%). The bean temperature is 178C with a high RoR (10.7C/min), close to but below the drop guide of 197C. To stretch development and avoid overshooting temperature while managing the high RoR, reduce heat substantially from 100% to 60% and increase fan to 60% to shift toward convective transfer. This will slow rate of rise while maintaining even heat and avoiding stalling or baking, supporting the target development duration and flavor. No drop yet as development time and ratio are too low.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.85 — 'Development is ongoing with 10.3% ratio but RoR is quite high at 9 C/min. To extend development and control rate, reduce heat substantially from 80% to 60% and raise fan to 70% to shift to convective heat and avoid baking while managing flavor. Drop is not yet recommended since development ratio is below target 17.8% and bean temp 187C is below temperature guide of 197C.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=15% drop=False conf=0.9 — 'Pre-first crack, bean temperature is dropping with a negative RoR. Environment temperature is stable and high. To resume temperature rise decisively, maintain full heat and slightly increase fan to 15% to enhance convective heat transfer and prevent scorching while encouraging a stable rise in bean temp toward first crack.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=25% drop=False conf=0.9 — "Pre-first crack phase with bean temperature rising steadily and a high bean RoR (~10.3°C/min). Maintain full heat to continue driving the roast with a slight increase in fan to 25% to stabilize temperature and prevent scorching. No drop advised since first crack hasn't started and development hasn't begun."
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=70% fan=70% drop=False conf=0.9 — 'The beans are in development phase but only 12 seconds have elapsed, which is very early. The bean temperature is approaching the target drop temp but we should prioritize stretching the development ratio near 13.6%. The bean rate of rise is moderately high at 9.7 C/min; to control this and avoid overdevelopment, reduce heat from 90 to 70% and increase fan to 70% to shift to more convective heat and evacuate smoke, slowing the roasting while protecting flavor.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=60% fan=90% drop=False conf=0.9 — 'Development time is about 10% of total roast time (72/710 ~ 10.1%), which is at the lower end of target; bean temp is close to target_drop_temp but RoR is still relatively high (10.3). To stretch development with rising RoR, reduce heat moderately and increase fan toward convective transfer to prevent stalling or baking, aiming to stabilize and gently prolong development without rushing drop.'

### meta-llama/llama-3.3-70b-instruct (speed-and-power)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=16.82 dir=0.81; fan MAE=6.36 dir=0.905; latency pre=2.17s preFC=2.21s FC=2.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=16.4 dir=0.833; fan MAE=2.4 dir=0.875; latency pre=2.0s preFC=2.18s FC=2.77s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.833 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.85 dir=0.769; fan MAE=5.56 dir=0.923; latency pre=3.6s preFC=2.35s FC=3.75s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    17 |     3 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.769 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.86 dir=0.9; fan MAE=2.38 dir=1.0; latency pre=2.58s preFC=2.24s FC=3.41s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    14 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=13.7 dir=0.885; fan MAE=7.22 dir=0.846; latency pre=2.09s preFC=2.52s FC=3.71s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.885 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=20 dir=0.783; fan MAE=3.96 dir=0.957; latency pre=3.04s preFC=2.74s FC=3.23s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.38 dir=0.85; fan MAE=7.62 dir=0.85; latency pre=2.51s preFC=3.06s FC=3.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=9.52 dir=0.95; fan MAE=1.43 dir=1.0; latency pre=2.3s preFC=2.71s FC=4.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.95 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.48 dir=0.85; fan MAE=4.29 dir=0.85; latency pre=2.77s preFC=2.75s FC=2.04s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=20 dir=0.591; fan MAE=10.43 dir=0.636; latency pre=1.56s preFC=1.88s FC=1.94s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=22     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     9 |    10 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.591 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10 dir=0.857; fan MAE=3.18 dir=0.905; latency pre=1.98s preFC=1.95s FC=2.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=31.6 dir=0.583; fan MAE=16.8 dir=0.583; latency pre=1.48s preFC=1.56s FC=1.76s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    11 |     5 |
    |        raise |     0 |     1 |     1 |
    (n=24; diagonal agreement=0.583 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.09 dir=0.81; fan MAE=13.18 dir=0.667; latency pre=2.25s preFC=1.63s FC=1.68s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    13 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.55 dir=0.762; fan MAE=8.18 dir=0.762; latency pre=1.46s preFC=1.73s FC=1.8s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    12 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=21; diagonal agreement=0.762 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=22.92 dir=0.609; fan MAE=12.92 dir=0.652; latency pre=1.42s preFC=1.9s FC=1.9s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     8 |    11 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.609 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15.2 dir=0.792; fan MAE=8.2 dir=0.667; latency pre=2.0s preFC=1.72s FC=1.68s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     4 |    15 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.792 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=16.19 dir=0.75; fan MAE=6.43 dir=0.75; latency pre=1.33s preFC=4.1s FC=1.77s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     5 |    11 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.75 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15 dir=0.6; fan MAE=7.31 dir=0.76; latency pre=1.97s preFC=2.34s FC=2.22s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    10 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.6 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=17.31 dir=0.64; fan MAE=8.46 dir=0.68; latency pre=2.51s preFC=1.78s FC=1.68s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     9 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.64 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.33 dir=0.783; fan MAE=4.38 dir=0.826; latency pre=2.05s preFC=1.8s FC=1.8s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     1 |     1 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.62 dir=0.56; fan MAE=11.73 dir=0.64; latency pre=2.04s preFC=1.64s FC=2.04s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     9 |    12 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.56 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=11.15 dir=0.84; fan MAE=5.58 dir=0.84; latency pre=3.96s preFC=1.72s FC=1.86s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     4 |    16 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=28.06 dir=0.7; fan MAE=8.06 dir=0.6; latency pre=1.74s preFC=1.77s FC=3.61s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    18 |     6 |
    |        raise |     0 |     1 |     1 |
    (n=30; diagonal agreement=0.7 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=16.25 dir=0.826; fan MAE=7.92 dir=0.739; latency pre=2.27s preFC=1.67s FC=1.74s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    15 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15.89 dir=0.852; fan MAE=2.86 dir=0.926; latency pre=2.27s preFC=1.58s FC=1.81s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    19 |     2 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.852 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=35 dir=0.37; fan MAE=17.5 dir=0.407; latency pre=1.59s preFC=1.62s FC=1.73s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |    12 |     8 |     4 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.37 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.58 dir=0.696; fan MAE=16.25 dir=0.652; latency pre=2.2s preFC=1.65s FC=1.49s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     7 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.696 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.15 dir=0.731; fan MAE=4.81 dir=0.731; latency pre=1.68s preFC=1.68s FC=2.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    16 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.731 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio target not yet reached, continue roasting with balanced heat and fan'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is 11.95%, which is within the target range of 10-20%. Bean temperature is 181.0 C, which is below the target drop temperature of 189.0 C. Recent RoR is 8.0 C/min, which is moderate. Reducing heat and increasing fan to stretch development and prevent over-roasting.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature and promote even roasting'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=80% fan=40% drop=False conf=0.8 — 'Pre-first crack, moderate heat and fan to maintain even temperature and prevent scorching.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is 13.2%, which is below the target of 17.9%. To stretch development, cut heat substantially and raise fan toward convective transfer.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and low fan to maintain temperature and promote even roasting.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio target not yet reached, continuing roast'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=50% fan=100% drop=False conf=0.8 — 'development ratio is 12.5%, which is within the target range of 10-20%. current_bean_temp_c is 183.3, which is below the target_drop_temp_c of 190.0. bean_ror_c_per_min is 8.6, which is high. To stretch development, cut heat substantially and raise fan toward convective transfer.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, high heat and low fan to maintain temperature and promote even roasting'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and low fan to continue temperature rise.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=30% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development and hit the target development ratio.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and low fan to maintain temperature and promote even roasting.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is 0.8%, which is below the target range of 10-20%, so we need to stretch development by cutting heat and raising fan'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=0% fan=100% drop=False conf=0.8 — 'development ratio is 13.4%, which is within the target range of 10-20%. Bean temperature is 183.0 C, which is below the target drop temperature of 191.7 C. Recent telemetry samples show a stable bean temperature and a high heat level, so we reduce heat and increase fan to maintain a balanced heat transfer and prevent over-roasting.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is low, increase fan to promote convective heat transfer and reduce heat to prevent over-roasting'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=50% fan=75% drop=False conf=0.8 — 'development ratio is 12.1%, which is within the target range of 10-20%. Bean temperature is 183.0 C, which is below the target drop temperature of 192.7 C. Recent telemetry samples show a stable bean temperature and a moderate rate of rise.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=50% fan=50% drop=False conf=0.8 — 'development ratio target not yet reached, continuing roast'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=30% fan=80% drop=False conf=0.8 — 'development ratio is 12.2%, which is within the target range of 10-20%. current_bean_temp_c is 187.7, which is below the target_drop_temp_c of 193.0. bean_ror_c_per_min is 9.7, which is relatively high. to stretch development, cut heat substantially and raise fan toward convective transfer.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and low fan to promote radiant and conductive heating.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=20% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and moderate fan to continue temperature rise.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=30% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development and hit the target development ratio.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=80% fan=40% drop=False conf=0.7 — 'Pre-first crack, moderate heat and increasing fan for even roast development'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, so continue development'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, so continue development'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Continue roasting with high heat and low fan to reach first crack'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=50% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=100% fan=50% drop=False conf=0.8 — 'Pre-first crack, increase heat to reach first crack and moderate fan to prevent overheating'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=30% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=80% fan=20% drop=False conf=0.7 — 'Pre-first crack, reduce heat and introduce convective heat transfer to prevent scorched flavor'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=50% fan=50% drop=False conf=0.8 — 'development ratio is low, increasing fan and decreasing heat to stretch development time'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is low, increasing fan to stretch development and prevent over-roasting'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=10% fan=100% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=100% fan=20% drop=False conf=0.8 — 'Pre-first crack phase, increasing heat to raise bean temperature'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature and promote even roasting'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is low, increasing fan to promote convective heat transfer and stretching development time'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.7 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=30% fan=100% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.8 — 'We are still in the roasting_pre_first_crack phase and the bean temperature is rising steadily, so we should maintain high heat and increase the fan to promote convective heat transfer and prevent scorched flavor.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=50% fan=80% drop=False conf=0.8 — 'The roast is in development phase and the current bean temperature is 188.0 C. The target drop temperature is 195.0 C, but the development ratio is the primary objective. To stretch development when post-crack RoR is high, cut heat substantially and raise fan toward convective transfer. The heat and fan levels are adjusted to achieve a balanced heat transfer and protect flavor.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'preheating phase, increasing heat to reach target temperature'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=80% fan=80% drop=False conf=0.7 — 'We are still in the roasting_pre_first_crack phase and the bean temperature is 140.0 C, which is below the target drop temperature of 195.3 C. The recent telemetry samples show a consistent heat level and fan level, so we will maintain a moderate heat and fan level to continue the roasting process.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio target'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=50% fan=90% drop=False conf=0.8 — 'development ratio is within target range, so continue development with balanced heat and fan'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and low fan to promote even roasting.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature and promote even roasting'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Maintain high heat and low fan to continue roasting pre-first crack'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and low fan to continue temperature rise.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=50% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=30% fan=100% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=50% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=30% fan=95% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Continue roasting with high heat and low fan to reach first crack'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=50% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=50% fan=90% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and low fan to continue temperature rise.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio target'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=30% fan=80% drop=False conf=0.8 — 'development ratio is within target range, so continue development'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'preheating phase, increasing heat to reach target temperature'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.8 — 'Maintain current heat and fan settings to continue roasting pre-first crack'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=50% fan=60% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.8 — 'Preheating phase, increase heat to reach target temperature'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.8 — 'The development ratio is within the target range, and the bean temperature is still below the target drop temperature. To maintain the development ratio and prevent over-roasting, the heat is reduced and the fan is increased to promote convective heat transfer.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=5% fan=80% drop=False conf=0.7 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Maintain high heat and low fan to continue roasting pre-first crack'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, increase heat and introduce convective heat transfer with moderate fan to prevent stalling'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is low, increasing fan to promote convective heat transfer and stretching development time'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=50% fan=60% drop=False conf=0.8 — 'The roast is in development phase and the bean temperature is 186.0 C. The development ratio is 11.3% which is within the target range of 10-20%. The RoR is 10.0 C/min which is relatively high. To stretch the development, we reduce the heat to 50% and increase the fan to 60% to promote convective heat transfer.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.8 — 'Pre-first crack, high heat and no fan to maintain temperature'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=80% fan=20% drop=False conf=0.8 — 'Pre-first crack, moderate heat and low fan to maintain even temperature distribution.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio target'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, so continue development'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, high heat and low fan to maintain temperature and promote even roasting'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=20% drop=False conf=0.8 — 'Pre-first crack, high heat and moderate fan to maintain RoR'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is low, increase fan to stretch development and cut heat to prevent overheating'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio is within target range, no need to drop yet'

---

## Opus 3-roast spot-check (DTR-spanning subset)

Subset: artisan-09, artisan-17, artisan-28

# Advisor bake-off — real-roast replay scorecard (#172/#173, D20)

> **Read first — what these numbers mean.** The ground truth is a known-GOOD roast, *not* a provably optimal one. Every metric below measures **agreement with a known-good roast**, NOT absolute correctness: a capable model may legitimately differ from what the human did and still roast well, and high agreement is not proof of quality. Drop F1 = 1.0 means *matched this one good roast*, not *correct*. Use these as a quantitative aid to the operator's judgement (the advice samples + the latency gate), never a replacement for it.

Test set (known-good 7-Jun Hottop roasts): .artisan-fixtures/artisan-09, .artisan-fixtures/artisan-17, .artisan-fixtures/artisan-28
Drop = should_drop agreement over ticks (F1/precision/recall) + first-drop timing error (s and °C vs the real drop). Heat/Fan = MAE (percentage points) + directional agreement (did the model move the lever the way the human did). Latency = median per phase, FC tightest. NO auto-pick.

Confusion matrices below are derived purely from the per-tick replay data (no extra calls). The 2×2 drop matrix is consistent with the F1/P/R above but is heavily class-imbalanced — almost every tick is no-drop, so TN dominates; read it WITH the drop-timing error, never alone. The 3×3 heat-direction matrix (cut/hold/raise) is the more informative view of control behaviour and anticipatory-cut agreement.

## prompt_version = v2

### anthropic/claude-opus-4.8 (incumbent)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=16.76 dir=0.35; fan MAE=15.95 dir=0.15; latency pre=4.58s preFC=5.96s FC=5.64s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |     4 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.35 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.1 dir=0.35; fan MAE=10 dir=0.4; latency pre=6.04s preFC=5.79s FC=6.14s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |    13 |     3 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.35 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.48 dir=0.346; fan MAE=9.81 dir=0.385; latency pre=4.95s preFC=6.35s FC=5.62s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    15 |     6 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.346 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=25% drop=False conf=0.8 — "Preheating, about to charge. Bean temp 183 C sits within the charge window (180-200 C), so the drum is ready. Anticipating the big thermal mass drop when beans go in, apply full heat now to counter the charge crash and drive the early ramp, given the electric element's thermal lag. Keep fan modest (25) to retain energy in the drum during the early absorption phase while still moving some air for evenness and chaff. No drop — roast hasn't started."
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=15% drop=False conf=0.7 — 'Pre-first-crack at 143C bean, ~5 min in, with bean RoR ~11.7 C/min — healthy momentum but the curve is flattening (last few samples nearly stalled at 142-143C despite full heat). Env temp is also plateaued at 204C, suggesting the drum is near its delivery ceiling and convective evacuation is lacking with fan at 0. Keep heat at 100 to drive toward first crack and avoid an early stall, but bring fan up off zero (to ~15) to introduce some convective transfer, even out the heat, and start clearing chaff/smoke without crashing RoR. Far from drop temp (193C); continue developing toward first crack.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=85% fan=40% drop=False conf=0.78 — "Pre-first-crack at 176 C with bean RoR ~10.3 C/min — approaching FC (typically ~195-200 env/~196 bean for this profile, with FC commonly near 196 C bean, so we're roughly 1-2 min out). Heat has been pinned at 100% and bean temp just flattened over the last few samples, suggesting we're already loaded with energy that will surface via thermal lag. Backing heat off to 85% now anticipates that lag and prevents a RoR spike into first crack that would blow past control. Simultaneously nudging fan from 30 to 40 shifts toward convective transfer for a more even approach to FC and helps evacuate smoke/chaff as the beans start to crack. Not dropping — we are still pre-FC with no development underway. Keep watching for crack onset to then cut heat further and stretch development toward the ~15% target."
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=62% fan=62% drop=False conf=0.78 — "Development is only 52s in (~9.8% of 533s total), short of the ~15% target, so don't drop yet — keep stretching. Bean is 186 C with RoR ~10 C/min on 80% heat; at that pace we'd blow past 193 C and risk going too dark before hitting the duration target. Cut heat decisively to ~62% to tame RoR, and raise fan to ~62% to shift toward convective transfer for even, smoke-free development without crashing the roll. Reassess in ~30s; aim to drop around 80-90s development."
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=25% drop=False conf=0.8 — 'Preheating, about to charge — bean temp 176 C sits just below the 180-200 C charge window. Set heat to full to load energy into the drum ahead of the cold-bean charge, anticipating thermal lag so the system recovers from the post-charge temperature drop quickly. Keep fan moderate (25) to provide some convective evenness and chaff/smoke evacuation without crashing the turnaround — we want heat dominant early to drive momentum. Not dropping; the roast has not even started.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=85% fan=25% drop=False conf=0.72 — "Pre-first-crack at 138.7C, ~4.4 min in, bean RoR ~12.7 C/min — a healthy climb toward first crack (typically ~196-205C env / ~150-160C bean). Heat is pinned at 100% with fan very low at 10%, which risks scorching and uneven roasting from too much radiant/conductive drum heat. I'm easing heat to 85% to begin moderating the approach (anticipating thermal lag so we don't blow through first crack with a runaway RoR) while raising fan to 25% to shift toward convective transfer, even out heat, and start evacuating chaff/smoke. This keeps momentum into first crack without scorching. Not dropping — we're well before first crack and development hasn't begun."
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=75% fan=45% drop=False conf=0.78 — 'Just 12s into development (ratio ~2.7%), bean at 172C with RoR ~10 C/min. Heat is pinned at 100% — that risks a runaway as development progresses and the bean approaches the 195C guide too fast, compressing development time. Cut heat decisively to 75% to tame RoR while keeping forward momentum, and raise fan from 30 to 45 to shift toward convective transfer for even development, smoke/chaff evacuation, and flavor protection. The heat:fan balance stays warm enough to avoid a stall/bake. Hold for a longer develop window; do not drop yet — far below any reasonable duration target.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=55% fan=80% drop=False conf=0.78 — "Development is at 102s / 534s total = ~19% — already in the upper part of the target band, with bean temp 188 C nearing the 195 C guide and RoR still 10.7 C/min. I don't want to push much hotter or the beans risk going too dark. Cut heat from 70 to 55 to ease the RoR, and raise fan from 70 to 80 to shift toward even convective transfer, evacuate smoke/chaff, and protect flavor while stretching the final minute or so of development. Not dropping yet — let development build flavor toward ~20%+ ratio, then drop shortly. Watch that the heat:fan balance doesn't crash RoR and stall."
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.82 — 'Very early in the roast (~19s, BT 86C) right after charge. The bean temp is still falling toward the turning point — the negative RoR is the normal post-charge dip, not a problem. Keep heat at 100% to drive energy into the drum and get past the turning point briskly, and hold fan low at 10% to conserve heat and let the charge recover. Far from first crack and the drop target; no reason to change levers yet. Will reassess as BT bottoms out and RoR turns positive.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=85% fan=30% drop=False conf=0.7 — "Pre-first-crack at 138 C bean, ~6.3 min in, with bean RoR ~10 C/min — a healthy approach to first crack (expected near 196-200 C env / ~155-165 C bean). Heat has been pinned at 100% with fan only at 20%; that risks a runaway RoR into the crack and scorching from too-little convective transfer. I'm easing heat to 85% to start tempering the RoR ahead of first crack (anticipating thermal lag — act early) while nudging fan to 30% to shift toward convective heat, improve evenness, and clear chaff/smoke. Still plenty of momentum to reach the crack; no drop anywhere near warranted."
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=60% fan=70% drop=False conf=0.8 — "Just 12s into development (ratio ~1.8%) with bean temp 182.7C — far from a drop. We're well below the 197.3C guide and need to stretch development toward ~13.6% (roughly 90-100s post-crack). Heat is still pinned at 90% with bean RoR near 9.7 C/min; left unchecked we'd blow past the target too fast and risk a baked/dark finish. Cut heat decisively from 90 to 60 to tame the climb while keeping forward momentum, and raise fan from 60 to 70 to shift toward convective transfer for even development and smoke/chaff evacuation. This 60/70 balance keeps RoR positive without crashing it, buying the time needed to develop into the target ratio."
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=60% fan=100% drop=False conf=0.78 — "Development is 72s into a 710s roast = ~10.1%, just under the 13.6% target, so keep developing. Bean is at 193 C with RoR ~10.3 C/min and heat pinned at 100% — at this rate it will blow past the 197.3 C guide and risk going too dark before hitting the duration target. Cut heat decisively to 60% to flatten RoR while holding fan at 100% to maintain convective, even heat transfer and evacuate smoke, stretching development without baking. Reassess for drop as the ratio climbs toward ~13-14% and flavor develops; don't drop yet."