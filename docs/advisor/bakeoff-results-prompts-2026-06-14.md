# Advisor bake-off — prompt-tuning sweep (#194, drop-recall gap)

> Pinned model `google/gemini-3.1-flash-lite` (D33). This run tunes the PROMPT, not the model: v2 = baseline; v4-v8 keep v2's heat/fan control intact and change only the drop-decision guidance to close the ~0.64-recall gap (gemini misses the drop on ~10/28 roasts, leaning late). Agreement-with-the-operator is the target here, NOT proven optimality; precision matters as much as recall (an over-eager prompt racks up false positives = under-developed roasts). The variants are hand-authored against this same 28-roast set, so read for mild train-on-test bias.

## Per-prompt comparison (mean across 28 roasts)

| prompt | drop F1 | precision | recall | called drop on | FP roasts | dropΔ (s/°C) | heat-dir | heat MAE | fan-dir |
|---|---|---|---|---|---|---|---|---|---|
| **v2** (baseline) | 0.655 | 0.643 | 0.679 | 19/28 | 3 | -0.45/-0.065 | 0.887 | 9.036 | 0.533 |
| **v4** | 0.881 | 0.821 | 1.0 | 28/28 | 10 | -2.357/-0.268 | 0.851 | 10.266 | 0.455 |
| **v5** | 0.881 | 0.821 | 1.0 | 28/28 | 10 | -2.786/-0.318 | 0.826 | 10.3 | 0.437 |
| **v6** | 0.744 | 0.631 | 1.0 | 28/28 | 19 | -13.464/-2.125 | 0.902 | 9.383 | 0.541 |
| **v7** | 0.62 | 0.467 | 1.0 | 28/28 | 26 | -33.036/-5.207 | 0.818 | 16.056 | 0.443 |
| **v8** | 0.786 | 0.679 | 1.0 | 28/28 | 18 | -7.393/-1.1 | 0.911 | 7.449 | 0.558 |

## Recommendation — v4 (the lead reviews with the operator before re-pinning)

**v4 (profile-target anchor) is the recommended winner**, tied with v5 on the
headline but with better drop timing (-2.4 s vs -2.8 s) and a heat-direction
closer to v2's:

- **Closes the recall gap cleanly.** Every drop-recall variant raised recall
  from v2's 0.68 to **1.0** (drop called on **28/28** vs v2's 19/28 — zero
  misses). The whole point of the iteration is achieved by all five.
- **v4/v5 keep precision high.** v4 = precision **0.821**, F1 **0.881** — well
  above v2's 0.643 / 0.655. The only precision cost is a **single one-tick-early
  false positive** on 10/28 roasts (FP-ticks = FP-roasts = 10), i.e. the model
  signals drop one 30 s tick before the operator's tick — a near-miss, not an
  under-developed roast. Drop timing is essentially exact (-2.4 s / -0.27 °C).
- **Heat-direction barely moves.** v4 heat-dir **0.851** vs v2 0.887 (the
  anticipatory thermal-lag cut is preserved verbatim). v8 (0.911) and v6 (0.902)
  actually edge v2 on heat-dir, but lose badly on drop precision.
- **v4 vs v5:** identical F1/precision/recall; v4 wins on timing (-2.4 s vs
  -2.8 s) and heat-dir (0.851 vs 0.826). Pick v4; keep v5 as the close runner-up.

### Over-correction (the warned failure mode — do NOT pick these)

- **v7 (lag-anticipation) over-corrected hardest:** precision **0.467**, F1
  0.62 (BELOW v2), dropping **-33 s / -5.2 °C too early** on average, up to
  -97 s / -15 °C. "Trigger before the band" plus "the clock understates
  development" compounded into chronic early drops = under-developed roasts.
- **v6 (floor-biased window) also over-corrected:** precision 0.631 (≈ v2), F1
  0.744, -13 s / -2.1 °C early. The hard floor-bias ("when in doubt, drop") tips
  it too eager.
- **v8 (concise synthesis):** middle — F1 0.786, precision 0.679, -7.4 s early.
  Better than v6/v7 but more over-eager than v4/v5; brevity did not beat v4's
  profile-anchored framing here.

### Honest caveats

- **Train-on-test:** v4-v8 were hand-authored against this same 28-roast set, so
  the absolute F1/precision carry mild optimistic bias; the *ranking* (v4/v5 >
  v8 > v6 > v7, and all > v2 on recall) is the load-bearing result.
- **Agreement ≠ correctness:** a later or earlier drop than the operator may
  still roast fine. But the operator's band IS the target here, so the FP-tick
  on v4 (one tick = 30 s early) is a near-match, and v7's -90 s drops are a real
  regression, not a stylistic difference.
- **Read precision WITH recall.** Recall went to 1.0 for *every* variant; the
  discriminating axis is precision / timing, where v4 wins decisively.
- **Baseline shift:** v2 reads 19/28 (recall 0.679) here vs the prior run's
  18/28 (0.63) — the corrected `first_crack_temp_c` fixtures (see commit) plus
  run-to-run sampling variance; v2 is the same prompt, re-measured.

**Final run cost:** ~$1.86 (OpenRouter usage 16.413 → 18.274). The model is the
pinned, very cheap flash-lite; wall-time (~9 h) was rate-limit-bound, not cost.

---

# Advisor bake-off — real-roast replay scorecard (#172/#173, D20)

> **Read first — what these numbers mean.** The ground truth is a known-GOOD roast, *not* a provably optimal one. Every metric below measures **agreement with a known-good roast**, NOT absolute correctness: a capable model may legitimately differ from what the human did and still roast well, and high agreement is not proof of quality. Drop F1 = 1.0 means *matched this one good roast*, not *correct*. Use these as a quantitative aid to the operator's judgement (the advice samples + the latency gate), never a replacement for it.

Test set (known-good 7-Jun Hottop roasts): .artisan-fixtures/artisan-01, .artisan-fixtures/artisan-02, .artisan-fixtures/artisan-03, .artisan-fixtures/artisan-04, .artisan-fixtures/artisan-05, .artisan-fixtures/artisan-06, .artisan-fixtures/artisan-07, .artisan-fixtures/artisan-08, .artisan-fixtures/artisan-09, .artisan-fixtures/artisan-10, .artisan-fixtures/artisan-11, .artisan-fixtures/artisan-12, .artisan-fixtures/artisan-13, .artisan-fixtures/artisan-14, .artisan-fixtures/artisan-15, .artisan-fixtures/artisan-16, .artisan-fixtures/artisan-17, .artisan-fixtures/artisan-18, .artisan-fixtures/artisan-19, .artisan-fixtures/artisan-20, .artisan-fixtures/artisan-21, .artisan-fixtures/artisan-22, .artisan-fixtures/artisan-23, .artisan-fixtures/artisan-24, .artisan-fixtures/artisan-25, .artisan-fixtures/artisan-26, .artisan-fixtures/artisan-27, .artisan-fixtures/artisan-28
Drop = should_drop agreement over ticks (F1/precision/recall) + first-drop timing error (s and °C vs the real drop). Heat/Fan = MAE (percentage points) + directional agreement (did the model move the lever the way the human did). Latency = median per phase, FC tightest. NO auto-pick.

Confusion matrices below are derived purely from the per-tick replay data (no extra calls). The 2×2 drop matrix is consistent with the F1/P/R above but is heavily class-imbalanced — almost every tick is no-drop, so TN dominates; read it WITH the drop-timing error, never alone. The 3×3 heat-direction matrix (cut/hold/raise) is the more informative view of control behaviour and anticipatory-cut agreement.

## prompt_version = v2

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=10.91 dir=0.905; fan MAE=12.73 dir=0.524; latency pre=1.1s preFC=1.08s FC=1.16s
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
    |         hold |     1 |    16 |     0 |
    |        raise |     1 |     0 |     2 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.4 dir=0.875; fan MAE=12.4 dir=0.417; latency pre=1.31s preFC=1.1s FC=1.08s
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
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.78 dir=0.923; fan MAE=12.59 dir=0.577; latency pre=0.87s preFC=1.06s FC=1.16s
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
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=2.38 dir=0.95; fan MAE=7.38 dir=0.65; latency pre=0.95s preFC=1.04s FC=1.13s
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
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=10.74 dir=0.923; fan MAE=15.93 dir=0.346; latency pre=0.97s preFC=1.05s FC=1.08s
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
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-3.0s/-0.7°C; heat MAE=18.33 dir=0.783; fan MAE=11.25 dir=0.565; latency pre=0.86s preFC=1.09s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.24 dir=0.9; fan MAE=18.1 dir=0.35; latency pre=0.98s preFC=1.08s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=0 dir=1.0; fan MAE=6.67 dir=0.6; latency pre=0.91s preFC=1.04s FC=1.07s
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
    |         hold |     0 |    16 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=1.0 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.24 dir=0.9; fan MAE=10.95 dir=0.55; latency pre=0.88s preFC=1.07s FC=1.23s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=16.96 dir=0.818; fan MAE=13.48 dir=0.455; latency pre=0.98s preFC=1.03s FC=1.17s
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
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=8.18 dir=0.905; fan MAE=7.27 dir=0.619; latency pre=0.8s preFC=1.04s FC=1.13s
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
    |         hold |     2 |    16 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=16 dir=0.792; fan MAE=10 dir=0.625; latency pre=0.93s preFC=1.03s FC=1.11s
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
    |         hold |     2 |    16 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.792 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.73 dir=0.905; fan MAE=15.68 dir=0.429; latency pre=1.02s preFC=1.04s FC=1.02s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=2.73 dir=0.952; fan MAE=16.14 dir=0.333; latency pre=0.84s preFC=1.1s FC=1.06s
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
    |         hold |     1 |    16 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=21; diagonal agreement=0.952 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=17.92 dir=0.783; fan MAE=16.88 dir=0.348; latency pre=1.01s preFC=1.06s FC=1.03s
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
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=7.2 dir=0.917; fan MAE=6.6 dir=0.625; latency pre=0.93s preFC=1.02s FC=1.07s
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
    |         hold |     1 |    18 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.14 dir=0.85; fan MAE=6.67 dir=0.75; latency pre=0.89s preFC=1.03s FC=1.16s
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
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=5 dir=0.88; fan MAE=7.12 dir=0.88; latency pre=0.98s preFC=1.07s FC=1.33s
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
    |         hold |     3 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=-5.0s/-0.6°C; heat MAE=10.38 dir=0.8; fan MAE=17.31 dir=0.28; latency pre=0.9s preFC=1.09s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     5 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/+0.0°C; heat MAE=11.67 dir=0.913; fan MAE=5.83 dir=0.696; latency pre=0.94s preFC=1.07s FC=1.06s
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
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.54 dir=0.84; fan MAE=13.27 dir=0.52; latency pre=1.03s preFC=1.09s FC=1.22s
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
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.38 dir=0.92; fan MAE=7.5 dir=0.56; latency pre=1.12s preFC=1.06s FC=1.18s
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
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=7.1 dir=0.967; fan MAE=4.52 dir=0.767; latency pre=1.0s preFC=1.06s FC=1.12s
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
    |        raise |     0 |     0 |     2 |
    (n=30; diagonal agreement=0.967 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=15.42 dir=0.826; fan MAE=13.33 dir=0.435; latency pre=1.02s preFC=1.07s FC=1.21s
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
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=5.71 dir=0.963; fan MAE=10.89 dir=0.519; latency pre=0.95s preFC=1.05s FC=1.14s
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
    |         hold |     1 |    21 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=27; diagonal agreement=0.963 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.86 dir=0.889; fan MAE=12.5 dir=0.519; latency pre=0.93s preFC=1.04s FC=1.09s
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
    |         hold |     3 |    21 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=27; diagonal agreement=0.889 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.75 dir=0.87; fan MAE=20.42 dir=0.304; latency pre=0.95s preFC=1.12s FC=1.1s
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
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=13.33 dir=0.885; fan MAE=5.74 dir=0.692; latency pre=0.92s preFC=1.05s FC=1.26s
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
    |         hold |     2 |    19 |     1 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.885 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 160°C and rising. I am setting heat to 100% to reach the target charge temperature range (180-200°C) efficiently. Fan is kept at 0% to allow the drum to soak heat effectively before charging.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 131°C. With heat at 100% and fan at 0%, the RoR is healthy (14.6°C/min). I am maintaining full heat to ensure momentum toward first crack, but I will introduce a small amount of fan (20%) now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the drying/Maillard transition.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 494s. Development is very early (7s). RoR is 11C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% and increasing fan to 60% to transition to convective heat, ensuring a controlled development phase toward the 20% target.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=80% fan=60% drop=False conf=0.9 — 'Development time is 67s out of 561s total (11.9%). To reach the 20.5% target, I need ~115s total development time (approx 48s more). RoR is 8C/min, which is healthy. I will maintain current settings to allow a steady, controlled development without stalling, as the RoR is not excessive. I will monitor closely to drop near the 20% mark.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 121°C, which is below the target charge range of 180°C-200°C. I will maintain zero heat and fan to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 135C with no fan. To prepare for the approach to first crack and ensure even heat distribution, I am increasing the fan to 25% to introduce convective heat. I am maintaining 100% heat to keep the RoR healthy as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=90% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 10.7 C/min. Maintaining current heat and fan settings to ensure momentum into the crack. Will prepare to adjust once first crack is confirmed to manage the development phase.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=60% fan=80% drop=False conf=0.9 — 'Development time is 86s out of 654s total (13.1%). To reach the 17.9% target, I need ~31s more development. RoR is 7.3 C/min, which is healthy. I will reduce heat to 60% to manage the momentum and increase fan to 80% to ensure convective heat and clean airflow for the final stretch. Drop when development time reaches ~117s.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (17 seconds elapsed is likely an error in the provided telemetry, but the bean temperature of 97C confirms early drying/yellowing phase). The RoR is negative, which is highly concerning. I am maintaining maximum heat to recover momentum and keeping the fan off to maximize conductive heat transfer and prevent further stalling.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 131°C. With a bean RoR of 12°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 542s. Development is currently 17s (approx 3% of total time). To reach a 19% development ratio, I need to extend development to ~130s total. The current RoR is 10C/min, which is healthy but needs to be managed to avoid overshooting the target temperature too quickly. I am reducing heat to 60% and increasing fan to 60% to shift toward convective heat and gently manage the RoR for a controlled development phase.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=100% fan=90% drop=False conf=0.9 — 'The roast is at 183.3°C with 77 seconds of development time (12.4% of total time). The RoR is 8.6°C/min, which is healthy. I need to maintain this momentum to reach the 19% development target (approx. 117 seconds total development time). I will keep heat high to ensure steady development and maintain the fan at 90% for convective efficiency. I am not ready to drop yet.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is within the target charge range (180-200C). I am maintaining zero heat and fan to allow the drum to stabilize at the target charge temperature before the roast begins.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — "The roast is progressing steadily toward first crack. With an RoR of ~13.5 C/min, we have good momentum. I am maintaining full heat to ensure we don't stall before first crack, while keeping the fan low to build sufficient energy for the development phase. No changes needed yet."
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 11C/min. Maintaining full heat to ensure momentum through the crack, but increasing fan to 30% to prepare for the exothermic phase and improve convective heat transfer as we approach the development stage.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=30% fan=75% drop=False conf=0.9 — 'Development time is 52s out of 502s total (10.3%). To reach the 15.7% target, I need ~27s more development. RoR is stable at 10C/min. Reducing heat slightly and increasing fan will gently stretch the development phase while ensuring convective heat transfer to avoid baking. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156°C, which is below the target charge range (180°C-200°C). I am setting heat to 100% to accelerate the preheating process to reach the target charge temperature. Fan is kept at 0% to allow the drum to heat up efficiently.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the drying/early Maillard phase. With an RoR of 11.7 C/min and no fan, the environment is building heat effectively. I will maintain full heat to ensure momentum toward first crack, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the crack.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just occurred (5 seconds ago). The RoR is currently 9.7 C/min, which is healthy but needs to be managed to ensure a smooth development phase. I am reducing heat to 70% to prevent a spike and increasing fan to 50% to transition toward convective heat transfer, which will help maintain a steady, controlled development. The goal is to stretch the development time to reach the 19.5% target ratio.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=60% fan=70% drop=False conf=0.95 — 'Development is at 13.4% (95s / 711s). To reach the 19.5% target, I need approximately 45-50 more seconds of development. The RoR is stable at 6C/min. I will reduce heat to 60% to prevent a spike and increase fan to 70% to shift toward convective heat, ensuring a smooth, controlled finish without stalling. Drop when development time reaches ~140-145s.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (9 seconds elapsed). The telemetry shows a rapid drop in bean temperature, likely due to the initial charge and sensor stabilization. Maintaining 100% heat is appropriate to recover momentum, and keeping the fan at 0% helps build the necessary thermal energy in the drum for the drying phase.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily at 134°C. With heat at 100% and fan at 0%, the RoR is healthy (14°C/min). I am maintaining full heat to ensure sufficient momentum approaching first crack, but I am introducing 20% fan now to begin establishing convective airflow and preparing for the upcoming transition, ensuring the environment doesn't become too stagnant."
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun (10s in). The RoR is healthy at 10C/min. To manage the development phase and avoid a runaway RoR, I am reducing heat to 60% and increasing fan to 60% to shift toward convective heat transfer and ensure smoke evacuation. This will help stretch the development time toward the target ratio without stalling the roast.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=60% fan=70% drop=False conf=0.9 — "Development time is 70s out of 581s total (12%). To reach the 20.7% target, I need ~120s total development time. With a current RoR of 7.3 C/min, I have room to coast. Reducing heat to 60% and increasing fan to 70% will gently manage the momentum, ensuring I don't hit the drop temp too quickly while maintaining convective airflow for the final stage."
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase, approaching the target charge temperature of 188°C. I am maintaining zero heat and fan to allow the drum to stabilize at the desired charge temperature before the roast begins.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'RoR is healthy (11.8 C/min) and the bean temperature is approaching the transition to the Maillard phase. I am increasing the fan to 25% to begin introducing convective heat and preparing for the smoke/chaff management needed as we approach first crack, while maintaining 100% heat to keep momentum steady.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just occurred (5 seconds ago). The RoR is currently 10 C/min, which is healthy but needs to be managed to ensure a smooth development phase. I am reducing heat to 60% to prevent a spike and increasing fan to 40% to introduce convective heat and clear smoke, balancing the roast for a controlled development toward the 17.2% target.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.9 — 'Development time is 65s out of 532s total (12.2%). To reach the 17.2% target, I need ~26s more development. RoR is 9.7 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 193-194C in about 30 seconds.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The telemetry shows a significant drop in bean temperature (139C to 125C in 5 seconds), which is likely a sensor artifact or a very rapid adjustment. Given the heat is at 100% and fan is at 0%, I will maintain maximum heat to ensure momentum through the end of drying, but I will introduce a small amount of fan (20%) to begin clearing moisture and preparing for the transition to convective heat transfer.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=30% drop=False conf=0.9 — 'RoR has flattened significantly (12 C/min) while heat is at 100%. To maintain momentum toward first crack without stalling, I am maintaining heat but increasing fan to 30% to improve airflow and prepare for the upcoming exothermic phase.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=80% fan=40% drop=False conf=0.9 — 'Development is at 18s (approx 3.6% of total time). To reach a 16.6% development ratio (approx 96s total development), I need to extend the roast by about 78s. The current RoR is 10C/min, which is appropriate for a controlled development. I will maintain current heat and fan settings to keep the momentum steady without risking a stall or an overly aggressive finish.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=50% fan=70% drop=False conf=0.9 — 'Total roast time is 530s, development is 48s (approx 9%). To reach the 16.6% target, I need about 38 more seconds of development (total ~86s). RoR is 10.6 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 193-194C.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C). It is ready to be charged.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is beginning to decline. To prepare for first crack and ensure a smooth transition into development, I am increasing the fan to 25% to introduce convective heat and begin clearing moisture/chaff, while maintaining full heat to keep the momentum steady as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR. Maintaining 100% heat is appropriate to ensure sufficient momentum into the development phase. Keeping fan at 30% to maintain conductive heat transfer for now; will increase fan once first crack begins to manage convective heat and clear smoke.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.95 — 'Development time is 52s out of 533s total (9.7%). To reach the 15.3% target, I need ~30 more seconds of development. RoR is 10C/min, which is healthy. I will reduce heat to 60% to gently manage the momentum and increase fan to 70% to shift toward convective heat, ensuring a smooth finish without stalling. Drop target is 193C, but I will prioritize the development time.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (drying/yellowing). The bean temperature drop is likely a sensor stabilization issue following the charge. Maintaining maximum heat is appropriate to build momentum through the drying phase. Fan remains off to conserve energy and build drum heat.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'RoR is healthy (13.77 C/min) and the bean temperature is climbing steadily. I am maintaining full heat to ensure momentum toward first crack. Fan remains at 0% to maximize conductive heat transfer during this drying/early Maillard phase. No changes needed yet.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=70% fan=60% drop=False conf=0.9 — 'First crack occurred at 481s. Development is very early (14s). RoR is 10.3 C/min, which is healthy but needs to be managed to avoid overshooting the development target. Reducing heat to 70% and increasing fan to 60% will transition to convective heat, smoothing the RoR and ensuring a controlled development phase.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=60% fan=40% drop=False conf=0.9 — 'Development is currently at 17.7% (104s / 585s). The RoR is stable at 9C/min, and we are approaching the target drop temp. To reach the 24% development target, we need to extend the roast by about 40-50 seconds. I am reducing heat to 60% to prevent a spike and increasing fan to 40% to shift toward convective heat and ensure clean development. We are not ready to drop yet.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (168C to 151C in 5 seconds) despite 100% heat. This indicates a sensor error or a massive thermal stall. I am maintaining 100% heat to attempt to recover momentum and keeping fan at 0% to maximize conductive heat transfer. If the temperature continues to drop, the roast is likely compromised.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With a bean RoR of 12.3 C/min and the bean temperature at 142 C, I am maintaining full heat to ensure sufficient momentum through the Maillard phase. I am keeping the fan low (10%) to prioritize conductive heat transfer for now, but I will prepare to increase airflow as we approach first crack to manage smoke and transition to convective heat.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=60% fan=80% drop=False conf=0.9 — 'First crack occurred at 511s. With 13s of development, we are very early. RoR is 11 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to moderate the momentum and increasing fan to 80% to shift toward convective heat, ensuring even development and smoke evacuation as we approach the target drop.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=60% fan=80% drop=False conf=0.9 — 'Development time is 43s out of 554s total (7.7%). To reach a 14% development ratio, I need approximately 90s of development time total (47s more). The RoR is 10.3 C/min, which is healthy. I am reducing heat to 60% and increasing fan to 80% to manage the momentum and transition to convective heat for the final stretch, ensuring a smooth, controlled finish without stalling.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roast is approaching first crack (180C). Heat is currently off, which is appropriate to prevent a spike, but the RoR is still positive. I am keeping heat off to allow the momentum to carry the beans into first crack gently. Fan is off to maintain thermal energy for the transition, but I will need to engage it once first crack begins to manage smoke and convective development.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With a bean RoR of 12.3 C/min and bean temp at 141.3 C, I am maintaining full heat to ensure sufficient momentum for the upcoming crack, while keeping fan low to preserve conductive heat transfer. No changes needed yet.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just started (6s ago). RoR is healthy at 11C/min. To manage the development phase and avoid a runaway roast, I am cutting heat significantly to prevent scorching and increasing fan to 50% to shift toward convective heat transfer, ensuring even development. I will aim for a total roast time of ~550s to hit the 14.4% development target.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=30% fan=70% drop=False conf=0.9 — 'Development is at 36s (approx 7% of total time). To reach the 14.4% target, I need about 38 more seconds of development. RoR is stable at 10C/min. I will reduce heat to 30% to gently coast toward the drop, while increasing fan to 70% to shift to convective heat and ensure clean development as we approach the finish. Not ready to drop yet.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (17 seconds elapsed). The telemetry shows a rapid, likely erroneous drop in bean temperature (143C to 127C in 5 seconds), suggesting a sensor glitch or unstable reading. Given the heat is at 100% and fan is at 0%, I will maintain maximum heat to drive the roast forward while keeping the fan off to build momentum. I will monitor for more stable readings.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 140°C. With heat at 100% and fan at 0%, the RoR is healthy (12.7°C/min). I am maintaining full heat to ensure momentum toward first crack, but I will introduce a small amount of fan (20%) now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the drying/Maillard transition.'
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just begun. The RoR is healthy (10 C/min) and heat is at 100% with no fan. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and introducing airflow to shift toward convective heat transfer, ensuring a controlled development period.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=40% fan=70% drop=False conf=0.9 — 'Total roast time is ~622s, development time is 63s (10.1%). RoR is stable at 8.7C/min. To reach the 19.9% development target, I need ~60 more seconds of development. I will reduce heat to 40% to prevent a spike and increase fan to 70% to shift to convective heat, ensuring a gentle, controlled finish without stalling. Drop when development time reaches ~124s.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 219°C, which is within the appropriate range for charging. I am setting heat and fan to 0 to ensure the roaster is ready for the charge command.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'RoR is slowing significantly (11.7 C/min) while heat is at 100% and fan is at 0%. The roast is stalling before first crack. I need to maintain heat to push through the end of the drying/Maillard phase, but I will introduce a small amount of fan (20%) to improve airflow and prepare for the upcoming exothermic reaction.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'First crack occurred at 494s. Development is very early (8s). RoR is 10C/min, which is healthy but needs to be managed to avoid a runaway finish. I am cutting heat to 60% to manage the momentum and increasing fan to 40% to introduce convective heat and clear smoke, ensuring a controlled development phase.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=30% fan=100% drop=False conf=0.9 — 'The current development ratio is ~12.2% (69s / 563s). To reach the target of 17.7%, I need approximately 30 more seconds of development. The RoR is stable at 9C/min. I will maintain current heat and fan settings to allow a gentle, controlled finish toward the target development time without risking a crash or excessive temperature spike.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=80% fan=20% drop=False conf=0.9 — 'The roast has just begun (1 second elapsed). The heat and fan are currently at 0%, which is inappropriate for the drying/early ramp phase. I am initiating heat to 80% to establish momentum and setting the fan to 20% to begin clearing moisture and establishing airflow.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 130C. With an RoR of 14C/min, we are in a good position to approach first crack. Maintaining full heat is appropriate here to ensure sufficient momentum, but I am introducing 25% fan now to begin clearing moisture and preparing for the convective phase as we approach the Maillard reaction and first crack.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.95 — 'First crack has just begun. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and increasing airflow to transition to convective heat transfer. This will help control the development duration and ensure an even roast.'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is currently at 92 seconds of development (15.2% of total time). To reach the 22% development target, I need approximately 40-45 more seconds of development. The RoR is stable at 8C/min. I am reducing heat to 60% to prevent a temperature spike and increasing fan to 60% to shift toward convective heat, ensuring a controlled finish without stalling. I will monitor for the target development time before dropping.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is approaching the target charge range (180-200C). I am setting heat to 100% to ensure the drum reaches the desired thermal mass for charging, while keeping the fan off to maintain heat retention.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 137°C with a healthy RoR of 12.3°C/min. We are approaching the Maillard phase and first crack. Maintaining full heat (100%) is appropriate to keep momentum, but I am increasing the fan to 25% to begin clearing moisture and preparing for the transition to convective heat transfer as we approach first crack.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.95 — 'First crack has just begun (3 seconds in). The current heat (100%) is too high for the development phase and will cause an uncontrolled RoR spike. I am cutting heat significantly to 40% to manage the momentum and increasing fan to 60% to shift toward convective heat, ensuring even development and smoke evacuation. The goal is to stretch the development time to reach the 13.4% target ratio.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=60% fan=80% drop=False conf=0.95 — 'Total roast time is ~722s (659s + 63s dev). Current dev ratio is ~9.5%. To reach the 13.4% target, I need ~34 more seconds of development (total dev time ~97s). RoR is 11C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop in about 30-40 seconds.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is at 206°C, which is slightly above the charge guidance range (180-200°C). I am setting heat and fan to 0 to allow the environment temperature to stabilize within the target range before charging.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With an RoR of 12.7 C/min and bean temp at 138.7 C, I am maintaining full heat to ensure sufficient momentum for the Maillard phase. I am keeping the fan low (10%) to preserve drum heat and build energy, as we are still well before first crack. No changes needed yet.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred 12 seconds ago. The RoR is currently 10C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to moderate the energy input and increasing fan to 60% to shift toward convective heat and ensure smoke evacuation as development progresses. This will help achieve a controlled development duration.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=50% fan=80% drop=False conf=0.9 — 'Development time is currently 102s out of 534s total (19.1%). The target is 25.5%, which requires ~34s more development. RoR is 10.7 C/min, which is healthy but needs to be managed to avoid overshooting the target temperature too quickly. Reducing heat to 50% and increasing fan to 80% will gently taper the RoR to ensure a smooth finish. Drop when development time reaches ~136s.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156°C and rising toward the target charge range (180-200°C). No heat or fan is currently applied. I will maintain this state to allow the drum to continue soaking and reach the target charge temperature.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR has dropped significantly (11.3 C/min) while at maximum heat and fan. To ensure we have enough momentum to reach first crack without stalling, I am maintaining maximum heat. The fan is already at 100%, which is appropriate for this stage to manage moisture and prepare for the convective phase. No changes are needed yet.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just started. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and increasing fan to shift toward convective heat transfer. This will help stretch the development time to reach the 13.6% target ratio without overshooting the drop temperature too quickly.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=50% fan=90% drop=False conf=0.9 — 'The roast is at 62 seconds of development with a total time of 684 seconds (approx 9% development ratio). The RoR is 9.7 C/min, which is healthy but needs to be managed to reach the 13.6% target (approx 108 seconds total development). I am reducing heat to 50% to prevent a spike and increasing fan to 90% to shift toward convective heat and ensure clean development. The bean temp is 190.7 C, so we have room to develop for another 40-50 seconds.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early drying/yellowing phase. The bean temperature is dropping, likely due to the initial charge impact and the thermal lag of the drum. Heat is already at 100% to recover momentum. I will maintain full heat and keep the fan off to ensure maximum energy transfer into the beans during this early stage.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has flattened significantly (10.7 C/min and stalling at 137 C). With heat at 100% and fan at 0%, the roast is stalling due to lack of airflow and potential heat saturation. I am increasing fan to 25% to introduce convective heat and improve airflow, while maintaining 100% heat to push through the current plateau and build momentum toward first crack.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=70% fan=40% drop=False conf=0.9 — 'Approaching first crack with high heat and no fan. Need to prepare for the exothermic reaction by increasing airflow to manage smoke and transition to convective heat, while reducing heat to prevent an uncontrolled RoR spike during crack.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.95 — 'Current development time is 54s out of 680s total (7.9%). To reach the 12.4% target, I need ~30 more seconds of development. The RoR is stable at 10C/min. I will reduce heat to 60% to gently coast toward the finish and increase fan to 60% to improve convective airflow and manage the roast profile as we approach the drop. Drop should occur in approximately 30-40 seconds.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the very early stages (drying/yellowing). The bean temperature drop in the recent samples is likely a sensor artifact or a very brief stabilization period after charge. Maintaining full heat is appropriate to build momentum for the Maillard phase. Fan is kept low to retain heat in the drum.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — "The roast is progressing steadily toward first crack. With a bean RoR of 13.3 C/min and the bean temperature at 139 C, we are in a good position. I am maintaining full heat to ensure we don't stall before first crack, while keeping the fan low (10%) to build sufficient momentum for the development phase. No changes are needed yet."
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.9 — 'First crack occurred at 522s. Development is very early (6s). RoR is currently 9.8 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% and increasing fan to 70% to shift toward convective heat, ensuring a controlled, steady development toward the 19.6% target. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=30% fan=100% drop=False conf=0.95 — 'Current development time is 66s out of 588s total (11.2%). To reach the 19.6% target, I need ~115s total development time (approx 49s more). The RoR is stable at 9C/min. I will reduce heat to 30% to gently manage the momentum and increase fan to 100% to maximize convective airflow for the final stretch, ensuring a clean finish without stalling. Drop when development time reaches ~115s.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-27 C/min) despite 100% heat. This indicates the charge was likely too low or the environment temperature is insufficient to maintain momentum. I am maintaining 100% heat to recover the RoR and keeping the fan at 0% to maximize conductive heat transfer and prevent further stalling.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 135°C. With an RoR of 13°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the drying/Maillard transition.'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just occurred. The RoR is healthy (10 C/min) but heat is at 100%. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and increasing fan to shift toward convective heat, ensuring a controlled development period toward the 16.5% target.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=60% fan=90% drop=False conf=0.9 — 'Development time is 63s out of 715s total (8.8%). To reach the 16.5% target, I need ~118s total development time, meaning ~55s more. RoR is 9.0 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop in about 50-60 seconds.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 139C and rising toward the target charge range (180C-200C). No heat or fan is required yet as the machine continues to soak and stabilize.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has flattened significantly at 137C, suggesting the roast is stalling or entering a plateau. I need to maintain high heat to push through to first crack, but I will increase the fan slightly to improve airflow and ensure the bean mass is receiving consistent convective heat as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=60% fan=40% drop=False conf=0.9 — 'First crack just started (3 seconds ago). Heat is currently at 100% and fan at 10%. To manage the development phase and prevent a runaway RoR, I need to reduce heat and increase airflow to transition to convective heat transfer. Dropping heat to 60% and increasing fan to 40% will help control the momentum while aiming for the 14.6% development ratio target.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=50% fan=80% drop=False conf=0.9 — 'Development is at 63s out of 683s total (9.2%). To reach the 14.6% target, I need about 37 more seconds of development. RoR is 9.3 C/min, which is healthy. I will reduce heat slightly to 50% to ensure a gentle glide toward the finish while maintaining the current fan setting to keep airflow consistent. I am not ready to drop yet.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-17.7 C/min) despite maximum heat. This indicates a stall or a measurement error, but given the current low bean temperature (97 C), the roast is in danger of stalling. I am maintaining maximum heat to recover momentum and keeping fan low to minimize convective heat loss until the bean temperature stabilizes and begins to rise again.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'RoR is healthy at 10.3 C/min. We are approaching the Maillard phase and first crack. Maintaining 100% heat is appropriate to keep momentum, but I am increasing fan to 25% to begin transitioning toward convective heat transfer and preparing for the smoke/chaff management needed as we approach first crack.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just begun. The RoR is healthy (11.3 C/min) but the heat is at 100%. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and increasing fan to shift toward convective heat transfer, ensuring a controlled development period of ~80-90 seconds to reach the 12.7% development target.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=50% fan=100% drop=False conf=0.9 — 'Current development time is 64s out of 676s total (9.5%). To reach the 12.7% target, I need approximately 22-25 more seconds of development. RoR is 13C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 194-195C in about 25 seconds.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature is 130C, which is below the recommended charge range of 180C-200C. I am setting heat to 100% to accelerate the preheating process to reach the target charge temperature. Fan is kept at 0% to allow the drum to heat up efficiently.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'RoR is healthy at 14.3 C/min and bean temp is 125.3 C. We are approaching the mid-roast phase. Maintaining full heat is appropriate to keep momentum, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach first crack.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'Approaching first crack with a steady RoR of 12C/min. Maintaining full heat to ensure momentum through the transition, but increasing fan to 50% to prepare for the exothermic reaction and improve convective heat transfer as we approach the crack.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=40% fan=60% drop=False conf=0.9 — 'The roast is currently at 85s of development with a total time of 595s (14.3% development ratio). The RoR is 12C/min, which is healthy but needs to be managed to avoid overshooting the target development time. I am reducing heat to 40% to slow the momentum and increasing fan to 60% to shift toward convective heat, ensuring a controlled finish. The drop is not yet indicated as we are still building development.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has not yet been charged. The environment temperature is approaching the target charge range. I am setting heat to 0 and fan to 0 to maintain the current state until the user is ready to charge the beans.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The RoR has dropped significantly to 9.3 C/min, indicating the roast is losing momentum well before first crack. I need to maintain heat to ensure we reach first crack with enough energy to sustain development. Keeping heat at 100% and fan at 0% to maximize conductive heat transfer and prevent a stall.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=40% fan=70% drop=False conf=0.9 — 'First crack occurred at 637s. Total roast time is 656s, with 19s of development. To reach a 14% development ratio, I need a total roast time of ~135s post-crack (approx 772s total). With an RoR of 11.7 C/min, I have plenty of momentum. I am reducing heat to 40% and increasing fan to 70% to transition to convective heat, manage the RoR, and ensure a smooth, controlled development phase without stalling.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=0% fan=70% drop=False conf=0.9 — 'Development time is 49s out of 686s total (7.1%). To reach a 14% development ratio, I need ~110s total development time (61s more). RoR is 12.7 C/min, which is healthy. I am reducing heat to 0% to coast toward the target and increasing fan to 70% to ensure convective airflow and smoke evacuation as we approach the finish. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-7.37 C/min) despite 100% heat. This indicates a stall or a measurement error, but assuming the telemetry is accurate, the roast is losing momentum rapidly. I am maintaining 100% heat to recover the RoR and keeping fan low (10%) to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 134°C. With an RoR of 13.3°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the crack.'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just occurred. The RoR is healthy (11 C/min), but heat is at 100% with no fan. To manage the development phase and prevent a runaway roast, I am cutting heat significantly and introducing airflow to shift toward convective heat transfer, ensuring a controlled development period toward the 20% target.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=100% fan=60% drop=False conf=0.9 — 'Development time is 66s out of 583s total (11.3%). To reach the 20.5% target, I need ~120s total development time. Current RoR is 10C/min, which is healthy. I will maintain heat to ensure steady development while increasing fan to 60% to improve convective heat transfer and manage smoke as we approach the drop. I am not dropping yet as I need more development time.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — "The roast is in the very early drying/yellowing phase. The bean temperature is dropping rapidly (likely due to the charge temperature being lower than the probe's initial reading or a sensor stabilization issue). Heat is already at 100%. I will maintain maximum heat to drive the roast forward and keep the fan at 0% to build momentum and ensure the drum retains sufficient energy for the upcoming Maillard phase."
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'RoR is healthy at 12.3 C/min and the bean temperature is climbing steadily. I am increasing the fan to 25% to begin introducing convective heat and preparing for the upcoming moisture release, while maintaining full heat to ensure we reach first crack with sufficient momentum.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just begun (4 seconds in). The current heat setting of 100% is far too high for the development phase and will cause an uncontrolled RoR spike. I am cutting heat significantly to 40% and increasing fan to 50% to transition to convective heat, manage the RoR, and clear smoke, ensuring a controlled development period toward the 17.8% target.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.9 — 'Development time is 64s out of 621s total (10.3%). To reach the 17.8% target, I need ~135s total development time (approx 70s more). RoR is 9C/min, which is healthy. I will reduce heat to 60% to manage the momentum and increase fan to 70% to shift toward convective heat, ensuring a steady, controlled finish without stalling. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR is showing a sharp, concerning drop (-16 C/min) despite 100% heat. This suggests a measurement anomaly or a significant stall. Given the low bean temperature (86 C), I need to maintain maximum heat to recover momentum. The fan is kept low to minimize convective heat loss while the drum recovers.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily toward first crack. With a bean RoR of 10.3 C/min, we have good momentum. I am maintaining full heat to ensure we don't stall before first crack, while keeping the fan at 20% to preserve drum heat for the upcoming endothermic phase. No drop yet."
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=60% fan=70% drop=False conf=0.9 — 'Development is just beginning (12s in). The RoR is healthy at 9.7 C/min. To ensure a smooth development phase and hit the 13.6% target (approx 102s total development time), I am reducing heat to prevent a spike while maintaining moderate airflow to transition to convective heat transfer. This balance will allow for a controlled, steady development.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'The roast is currently at 193°C with a development time of 72 seconds (approx 10% of total time). The RoR is 10.3°C/min, which is healthy. To reach the target development ratio of ~13.6% (approx 109 seconds total development), I need about 37 more seconds. Given the current RoR, the temperature will likely reach ~198-199°C, which is appropriate. I am maintaining full heat and fan to ensure consistent development without stalling, as the RoR is not excessive.'

## prompt_version = v4

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=15 dir=0.762; fan MAE=13.41 dir=0.524; latency pre=1.25s preFC=1.2s FC=1.08s
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
    |         hold |     3 |    14 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.762 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-8.0s/-1.0°C; heat MAE=11.2 dir=0.833; fan MAE=14.6 dir=0.333; latency pre=1.05s preFC=1.19s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.833 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=13.7 dir=0.808; fan MAE=16.48 dir=0.462; latency pre=1.26s preFC=1.25s FC=1.43s
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
    |         hold |     3 |    17 |     2 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.808 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/-0.3°C; heat MAE=7.14 dir=0.9; fan MAE=8.57 dir=0.55; latency pre=1.03s preFC=1.35s FC=1.22s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.52 dir=0.885; fan MAE=17.41 dir=0.308; latency pre=1.15s preFC=1.29s FC=1.28s
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
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.885 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-3.0s/-0.7°C; heat MAE=16.67 dir=0.783; fan MAE=13.75 dir=0.478; latency pre=1.15s preFC=1.22s FC=1.28s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/+0.0°C; heat MAE=9.05 dir=0.85; fan MAE=15.48 dir=0.45; latency pre=1.46s preFC=1.23s FC=1.39s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.62 dir=0.9; fan MAE=9.05 dir=0.6; latency pre=1.23s preFC=1.31s FC=1.32s
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
    |         hold |     2 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.62 dir=0.85; fan MAE=14.29 dir=0.4; latency pre=0.92s preFC=1.18s FC=1.32s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=13.48 dir=0.818; fan MAE=18.7 dir=0.318; latency pre=0.89s preFC=1.22s FC=1.45s
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
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.14 dir=0.857; fan MAE=10 dir=0.476; latency pre=1.17s preFC=1.23s FC=1.24s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=13.8 dir=0.75; fan MAE=14.6 dir=0.5; latency pre=1.24s preFC=1.19s FC=1.3s
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
    |         hold |     3 |    15 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.75 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5 dir=0.905; fan MAE=18.86 dir=0.286; latency pre=0.98s preFC=1.23s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-7.0s/-1.3°C; heat MAE=11.14 dir=0.81; fan MAE=17.05 dir=0.429; latency pre=0.9s preFC=1.18s FC=1.47s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.67 dir=0.826; fan MAE=18.12 dir=0.348; latency pre=1.12s preFC=1.23s FC=1.36s
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
    |         hold |     3 |    16 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.4 dir=0.875; fan MAE=11.2 dir=0.458; latency pre=0.97s preFC=1.15s FC=1.18s
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
    |         hold |     3 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=12.86 dir=0.85; fan MAE=12.86 dir=0.4; latency pre=1.11s preFC=1.3s FC=1.35s
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
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-6.0s/-0.6°C; heat MAE=10.38 dir=0.8; fan MAE=7.31 dir=0.84; latency pre=1.09s preFC=1.21s FC=1.04s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     5 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-5.0s/-0.6°C; heat MAE=14.62 dir=0.84; fan MAE=19.62 dir=0.24; latency pre=1.11s preFC=1.38s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     4 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/+0.0°C; heat MAE=13.33 dir=0.87; fan MAE=10 dir=0.565; latency pre=0.94s preFC=1.12s FC=1.38s
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
    |         hold |     2 |    15 |     0 |
    |        raise |     1 |     0 |     2 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.92 dir=0.8; fan MAE=15 dir=0.44; latency pre=1.01s preFC=1.3s FC=1.29s
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
    |         hold |     4 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.38 dir=0.92; fan MAE=9.23 dir=0.56; latency pre=1.24s preFC=1.23s FC=1.34s
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
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.74 dir=0.9; fan MAE=7.58 dir=0.633; latency pre=1.21s preFC=1.17s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    24 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=10 dir=0.826; fan MAE=16.46 dir=0.304; latency pre=1.08s preFC=1.21s FC=1.51s
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
    |         hold |     2 |    16 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-25.0s/-2.0°C; heat MAE=5.54 dir=0.926; fan MAE=12.32 dir=0.519; latency pre=1.32s preFC=1.16s FC=1.31s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    21 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.926 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-7.0s/-1.0°C; heat MAE=14.29 dir=0.852; fan MAE=15.54 dir=0.444; latency pre=0.96s preFC=1.35s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    21 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.852 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.67 dir=0.913; fan MAE=19.17 dir=0.348; latency pre=1.03s preFC=1.26s FC=1.24s
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
    |         hold |     2 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.913 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.56 dir=0.923; fan MAE=9.07 dir=0.538; latency pre=1.33s preFC=1.23s FC=1.1s
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
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123°C, which is below the recommended charge range (180°C-200°C). I am setting heat to 100% to reach the target charge temperature efficiently. Fan is kept at 0% to allow the drum to soak up heat.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'RoR is healthy at 14.6 C/min and the bean temperature is climbing steadily through the drying/early Maillard phase. I am maintaining full heat to ensure sufficient momentum for the upcoming Maillard phase and first crack. Fan remains at 0% to maximize conductive heat transfer during this stage.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just begun (13 seconds ago). The RoR is healthy at 11C/min. To manage the development phase and prevent a spike, I am reducing heat to 60% and increasing fan to 50% to shift toward convective heat and ensure steady, controlled development toward the 189C target.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=60% fan=80% drop=False conf=0.9 — 'The roast is in the development phase with a steady RoR of 8C/min. We are 8C away from the target drop temperature of 189C. To ensure a smooth finish without overshooting or stalling, I am reducing heat to 60% to manage the momentum and increasing fan to 80% to shift toward convective heat, which will help maintain a controlled, steady finish as we approach the drop. We are not yet at the target drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The current bean temperature is 121°C, which is below the recommended charge range (180°C-200°C). I am setting heat to 0 and fan to 0 to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With the bean temperature at 135.3°C and a stable RoR, it is time to introduce airflow to transition toward convective heat transfer and prepare for the upcoming first crack. I am increasing the fan to 25% while maintaining full heat to ensure momentum is preserved as we approach the crack.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=70% fan=50% drop=False conf=0.9 — 'The roast is approaching first crack (173C indicated). The RoR is healthy at 10.7C/min. To prepare for the transition into first crack and ensure a smooth development phase, I am reducing heat to manage the momentum and increasing fan to shift toward convective heat transfer, which will help prevent tipping or scorching as the beans become more porous.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=60% fan=80% drop=False conf=0.95 — 'The roast is progressing well with a steady RoR of 7.3 C/min. We are 5 degrees away from the target drop temp of 190 C. With 86 seconds of development time, we are approaching the target development ratio. I am reducing heat to 60% to manage the final approach and increasing fan to 80% to ensure convective heat transfer and clean airflow as we approach the drop. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is currently in a severe crash (RoR -14.3 C/min) immediately following the charge/early phase. The bean temperature is dropping rapidly despite 100% heat. I need to maintain maximum heat to recover the momentum and keep the fan at 0% to prevent further heat loss until the bean temperature stabilizes and begins to climb again.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early stages of the Maillard phase. With a RoR of 12C/min and no first crack yet, I am maintaining full heat to ensure sufficient momentum. I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release, which will help prevent stalling as we approach the crack.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has started (17s ago). RoR is currently 10C/min, which is healthy but needs to be managed to avoid a runaway finish. I am increasing fan to 60% to shift toward convective heat and improve smoke evacuation, while reducing heat to 60% to gently taper the RoR as we approach the 190C target. We are still early in development (17s), so we are not ready to drop.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=100% fan=90% drop=False conf=0.95 — 'The roast is in the final stages of development. Current bean temp is 183.3C with a target of 190C. The RoR is low (8.6C/min) and stable. Heat was recently increased to 100% to push through the final stretch, but given the proximity to the target and the risk of overshooting, I am maintaining high heat to ensure we reach the target without stalling, while keeping fan at 90% to manage convective heat and smoke. I will monitor closely for the 190C mark to trigger the drop.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C is within the 180-200C guidance). It is ready to be charged.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'RoR is healthy (13.5 C/min) and the bean temperature is climbing steadily through the mid-Maillard phase. Maintaining full heat is appropriate here to ensure sufficient momentum before first crack. I am increasing the fan slightly to 20% to begin clearing moisture and preparing for the upcoming exothermic phase, while keeping heat at 100% to avoid stalling.'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'Approaching first crack (176C indicated). RoR is healthy at 11C/min. To manage the transition into first crack and prevent a spike, I am reducing heat to 70% and increasing fan to 30% to shift toward convective heat and prepare for the exothermic reaction.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=30% fan=60% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 10C/min. We are 5.3C away from the target drop temperature of 191.3C. At the current rate, we will reach the target in approximately 30-40 seconds, which will align well with the development percentage target. Reducing heat slightly now to manage the momentum as we approach the drop.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156°C, which is below the recommended charge range (180°C-200°C). I am maintaining heat and fan at 0 to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early Maillard phase. With a bean RoR of 11.7 C/min and no fan usage yet, I am increasing the fan to 25% to begin introducing convective heat and preparing for the upcoming moisture release, while maintaining full heat to keep the momentum toward first crack.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — "First crack has just started (5 seconds ago). The RoR is currently 9.7 C/min, which is healthy for this stage. I am reducing heat to 60% and increasing fan to 60% to manage the momentum of the roast, transition to convective heat, and ensure we don't overshoot the target drop temperature of 191.7 C. We are not ready to drop yet."
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=60% fan=70% drop=False conf=0.95 — 'The roast is in the final stages of development. With a current bean temp of 183C and a target of 191.7C, we are approaching the finish. The RoR has slowed to 6C/min. To ensure a smooth finish without stalling, I am reducing heat to 60% to manage the momentum and increasing fan to 70% to improve convective efficiency and clear smoke as we approach the drop. We are nearing the target development time and temperature.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (9 seconds elapsed). The bean temperature is dropping rapidly after the charge, which is expected. Maintaining maximum heat (100%) is necessary to recover momentum. Fan remains at 0% to build initial energy.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing well through the drying/early Maillard phase. With a bean RoR of 14.0 C/min and no fan usage yet, it is time to introduce airflow to transition toward convective heat transfer and prepare for the upcoming Maillard phase. I am increasing the fan to 25% while maintaining full heat to keep the momentum steady as we approach first crack.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=80% fan=40% drop=False conf=0.9 — 'First crack has just begun (10s ago). The RoR is currently 10C/min, which is healthy for this stage. I am maintaining high heat and moderate fan to ensure momentum through the development phase, but will prepare to reduce heat as we approach the target drop temperature of 192.7C to avoid overshooting.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=70% fan=70% drop=False conf=0.9 — 'The roast is progressing well through development (70s in, ~12% development ratio). With the RoR at 7.3 C/min and the bean temp at 183 C, we are approaching the target drop of 192.7 C. To ensure a smooth finish without stalling, I am reducing heat to 70% to manage the momentum and increasing fan to 70% to shift toward convective heat and clear smoke as we approach the final stages. We are not yet at the drop temperature.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and is within the recommended charge temperature range (180-200C). The current bean temperature is 188C, which is ideal for charging. No heat or fan is currently applied, as expected for preheating. Ready to charge.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 145.7°C. With a RoR of ~11.8°C/min, we are approaching the Maillard phase. It is time to introduce airflow to shift toward convective heat and manage the environment temperature. I am increasing the fan to 25% to begin clearing moisture and chaff, while maintaining full heat to sustain momentum before the inevitable RoR decline.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=50% fan=50% drop=False conf=0.9 — 'First crack has just begun (5 seconds ago). The RoR is still quite high (10 C/min) and heat is at 100% with no fan. To manage the development phase and prevent a runaway roast, I need to introduce convective airflow to stabilize the RoR and reduce the heat input to avoid overshooting the 193 C target. Dropping heat to 50% and increasing fan to 50% will help manage the momentum while ensuring even development.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=30% fan=80% drop=False conf=0.95 — 'The roast is progressing well through development (65s in, ~12% development ratio). RoR is healthy at 9.7 C/min. With 5.3 C to go to the target drop of 193 C, I am reducing heat to 30% to manage the momentum and increasing fan to 80% to ensure convective heat transfer and smoke evacuation as we approach the finish. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature is dropping rapidly in the telemetry (likely due to a sensor artifact or probe stabilization after charge), but the environment temperature is stable. Maintaining 100% heat is appropriate to build momentum for the Maillard phase. Fan remains at 0% to maximize conductive heat transfer during this early stage.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The RoR has flattened significantly (12 C/min and stalling at 143 C). To prevent a stall and maintain momentum toward first crack, I need to increase convective heat transfer. Increasing fan speed will help pull more heat through the bean mass, and maintaining high heat is necessary to overcome the current stagnation.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=50% fan=60% drop=False conf=0.9 — 'First crack occurred 18 seconds ago. The RoR is currently 10C/min, which is healthy for this stage. To ensure a smooth development toward the 193C target without overshooting or stalling, I am reducing heat to manage the momentum and increasing fan to shift toward convective heat transfer, which helps maintain a clean finish. We are still early in development (18s), so we are not ready to drop yet.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=25% fan=80% drop=False conf=0.9 — 'RoR is healthy (10.6 C/min) and we are approaching the target drop temp of 193 C. Development time is currently ~8.3% (48s/578s total). To reach the 16.6% target development, we need to extend the roast by about 45-50 seconds. I am reducing heat to 25% to manage the momentum and increasing fan to 80% to shift toward convective heat, ensuring a gentle, controlled finish without stalling.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C). It is ready to be charged.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily through the Maillard phase. With the bean RoR at 11.7 C/min and the bean temperature at 143 C, we are approaching the window where we need to manage the transition to first crack. I am increasing the fan to 25% to introduce convective heat and begin clearing moisture/chaff, while maintaining full heat to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'Approaching first crack (176C indicated). RoR is healthy at 10.3C/min. I am reducing heat to 70% to manage the exothermic spike and increasing fan to 50% to shift toward convective heat and prepare for smoke evacuation. This prepares the roast for a smooth transition into the development phase.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.95 — 'The roast is progressing well through development (52 seconds in, ~10% development ratio). With the bean temperature at 186°C and a steady RoR of 10°C/min, we are approaching the 193°C target. To ensure a smooth finish without overshooting or stalling, I am reducing heat to 60% to manage the momentum and increasing fan to 70% to transition to convective heat for a clean finish. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (13 seconds elapsed). The bean temperature reading is currently erratic/dropping (likely due to probe stabilization after charging), so I am maintaining maximum heat to ensure sufficient energy input for the drying phase. Fan remains at 0% to build momentum.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 129C. With RoR at ~13.8C/min, we are in a good position to maintain momentum. I am increasing the fan to 25% to begin introducing convective heat and preparing for the upcoming Maillard phase, while keeping heat at 100% to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=100% fan=60% drop=False conf=0.9 — "First crack occurred at 481s. We are 14s into development with a RoR of 10.3 C/min. The RoR is healthy but needs to be managed to avoid a spike as we approach the 193 C target. I am increasing the fan to 60% to shift toward convective heat and reduce the risk of tipping/scorching, while maintaining 100% heat for now to ensure we don't stall the momentum before the final approach."
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=70% fan=50% drop=False conf=0.9 — 'The roast is progressing well through development (104s in). With a RoR of 9C/min and 8 degrees to go until the 193C target, I need to start tapering energy to avoid overshooting or hitting the 196C ceiling. Reducing heat to 70% and increasing fan to 50% will shift the roast toward convective heat, smoothing the final approach to the drop temperature.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (168C to 151C in 5 seconds), which suggests a sensor error or a massive stall. Given the heat is at 100% and fan is at 0%, I must maintain maximum heat to recover momentum and prevent a stall, while keeping the fan off to maximize conductive heat transfer.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.3 C/min and the bean temperature at 142 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 511s. We are 13s into development with a RoR of 11C/min. The bean temp is 182.7C, well below the 193.7C target. To manage the momentum post-crack, I am reducing heat to 60% to prevent a spike, while maintaining moderate airflow (60%) to ensure convective heat transfer and smoke evacuation without stalling the RoR.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=60% fan=80% drop=False conf=0.9 — 'RoR is still healthy at 10.3 C/min, and we are 6.4 C away from the target drop temp. Development time is currently 43s (approx 7.7% of total time). To reach the 14% development target, we need to extend the roast slightly while managing the momentum. Reducing heat to 60% and increasing fan to 80% will shift to convective heat, smoothing the final approach to the drop temperature without stalling the roast.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=50% fan=50% drop=False conf=0.9 — 'The roast is currently in a dangerous state: heat and fan are both at 0%, and the bean RoR has dropped significantly (from 18.3 to negative). To prevent stalling or baking before first crack, I must introduce convective heat and airflow immediately. I am setting heat to 50% and fan to 50% to stabilize the momentum and prepare for the upcoming first crack.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a RoR of 12.3 C/min and bean temp at 141.3 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing fan to 30% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just begun (6 seconds ago). The RoR is still healthy at 11C/min. To manage the development phase and prevent a spike, I am reducing heat to 70% and increasing fan to 50% to transition to convective heat and manage the momentum. We are 12C away from the target drop temp, so we have time to coast.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=30% fan=70% drop=False conf=0.9 — 'The roast is in the development phase post-first crack. Current bean temp is 187C, approaching the 194C target. RoR is 10C/min, which is healthy but needs to be managed to avoid overshooting. I am reducing heat to 30% to gently coast toward the drop temp and increasing fan to 70% to improve convective heat transfer and clear smoke, ensuring a clean finish. Drop is not yet indicated as we are 7C away from target.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early stages (127C). The recent telemetry shows a rapid, erratic drop in bean temperature, likely due to a probe reading anomaly or a very early stage adjustment. Since the heat is at 100% and fan is at 0%, I will maintain maximum heat to drive the roast forward through the drying phase, keeping the fan off to build momentum.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'RoR is healthy at 12.7 C/min and bean temp is 140 C. We are approaching the Maillard phase. It is time to introduce airflow to shift toward convective heat transfer and prepare for the upcoming first crack. Increasing fan to 25% while maintaining full heat to sustain momentum.'
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just begun (2 seconds ago). The RoR is still healthy at 10C/min. To manage the development phase and prevent a runaway roast, I am reducing heat to 60% to moderate the energy input and increasing fan to 40% to introduce convective heat and begin clearing smoke. This balance will allow for a controlled, steady development toward the 194C target.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=50% fan=70% drop=False conf=0.9 — 'The roast is progressing well through development. With a current bean temp of 186C and a target of 194C, we are 8C away. The RoR is stable at 8.7C/min. To ensure a smooth finish without overshooting or stalling, I am reducing heat to 50% to manage the momentum and increasing fan to 70% to shift toward convective heat and clear smoke as we approach the drop. We are not yet at the target drop temp, so should_drop is false.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The current bean temperature of 174.0°C is within the recommended charge range (180°C-200°C). I am setting heat and fan to 0 to maintain the current state until the operator is ready to charge the beans.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has flattened significantly (11.7 C/min and stalling at 141 C). With heat at 100% and fan at 0%, the roaster is heat-saturated but failing to maintain momentum. I need to introduce airflow to move the heat and prevent a stall, while maintaining high heat to push through the Maillard phase toward first crack. Increasing fan to 25% will improve convective efficiency, and keeping heat at 100% is necessary to recover the RoR.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just begun (14 seconds ago). The RoR is healthy at 10C/min. To manage the development phase and prevent a spike, I am reducing heat to 60% and increasing fan to 50% to transition to convective heat transfer and clear smoke. This will help maintain a controlled, steady climb toward the 194.3C target without stalling or overshooting.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=30% fan=100% drop=False conf=0.95 — 'The roast is in the final stages of development. With a current bean temperature of 188°C and a target drop of 194.3°C, we are approaching the finish. The RoR is low (9°C/min) and stable. Maintaining heat at 30% is appropriate to gently coast to the target without stalling, while keeping the fan at 100% ensures clean convective heat and smoke evacuation. I will hold these settings until the target temperature is reached.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=80% fan=20% drop=False conf=0.9 — 'The roast has just started (1 second elapsed, though telemetry shows 31 seconds). Heat and fan are currently at 0%. I need to initiate the roast by applying heat to build momentum toward the Maillard phase. Setting heat to 80% and fan to 20% to begin the drying/early ramp phase while ensuring enough airflow to clear initial moisture.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily at 130°C. With a RoR of 14°C/min, we are in a good position. I am maintaining full heat to ensure we don't lose momentum before the Maillard phase, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke that will accompany the upcoming Maillard and first crack phases."
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=100% fan=50% drop=False conf=0.9 — "First crack has just begun. The RoR is healthy (11.3 C/min), and we are at the very start of the development phase. To manage the momentum through the development phase and avoid an overly rapid finish, I am increasing the fan to 50% to shift toward convective heat and reduce the risk of tipping/scorching, while maintaining heat at 100% for now to ensure we don't stall the roast immediately after crack. I will monitor the RoR closely to decide when to begin tapering heat."
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.9 — 'The roast is in the final stages of development. With a current bean temp of 185C and a target of 194.7C, we are approaching the finish. The RoR is low (8C/min) and stable. To ensure a smooth finish without overshooting or stalling, I am reducing heat to 50% to manage the momentum and increasing fan to 60% to transition to convective heat for the final minute, ensuring clean development. We are not yet at the drop temperature.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and is ready for charging. The current environment temperature (143.3C) is below the recommended charge range (180C-200C). I am recommending no heat or fan yet to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.3 C/min and the bean temperature at 137.3 C, we are approaching the window where we need to prepare for first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required as we approach first crack.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun (3 seconds ago). The RoR is still healthy at 9C/min. To manage the development phase and prevent a runaway finish, I am reducing heat to 60% to moderate the momentum and increasing fan to 60% to shift toward convective heat and clear smoke. This prepares the roast for a smooth approach to the 195C target.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=40% fan=90% drop=False conf=0.95 — 'The roast is progressing well through development (63 seconds in). With a current bean temp of 190C and a target of 195C, the RoR is at 11C/min, which is appropriate for this stage. I am reducing heat to 40% to manage the final approach to the drop temperature and increasing fan to 90% to ensure clean convective heat and effective smoke evacuation as we approach the finish. We are nearing the target drop temp and development ratio, so I will prepare to drop shortly.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=20% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (176C is close to the 180C minimum). I am recommending a heat setting to stabilize the drum and fan to ensure airflow is ready for the charge.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.7 C/min and a current temperature of 138.7 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required as we approach first crack.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred 12 seconds ago. The RoR is currently 10C/min, which is healthy but needs to be managed to avoid overshooting the target. I am reducing heat to 60% to begin the glide toward the 195C drop target and increasing fan to 60% to shift toward convective heat and clear smoke, ensuring a clean finish.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=50% fan=70% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 10.7 C/min. We are at 188 C, approaching the 195 C target. To ensure a smooth finish without overshooting or stalling, I am reducing heat to 50% to manage the momentum while maintaining 70% fan to ensure convective consistency. We are nearing the target drop temperature and development ratio; I will prepare to drop in the next 60-90 seconds.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156°C, which is below the recommended charge range (180°C-200°C). I am maintaining heat and fan at 0 to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=60% drop=False conf=0.9 — 'The roast is stalling significantly at 140C with 100% heat and 100% fan. The RoR has dropped to 11.3 C/min and is flatlining. To regain momentum before first crack, I need to reduce the fan to increase conductive heat transfer and reduce the cooling effect of the airflow, while maintaining maximum heat.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just started (622s). The RoR is currently 10C/min, which is healthy for the start of development. I am reducing heat to 70% to manage the momentum and increasing fan to 50% to shift toward convective heat and clear smoke. This will help maintain a controlled, steady climb toward the 195.3C target without spiking the RoR.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=60% fan=80% drop=False conf=0.9 — 'The roast is progressing well through development (62 seconds in). The RoR is at 9.7 C/min, which is healthy for this stage. With the bean temperature at 190.7 C and a target of 195.3 C, we are approaching the finish. I am reducing heat to 60% to manage the momentum as we approach the drop, while maintaining 80% fan to ensure convective heat and smoke evacuation. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (drying phase). The bean temperature is dropping/stalling after the initial charge, which is expected as the beans absorb heat. Maintaining maximum heat (100%) is appropriate here to build momentum. Fan remains at 0% to maximize conductive heat transfer and build energy in the drum.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has dropped significantly to 10.7 C/min and the bean temperature has stalled at 137 C despite 100% heat. The roast is losing momentum. I am increasing the fan to 25% to improve convective heat transfer and help pull the heat through the bean mass, while maintaining 100% heat to recover the RoR before first crack.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is approaching first crack (179C indicated). RoR is healthy at 10.3C/min. I am increasing fan to 30% to transition toward convective heat transfer and prepare for the smoke/chaff of first crack, while maintaining 100% heat to ensure momentum is carried through the transition without stalling.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.95 — 'The roast is progressing well through development (54s in, ~8% development ratio). The RoR has flattened at 10C/min, which is appropriate for this stage. To ensure a smooth finish toward the 195.3C target without overshooting or stalling, I am reducing heat to 60% to manage the momentum and increasing fan to 60% to shift toward convective heat and clear smoke. We are approaching the drop window; I will hold here and monitor for the final approach.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the very early stages (drying phase). The bean temperature is dropping rapidly due to the initial charge, which is expected. Maintaining maximum heat (100%) is necessary to recover momentum. Fan is kept low (10%) to retain heat and build energy for the Maillard phase.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a RoR of 13.3 C/min and bean temp at 139 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 25% to begin shifting toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=50% fan=70% drop=False conf=0.9 — 'First crack has just begun (6 seconds ago). The RoR is already dropping significantly (9.8 C/min). To manage the development phase and prevent an overly rapid finish, I am reducing heat to 50% and increasing fan to 70% to shift toward convective heat and ensure steady, controlled development toward the 196C target. We are nowhere near the drop point yet.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=30% fan=100% drop=False conf=0.9 — 'The roast is in the development phase with 66 seconds elapsed since first crack. The current bean temperature is 188°C, and the RoR is 9°C/min. To reach the target drop of 196°C without overshooting or stalling, I am reducing heat to 30% to manage the momentum and increasing fan to 100% to maximize convective heat and ensure clean development. We are approaching the target, so we will monitor closely for the drop.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is currently in a severe stall/crash (RoR -27 C/min). The bean temperature is dropping rapidly despite 100% heat. This indicates the charge was likely too low or the environment is not yet saturated. I am maintaining 100% heat to recover the energy and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling. The roast needs to stabilize and begin climbing again immediately.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 13°C/min and the bean temperature at 135°C, we are approaching the window where we need to manage momentum before first crack. I am maintaining full heat to ensure sufficient energy for the upcoming crack, but initiating a modest increase in fan speed to 25% to begin transitioning toward convective heat transfer and clearing moisture/chaff.'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just begun (3 seconds ago). The RoR is still healthy at 10C/min. To manage the development phase and prevent an overly aggressive finish, I am reducing heat to 70% and increasing fan to 50% to shift toward convective heat transfer and manage the energy momentum. We are well below the drop target of 196C.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=60% fan=90% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 9.0 C/min. We are at 187 C, approaching the 196 C ceiling. Development time is currently ~9.6% (63s/652s), so we need to continue development toward the 16.5% target. Maintaining current settings is appropriate to keep the momentum without risking a stall or an overly aggressive finish. I will hold heat and fan steady to allow the roast to reach the target drop temperature gradually.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature is 139°C, which is below the recommended charge range (180°C-200°C). I am recommending no heat or fan to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has flattened significantly (11 C/min and stalling at 137 C). To push through the Maillard phase and maintain momentum toward first crack, I need to increase convective heat transfer. I am increasing fan to 30% to improve airflow while maintaining 100% heat to prevent a crash.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=70% fan=40% drop=False conf=0.9 — 'First crack has just begun (3 seconds ago). The RoR is still healthy at 10.7 C/min. I am reducing heat to 70% to manage the momentum and increasing fan to 40% to shift toward convective heat and clear smoke, preparing for a smooth finish toward the 196.3 C target.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.9 — 'The roast is progressing well through development (63 seconds in). With the bean temperature at 189°C and a steady RoR of 9.3°C/min, we are approaching the target drop of 196.3°C. I am reducing heat to 40% to manage the momentum as we approach the finish, while maintaining 80% fan to ensure clean convective heat and smoke evacuation. We are not yet at the drop temperature or the development time target, so we continue.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is experiencing a severe, unexplained drop in bean temperature (RoR -17.7 C/min) despite maximum heat. This suggests a sensor error or a major mechanical issue (e.g., bean probe displacement or airflow surge). Given the current state, I am maintaining maximum heat to attempt to stabilize the bean temperature, but the situation is highly abnormal.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 10.3 C/min and current temp at 135 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just begun (4 seconds ago). The RoR is still quite high (11.3 C/min) for this stage. To manage the development phase and prevent a crash or an overly fast finish, I am reducing heat to 60% and increasing fan to 50% to shift toward convective heat and manage the momentum. We are still well below the target drop temperature.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=50% fan=100% drop=False conf=0.9 — 'The roast is progressing well through development. With a current bean temperature of 191.0°C and a target of 196.3°C, we are approaching the finish line. The RoR has stabilized at 13°C/min, which is appropriate for this stage. I am maintaining the current heat and fan settings to ensure a steady, controlled finish without risking a stall or an overly rapid spike. I will monitor closely for the target temperature to trigger the drop.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current bean temperature is 99.0°C, which is well below the target charge range of 180°C-200°C. No heat or fan is required yet as the machine continues to stabilize.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "Roast is progressing steadily at 125C. With RoR at 14.3C/min, we are in a good position to maintain momentum. I am increasing the fan to 25% to begin introducing convective heat and preparing for the upcoming Maillard phase, while keeping heat at 100% to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — "The roast is approaching the first crack window. With a RoR of 12C/min and bean temp at 167.7C, I need to prepare for the transition. I am increasing the fan to 50% to shift toward convective heat and improve airflow before the crack, while maintaining 100% heat to ensure we don't stall before the crack begins."
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=100% fan=50% drop=False conf=0.9 — 'The roast is in the development phase with 85 seconds elapsed since first crack. The current bean temperature is 185°C, and the RoR is 12°C/min. The target drop temperature is 197°C. Given the current RoR, we have about 60-90 seconds of development remaining. I am increasing heat to 100% to maintain momentum and increasing fan to 50% to ensure convective heat transfer and smoke evacuation as we approach the finish. We are not yet at the drop temperature.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has not yet been charged. The current environment temperature is 165.7C, which is below the recommended charge range (180C-200C). I am recommending no heat or fan to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has flattened significantly (9.3 C/min and stalling at 134 C). With heat at 100% and fan at 0%, the roaster is struggling to maintain momentum. I am increasing fan to 25% to improve convective heat transfer and help push through the current plateau, while maintaining 100% heat to provide the necessary energy for the upcoming Maillard phase.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.9 — 'First crack occurred 19 seconds ago. The RoR is still healthy at 11.7 C/min. To manage the momentum toward the 197 C target, I am reducing heat to 50% and increasing fan to 60% to shift toward convective cooling and ensure a controlled, even finish without stalling. We are not yet at the drop temperature.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=0% fan=60% drop=False conf=0.95 — 'The roast is in the final stages of development. With a current bean temperature of 189°C and a target of 197°C, we are approaching the finish. The RoR is 12.7°C/min, which is healthy but needs to be managed to avoid overshooting the target or hitting the 196°C+ danger zone. I am reducing heat to 0% to coast into the drop and maintaining fan at 60% to ensure convective airflow and smoke evacuation. I will monitor closely to drop as soon as the target is reached.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (-7.377 C/min RoR) despite 100% heat. This indicates a sensor anomaly or a severe stall. Given the current environment temperature is stable at 167C, I am maintaining maximum heat to recover momentum and keeping fan low to minimize convective cooling while the bean temperature is this low.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily at 134°C. With RoR at 13.3°C/min, we are in a good position to maintain momentum. I am introducing 20% fan to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release, while keeping heat at 100% to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=100% fan=50% drop=False conf=0.9 — 'First crack has just begun (6 seconds ago). The RoR is still healthy at 11C/min. To manage the development phase and prevent an overly rapid finish, I am increasing the fan to 50% to introduce convective heat and clear smoke, while maintaining 100% heat for now to ensure the momentum carries through the development phase without stalling. I will monitor the RoR closely to decide when to cut heat.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is progressing well, but the RoR is still at 10C/min with 11 degrees to go to the target drop. To avoid overshooting or hitting the 196C ceiling too aggressively, I am cutting heat to 60% and increasing fan to 60% to shift toward convective cooling and manage the momentum. We are at 66 seconds of development; dropping now would be premature.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (91C). The recent telemetry shows a significant drop in bean temperature (101C to 91C in 5 seconds), which is likely a sensor artifact or a stabilization period after charging. Since the heat is already at 100% and fan is at 0%, I will maintain maximum heat to drive the roast forward and keep the fan off to build momentum during the drying phase.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily through the drying/early Maillard phase. With a bean RoR of 12.3 C/min and the bean temperature at 132.3 C, we have sufficient momentum. I am maintaining full heat to ensure we don't stall before first crack, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release."
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=50% fan=30% drop=False conf=0.9 — 'First crack has just started (4 seconds ago). The RoR is still healthy at 10.7 C/min. To manage the development phase and prevent an uncontrolled spike, I am reducing heat to 50% and introducing airflow (30%) to shift toward convective heat and clear smoke. This will help maintain a steady, controlled glide toward the 197 C target.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 9.0 C/min. We are at 187 C, approaching the 197 C target. With 64 seconds of development time, we are at ~10% development ratio, aiming for 17.8%. I am reducing heat to 60% to manage the momentum and increasing fan to 70% to shift toward convective heat and ensure clean development as we approach the drop. We are not yet at the drop temperature or development target, so should_drop is false.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is currently experiencing a significant and concerning drop in bean temperature (RoR -16.0 C/min) despite being at 100% heat. This suggests a potential sensor error or a severe stall. I am maintaining maximum heat to attempt to recover the momentum and keeping fan low to minimize convective cooling while the bean temperature is this low.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is beginning to dip (10.3 C/min). To maintain momentum through the Maillard phase and approach first crack, I am maintaining full heat and increasing fan speed to 40% to improve convective heat transfer and ensure even development as the bean density changes.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=90% fan=70% drop=False conf=0.9 — 'First crack occurred at 638s. We are 12s into development with a RoR of 9.7C/min. The current bean temp is 182.7C, well below the target drop of 197.3C. To manage the development phase and ensure a smooth finish, I am increasing the fan to 70% to improve convective heat transfer and clearing smoke, while maintaining 90% heat to sustain the momentum without stalling.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.95 — 'The roast is at 193°C with a RoR of 10.3°C/min. We are approaching the target drop of 197.3°C. Given the current momentum and the proximity to the ceiling, I am maintaining full heat and fan to ensure a clean finish without stalling, while preparing to drop as soon as the target is reached. We are very close to the development target.'

## prompt_version = v5

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.82 dir=0.905; fan MAE=12.5 dir=0.524; latency pre=1.19s preFC=1.25s FC=1.22s
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
    |         hold |     0 |    17 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.6 dir=0.917; fan MAE=13 dir=0.375; latency pre=1.09s preFC=1.35s FC=1.4s
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
    |         hold |     2 |    18 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.56 dir=0.923; fan MAE=13.33 dir=0.538; latency pre=1.1s preFC=1.36s FC=1.4s
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
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/-0.3°C; heat MAE=7.62 dir=0.8; fan MAE=9.52 dir=0.45; latency pre=0.96s preFC=1.2s FC=1.03s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     4 |    12 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.3 dir=0.923; fan MAE=16.85 dir=0.346; latency pre=0.81s preFC=1.24s FC=1.38s
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
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-3.0s/-0.7°C; heat MAE=14.17 dir=0.739; fan MAE=13.75 dir=0.478; latency pre=1.27s preFC=1.28s FC=1.04s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     5 |    15 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.739 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/+0.0°C; heat MAE=10.95 dir=0.75; fan MAE=17.86 dir=0.35; latency pre=0.96s preFC=1.1s FC=1.28s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    12 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.75 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.57 dir=0.8; fan MAE=11.9 dir=0.45; latency pre=0.82s preFC=1.28s FC=1.25s
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
    |         hold |     4 |    12 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.57 dir=0.8; fan MAE=13.81 dir=0.4; latency pre=1.23s preFC=1.33s FC=1.76s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=12.61 dir=0.727; fan MAE=18.26 dir=0.273; latency pre=1.12s preFC=1.22s FC=1.17s
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
    |         hold |     6 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.727 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-10.0s/-1.7°C; heat MAE=14.09 dir=0.81; fan MAE=12.05 dir=0.333; latency pre=0.98s preFC=1.24s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=14 dir=0.708; fan MAE=13.2 dir=0.542; latency pre=1.04s preFC=1.12s FC=1.3s
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
    |         hold |     6 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.708 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.91 dir=0.857; fan MAE=18.64 dir=0.333; latency pre=1.07s preFC=1.19s FC=1.38s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=9.55 dir=0.81; fan MAE=13.64 dir=0.476; latency pre=1.18s preFC=1.2s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=14.58 dir=0.739; fan MAE=18.12 dir=0.348; latency pre=1.1s preFC=1.17s FC=1.14s
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
    |         hold |     5 |    14 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.739 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=4 dir=0.958; fan MAE=11.2 dir=0.458; latency pre=0.99s preFC=1.13s FC=1.29s
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
    |         hold |     1 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.958 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=10.48 dir=0.8; fan MAE=12.86 dir=0.4; latency pre=1.3s preFC=1.31s FC=1.2s
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
    |         hold |     4 |    12 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-6.0s/-0.6°C; heat MAE=11.54 dir=0.88; fan MAE=5 dir=0.84; latency pre=1.38s preFC=1.25s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-5.0s/-0.6°C; heat MAE=18.85 dir=0.76; fan MAE=18.46 dir=0.28; latency pre=1.5s preFC=1.15s FC=1.23s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     6 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.76 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/+0.0°C; heat MAE=13.75 dir=0.783; fan MAE=9.58 dir=0.522; latency pre=1.12s preFC=1.23s FC=1.16s
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
    |         hold |     3 |    14 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.69 dir=0.84; fan MAE=15.96 dir=0.44; latency pre=1.07s preFC=1.25s FC=1.12s
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
    |         hold |     3 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.69 dir=0.84; fan MAE=12.69 dir=0.4; latency pre=1.02s preFC=1.14s FC=1.25s
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
    |         hold |     3 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.61 dir=0.9; fan MAE=8.06 dir=0.533; latency pre=1.0s preFC=1.2s FC=1.24s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    24 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-17.0s/-2.0°C; heat MAE=9.58 dir=0.826; fan MAE=15.62 dir=0.391; latency pre=1.12s preFC=1.06s FC=1.43s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     1 |     0 |
    |         hold |     2 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-25.0s/-2.0°C; heat MAE=7.86 dir=0.852; fan MAE=11.79 dir=0.519; latency pre=1.12s preFC=1.15s FC=1.32s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    19 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.852 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-7.0s/-1.0°C; heat MAE=18.21 dir=0.815; fan MAE=15.54 dir=0.444; latency pre=1.07s preFC=1.13s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    20 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.815 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.75 dir=0.826; fan MAE=20.62 dir=0.304; latency pre=0.8s preFC=1.23s FC=1.3s
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
    |         hold |     4 |    16 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.48 dir=0.846; fan MAE=9.44 dir=0.5; latency pre=0.87s preFC=1.24s FC=1.36s
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
    |         hold |     4 |    18 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.846 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123C, which is below the target charge range of 180C-200C. I am maintaining heat and fan at 0 to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 131.3°C with no airflow. To prepare for the Maillard phase and ensure even heat distribution as we approach first crack, I am introducing moderate airflow (25%) while maintaining full heat to keep the RoR healthy. This transition to convective heat will help prevent scorching as the bean temperature rises.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack has just begun (7 seconds ago). The RoR is currently 11 C/min, which is healthy for this stage. To manage the development phase and reach the target drop temperature of 189 C without overshooting or stalling, I am maintaining heat to sustain momentum while increasing fan speed to 50% to transition toward convective heat transfer, ensuring even development and effective smoke evacuation as the roast progresses.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=80% fan=60% drop=False conf=0.95 — 'Current development ratio is 11.9% (67s / 561s), well below the 20.5% target. Bean RoR is stable at 8°C/min. To maintain momentum toward the 189°C drop target without overshooting or stalling, I am maintaining current heat and fan settings to ensure a smooth, controlled finish.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 121°C, which is approaching the target charge range (180°C-200°C). Heat and fan should remain off until the target charge temperature is reached to ensure a stable, controlled start.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 135C with no airflow. To prepare for the upcoming Maillard phase and ensure even heat distribution, I am introducing moderate airflow (25%) while maintaining full heat to keep the RoR from dropping prematurely. This transition to convective heat will help manage the bean temperature rise as we approach first crack.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=60% fan=50% drop=False conf=0.9 — 'Approaching first crack (173C indicated). RoR is slowing (10.7 C/min). To manage the transition into first crack and prevent a spike, I am reducing heat to 60% and increasing fan to 50% to shift toward convective heat and prepare for the exothermic reaction.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is 13.1% (86s / 654s), approaching the 17.9% target. With bean temp at 185C and RoR at 7.3C/min, we are closing in on the 190C drop target. To ensure a smooth finish without overshooting or stalling, I am reducing heat to 40% to manage the momentum and increasing fan to 80% to improve convective airflow for the final stage. We are approximately 30-40 seconds away from the target drop.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is currently in a severe stall/crash (RoR -14.3 C/min). The bean temperature is dropping rapidly despite 100% heat. This indicates a likely charge-related thermal deficit or sensor noise. I am maintaining 100% heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily but is still in the early stages. With a bean temperature of 131°C and a RoR of 12°C/min, we are approaching the Maillard phase. I am increasing the fan to 25% to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release, while maintaining full heat to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'The roast is in early development (17s post-FC). The current development ratio is ~3%, well below the 19% target. RoR is 10C/min, which is healthy. I am maintaining full heat to ensure momentum through the development phase while increasing fan to 50% to manage smoke and transition toward convective heat transfer as the bean structure opens. I will monitor for the need to cut heat as we approach the 190C target.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=100% fan=90% drop=False conf=0.95 — 'The current development ratio is 12.4% (77s / 619s), well below the 19% target. The RoR is low (8.6 C/min) but stable. The recent spike in heat to 100% is aggressive; I will maintain high heat to ensure we reach the target drop temperature of 190C without stalling, while keeping fan at 90% to manage the convective environment. We are approaching the drop temperature; monitoring closely for the 19% development mark.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (183C). It is ready to charge.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — "The roast is progressing steadily toward first crack. With a bean RoR of ~13.5 C/min and bean temp at 140.7 C, we are in the late Maillard phase. I am increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff of first crack, while maintaining 100% heat to ensure we don't stall before the crack begins."
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 11 C/min. I am maintaining full heat to ensure momentum through the transition, but increasing fan to 30% to prepare for the convective shift and smoke management as we enter the final stage. We are 15 degrees away from the target drop, so we need to keep the energy input steady but controlled.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.95 — 'The current development ratio is 10.3% (52s / 502s), well below the 15.7% target. The RoR is stable at 10°C/min. To reach the target drop temperature of 191.3°C while maintaining momentum, I will maintain current settings. The roast is progressing well toward the target.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature (156C) is below the recommended charge range (180C-200C). Maintaining zero heat and fan to continue soaking the drum until the target charge temperature is reached.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 134.7°C with no airflow. To prepare for the upcoming Maillard phase and first crack, I am introducing 20% fan to begin convective heat transfer and manage moisture/chaff, while maintaining 100% heat to keep the RoR from dropping prematurely as we approach the critical transition.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.95 — 'First crack has occurred (5 seconds ago). The RoR is currently 9.7 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to manage the momentum and increasing fan to 60% to shift toward convective heat and clear smoke, ensuring a controlled development toward the 191.7 C target. We are at ~1% development; the target is 19.5%, so we have significant time remaining.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=100% fan=50% drop=False conf=0.95 — 'Current development ratio is 13.3% (95s / 711s), well below the 19.5% target. Bean temperature is 183C, approaching the 191.7C drop target. RoR is low (6C/min). I am maintaining high heat to ensure we reach the target drop temperature without stalling, while keeping fan at 50% to maintain convective efficiency without crashing the RoR. We are not yet at the drop point.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (9 seconds elapsed), but the telemetry shows a significant and concerning drop in bean temperature (115C to 104C in 5 seconds). This suggests the charge temperature was insufficient or the bean mass is absorbing heat faster than the element can compensate. I am maintaining 100% heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further stalling.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 134°C. With RoR at 14°C/min and no fan usage yet, it is time to introduce airflow to transition toward convective heat transfer and prepare for the Maillard phase. I am increasing the fan to 25% to manage the environment and maintain momentum without stalling, while keeping heat at 100% to continue the climb toward first crack.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just begun (10s in). The RoR is healthy at 10C/min. To manage the development phase and avoid a spike, I am reducing heat to 70% to control the momentum and increasing fan to 50% to shift toward convective heat and clear smoke. We are far from the drop target (192.7C) and the development percentage (1.9% currently vs 20.7% target), so we continue roasting.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=60% fan=70% drop=False conf=0.95 — 'Current development ratio is ~12% (70s/581s), well below the 20.7% target. RoR is stable at 7.3 C/min. I am reducing heat to 60% to manage the approach to the 192.7 C drop target and increasing fan to 70% to shift toward convective heat, ensuring a clean finish without stalling. We are approaching the target drop temperature steadily.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (188C). It is ready to be charged.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 145.7°C. With RoR at 11.8°C/min and no first crack yet, I need to maintain momentum while preparing for the transition. I am increasing the fan to 25% to introduce convective heat and begin clearing moisture/chaff, while keeping heat at 100% to ensure we don't stall before the crack."
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=100% fan=40% drop=False conf=0.9 — "First crack has just begun (5 seconds in). The RoR is currently 10 C/min, which is healthy for this stage. To manage the development phase and prevent an uncontrolled spike, I am increasing the fan to 40% to introduce convective heat and clear smoke, while maintaining 100% heat for now to sustain momentum through the initial crack. I will monitor the RoR closely to ensure it doesn't crash or spike as we approach the 193 C drop target. Development is at ~1% of the total time; target is 17.2%."
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=20% fan=80% drop=False conf=0.95 — 'Current development ratio is 12.2% (65s / 532s), approaching the 17.2% target. With bean temp at 187.7C and RoR at 9.7C/min, we are closing in on the 193C drop target. Reducing heat to 20% to manage the final approach and increasing fan to 80% to clear smoke and transition to convective cooling for the final stretch. Not dropping yet; will reach target development in approximately 25-30 seconds.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (19 seconds elapsed). The telemetry shows a rapid drop in bean temperature, likely due to the initial charge and the probe stabilizing. Heat is at 100% and fan is at 0%, which is appropriate for the drying phase to build momentum. I will maintain these settings to ensure sufficient energy is stored in the drum for the upcoming Maillard phase.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The RoR has flattened significantly (12 C/min and stalling at 143 C). To prevent a stall and maintain momentum toward first crack, I need to increase convective heat transfer. I am increasing fan to 40% to improve airflow and keeping heat at 100% to push through the current plateau.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=60% fan=60% drop=False conf=0.95 — 'The roast is currently at 180°C with 18 seconds of development time (3.6% of total time). The target development ratio is 16.6%, which corresponds to approximately 100 seconds of development time. With a current RoR of 10°C/min, we have plenty of headroom to reach the 193°C drop target. I am reducing heat to 60% to manage the momentum and increasing fan to 60% to shift toward convective heat and ensure clean development as we approach the target.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=50% fan=70% drop=False conf=0.95 — 'The current development ratio is 9.05% (48s / 530s), well below the 16.6% target. The RoR is 10.6 C/min, which is healthy for this stage. I am maintaining current heat and fan settings to ensure a steady, controlled approach to the target drop temperature of 193 C without stalling the roast.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature of 183C. It is ready to receive the beans. Heat and fan are set to 0 for the charge.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR is dropping significantly (11.7 C/min) while still well below first crack. To prevent a stall and maintain momentum toward the target drop temperature, I am maintaining maximum heat. I am keeping the fan at 0% to maximize conductive heat transfer and ensure the bean temperature continues to climb steadily through the Maillard phase.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'The bean temperature is approaching the target drop range (176C vs 193C target). With a RoR of 10.3C/min, first crack is imminent. I am reducing heat to 70% to manage the momentum and increasing fan to 50% to transition toward convective heat transfer, ensuring a controlled entry into first crack without stalling.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.95 — 'Current development ratio is 9.7% (52s / 533s). The target is 15.3%, which corresponds to approximately 81 seconds of development time. We have ~29 seconds remaining. The RoR is stable at 10C/min. I am reducing heat to 60% to manage the final approach to 193C and increasing fan to 70% to ensure convective airflow for the final stage, preventing any risk of baking while approaching the drop temperature.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly despite 100% heat, indicating a likely sensor error or a severe stall immediately post-charge. Given the current reading of 110C and the downward trend, I am maintaining maximum heat to recover momentum. Fan remains at 0% to maximize conductive heat transfer during this recovery phase.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 129.3°C and a healthy RoR of 13.77°C/min, it is time to introduce airflow to transition toward convective heat transfer and prepare for the upcoming Maillard phase. I am maintaining full heat to ensure momentum is preserved while initiating fan speed.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 481s. Current development time is 14s. Total roast time is 495s. Development ratio is currently ~2.8%. Target development is 24.3% (approx 120s dev time). RoR is 10.3 C/min and stable. Heat is at 100%, which is too high for this stage; I need to reduce heat to manage the momentum and increase fan to 60% to transition to convective heat and clear smoke, preventing a spike or stall.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=60% fan=50% drop=False conf=0.95 — 'Current development ratio is 17.8% (104s / 585s), well below the 24.3% target. Bean RoR is 9.0 C/min and stable. With 8 degrees to go to the target drop temp, we need to maintain momentum without spiking. Heat is currently at 100%, which is too high for this stage; I am cutting heat to 60% to manage the final approach and increasing fan to 50% to shift toward convective heat and ensure clean development. Drop is not yet indicated.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (168C to 151C in 5 seconds) despite 100% heat. This indicates a severe sensor error or a massive thermal stall. Given the current reading of 151C, I must maintain maximum heat to attempt to recover the roast momentum before the beans stall completely. Fan remains at 0% to conserve all available thermal energy.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily at 142C. With an RoR of 12.3C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum through the Maillard phase, but increasing fan to 30% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is in the early development phase (13s post-crack). Current development ratio is ~2.5%, well below the 14% target. RoR is healthy at 11C/min. To manage the remaining development toward the 193.7C target, I am reducing heat to 60% to prevent an overshoot and maintaining moderate fan (60%) to ensure steady convective heat transfer without stalling the RoR.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is ~7.8% (43s / 554s). The target is 14%, which equates to ~77s total development time. With 34s remaining and a steady RoR of 10.3 C/min, the bean temp will reach ~193 C just as the development target is met. Heat is currently too high for this stage; reducing heat to 40% and increasing fan to 80% will manage the momentum and ensure a clean finish without stalling or hitting the 196 C ceiling.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roast is approaching the target drop temperature (180C current vs 194C target) and the RoR has dropped significantly, even turning slightly negative in the last few samples. With heat and fan currently at 0%, the roast is coasting. I am maintaining 0% heat and 0% fan to allow the bean temperature to continue its gentle, controlled climb toward first crack and the target drop temperature without stalling or crashing the RoR prematurely.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'RoR is healthy at 12.3 C/min and bean temp is 141.3 C. We are approaching the Maillard phase. Maintaining full heat is appropriate for now to ensure momentum, but I am increasing fan to 20% to begin shifting toward convective heat transfer and preparing for the smoke that will accompany the upcoming Maillard and first crack phases.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just begun (6 seconds ago). The RoR is still healthy at 11 C/min. To manage the development phase and avoid a spike, I am cutting heat significantly to 40% and increasing fan to 60% to shift toward convective heat and manage the momentum. The current development ratio is very low (1.2%), so we have plenty of time before reaching the 14.4% target.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=30% fan=70% drop=False conf=0.95 — 'Current development ratio is ~7% (36s / 513s). The target is 14.4%, which requires ~74s of development time. We are at 36s, so we need ~38s more. Bean RoR is 10C/min and stable. To reach the 194C target without overshooting or stalling, I am reducing heat to 30% to manage the momentum and increasing fan to 70% to shift toward convective cooling and ensure clean development as we approach the drop.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (143C to 127C in 5 seconds) while heat is at 100%. This indicates a sensor error or a massive thermal stall. Given the current state, I must maintain maximum heat to recover momentum and prevent the roast from stalling completely. Fan remains at 0 to maximize conductive heat transfer during this recovery phase.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 140C. With RoR at 12.7C/min and no sign of first crack yet, I need to maintain momentum while preparing for the transition. I am increasing the fan to 25% to introduce convective heat and begin clearing chaff, while keeping heat at 100% to ensure we don't stall before the crack."
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.95 — 'First crack has just occurred. The RoR is still quite high (10 C/min) with heat at 100% and no fan. To manage the development phase and prevent a runaway roast, I need to immediately reduce heat to control the momentum and introduce fan to shift to convective heat transfer, ensuring even development and smoke evacuation. I am targeting a drop at 194 C, which is 17 C away, so a moderate heat reduction is appropriate to maintain a controlled glide.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=70% fan=50% drop=False conf=0.95 — 'Current development ratio is 11.3% (63s / 559s), well below the 19.9% target. RoR is stable at 8.7 C/min. With 8 degrees to go until the 194 C drop target, I need to maintain momentum without spiking. Keeping heat at 70% and fan at 50% is appropriate to reach the target development ratio smoothly. I will monitor for a RoR crash as we approach the drop temperature.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (174C indicated, approaching the 180-200C guidance). I am setting heat and fan to 0 to ensure the roaster is ready for the charge command.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has flattened significantly (11.7 C/min and stalling at 141 C). To prevent a stall and maintain momentum toward first crack, I need to increase convective heat transfer and manage the environment. Increasing fan to 25% will help pull heat through the bean mass, and maintaining 100% heat is necessary to overcome the thermal lag and push through the current plateau.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.95 — 'First crack occurred at 494s. Current development time is 8s (1.6% of total time). The target development is 17.7%, which will be reached at approximately 600s total time. The RoR is currently 10C/min, which is appropriate for this stage. I am maintaining full heat to ensure momentum through the development phase while keeping the fan off to prevent an premature RoR crash, as we are still early in the development window.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=30% fan=100% drop=False conf=0.95 — 'Current development ratio is 12.2% (69s / 563s), approaching the 17.7% target. Bean RoR is low (9.0 C/min) and stable. With bean temp at 188 C and a target of 194.3 C, we are in the final stretch. Maintaining heat at 30% and fan at 100% to ensure a gentle, convective finish without stalling or risking a crash before the target drop temp.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=80% fan=20% drop=False conf=0.9 — 'The roast has just begun (1 second elapsed) and the heat/fan are currently at 0%. I need to initiate the roast by applying heat to build momentum. Setting heat to 80% and fan to 20% to start the drying phase while maintaining a controlled environment.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 130°C. With a RoR of 14°C/min, we are in a good position to approach the Maillard phase. I am maintaining full heat to ensure sufficient energy for the upcoming endothermic transition, but I am introducing 20% fan now to begin clearing moisture and preparing for the convective heat transfer needed as we approach first crack.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just begun. The RoR is currently 11.3 C/min, which is quite high for this stage. To prevent a runaway roast and ensure a controlled development phase, I am cutting heat significantly and increasing airflow to shift toward convective heat transfer, which will help manage the momentum and prevent scorching as we approach the target drop temperature.'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.95 — 'Current development ratio is ~15.2% (92s / 604s), well below the 22% target. Bean RoR is stable at 8C/min. With 9.7C remaining to reach the drop temperature, I need to maintain steady energy to avoid stalling while increasing airflow to manage the convective profile and clear smoke. Increasing fan to 50% and maintaining heat will support a controlled finish.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target environment temperature range (143.3C indicated). It is ready for charging. I am setting heat to 0 and fan to 0 to maintain the current state until the user initiates the charge.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 137.3C and a stable RoR, it is time to begin increasing airflow to transition toward convective heat transfer and prepare for the upcoming first crack. I am maintaining full heat for now to ensure momentum is preserved as we approach the crack, but increasing fan to 30% to manage the environment.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun (3 seconds in). The RoR is currently 9 C/min, which is healthy for this stage. To manage the development phase and reach the 195 C target without overshooting or stalling, I am reducing heat to 60% to manage the momentum and increasing fan to 60% to transition to convective heat and clear smoke. Development is only at 0.5% of the total time; we are far from the 13.4% target.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is 9.5% (63s / 659s). The target is 13.4%, which equates to a total development time of approximately 88 seconds. With 25 seconds remaining and a steady RoR of 11 C/min, the bean temperature will reach the 195 C target drop temperature right as the development ratio is met. Reducing heat slightly to 40% to manage the final approach while maintaining 80% fan for convective consistency.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (176C indicated, approaching the 180-200C guidance). I am ready to charge.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but is still well before first crack. The bean RoR is healthy (12.7 C/min). To prepare for the upcoming endothermic phase and first crack, I am increasing the fan to 30% to shift toward convective heat transfer and ensure smoke/chaff management, while maintaining full heat to keep momentum through the Maillard transition.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is in early development (12s post-FC). The current RoR is 10C/min, which is healthy but needs to be managed to avoid overshooting the 195C target. I am reducing heat to 60% to manage the momentum and increasing fan to 60% to shift toward convective heat and ensure clean development. The development ratio is currently ~2.7%, well below the 25.5% target.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=50% fan=85% drop=False conf=0.95 — 'Current development ratio is 19.1% (102s / 534s), well below the 25.5% target. Bean temperature is 188C with a steady RoR of 10.7C/min. To reach the target drop temperature of 195C while maintaining momentum, I am reducing heat to 50% to prevent a spike and increasing fan to 85% to ensure convective heat transfer and clean airflow as we approach the finish. We are approximately 60-90 seconds from the drop.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature (156C) is below the target charge range (180C-200C). I am setting heat to maximum to reach the charge temperature efficiently. Fan remains off to allow the drum to soak heat.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'The bean RoR has flattened significantly at 140C, and the environment temperature is stalling. With heat and fan already at 100%, the roast is struggling to maintain momentum toward first crack. I am maintaining maximum settings to push through this plateau, as any reduction now would likely stall the roast entirely before reaching the target drop temperature.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — "First crack has just begun (2 seconds in). The RoR is currently 10 C/min, which is appropriate for the start of development. I am maintaining high heat to ensure momentum through the crack while increasing fan to 50% to manage smoke and transition toward convective heat transfer for a clean finish. I will monitor the RoR closely to ensure it doesn't spike or crash as we approach the 195.3 C target."
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is 9.06% (62s / 684s), approaching the 13.6% target. Bean temp is 190.7C with a manageable RoR of 9.7C/min. To reach the target drop temp of 195.3C without overshooting or stalling, I am cutting heat to 40% to manage the momentum while maintaining 80% fan for convective consistency. We are approximately 30-40 seconds from the drop.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (charge/drying). Heat is at maximum to build momentum. Fan remains at 0% to retain heat and build energy in the drum. No changes are needed at this early stage.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has flattened significantly (10.7 C/min and stalling at 137 C) despite 100% heat. The roast is stalling before reaching first crack. I need to increase airflow to introduce convective heat transfer and help push the bean temperature forward, while maintaining maximum heat to overcome the stall.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'The bean temperature is approaching the critical 180C-185C range where first crack typically initiates on this roaster. With RoR at 10.3 C/min and heat at 100%, I need to begin reducing energy input now to prevent an uncontrolled spike during first crack. I am introducing airflow to shift toward convective heat and prepare for smoke evacuation, while cutting heat to manage the momentum.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=0% fan=70% drop=False conf=0.95 — 'Current development ratio is 7.9% (54s / 680s). The target is 12.4%, which requires approximately 84 seconds of development time (total time ~710s). With bean temp at 189C and RoR at 10C/min, we are approaching the 195.3C target. I am cutting heat to 0% to manage the momentum and increasing fan to 70% to transition to convective cooling and ensure a clean finish as we approach the drop. We are not yet at the target development percentage, so should_drop is false.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the very early stages (drying/early yellowing). The bean temperature is dropping rapidly in the telemetry samples, likely due to the initial charge impact and the probe settling. Heat is at maximum to drive the roast forward, and fan is low to retain heat. Maintain full heat to build momentum through the drying phase.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — "The roast is progressing steadily through the Maillard phase. With bean temperature at 139C and RoR at 13.3C/min, we are approaching the transition to first crack. I am increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for smoke evacuation, while maintaining full heat to ensure we don't stall before the crack."
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=80% fan=50% drop=False conf=0.95 — 'First crack occurred at 522s. Current development time is 6s (1.1% of total time). The target development is 19.6% (approx 103s total development time). RoR is currently 9.8 C/min and stable. I am maintaining heat at 80% and fan at 50% to ensure steady, convective development without stalling, while keeping the bean temperature well below the 196C ceiling.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=50% fan=90% drop=False conf=0.95 — 'Current development ratio is 11.2% (66s / 588s), well below the 19.6% target. Bean temperature is 188°C with a stable RoR of 9°C/min. I am maintaining heat at 50% and increasing fan to 90% to ensure convective heat transfer and smoke evacuation as we approach the 196°C ceiling. We are on track to hit the development target before reaching the drop temperature.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is only 20 seconds in and the bean temperature is dropping rapidly (-27 C/min RoR) despite 100% heat. This indicates the charge temperature was likely too low or the beans are absorbing significant energy to overcome the initial thermal mass. I must maintain maximum heat to recover the RoR and prevent a stall. Fan remains at 0% to maximize conductive heat transfer during this recovery phase.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 135°C. With RoR at 13°C/min and no first crack yet, I need to maintain momentum while preparing for the transition to convective heat. Increasing the fan now will help clear chaff and prepare for the upcoming Maillard phase, while keeping heat at 100% ensures we don't stall before the crack."
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just begun (3 seconds ago). The RoR is still healthy at 10C/min. To manage the development phase and avoid a spike, I am cutting heat significantly to anticipate thermal lag and increasing fan to shift toward convective heat, ensuring a controlled development toward the 196C target. Development is only at 0.5% of the total time, so we are far from the 16.5% target.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=60% fan=90% drop=False conf=0.95 — 'Current development ratio is 9.6% (63s / 652s). The target is 16.5%, which equates to approximately 107 seconds of development time. With 44 seconds remaining to reach the target and a steady RoR of 9.0 C/min, the roast is progressing well toward the 196 C ceiling. Maintaining current settings is appropriate to ensure a smooth, controlled finish without stalling or overshooting the target temperature.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature (139C) is well below the target charge range (180C-200C). Maintaining zero heat and fan to allow the drum to continue soaking and rising toward the target charge temperature.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has flattened significantly (11 C/min and stalling at 137 C). With heat at 100% and fan at only 10%, the roast is stalling due to insufficient energy transfer and lack of airflow. I am increasing heat to maintain momentum and raising the fan to 30% to improve convective heat transfer and prevent a stall before first crack.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just begun (3 seconds ago). The RoR is currently 10.7 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to begin the glide toward the drop temperature and increasing fan to 40% to shift toward convective heat and clear smoke, ensuring a clean development phase. We are far from the 14.6% development target, so we continue roasting.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is 9.2% (63s / 683s), well below the 14.6% target. Bean RoR is 9.3 C/min and stable. I am reducing heat to 40% to manage the approach to the 196.3 C drop target and prevent a spike, while maintaining 80% fan to ensure convective heat and smoke evacuation. We are approaching the drop window; will monitor for the RoR to flatten as we hit the target.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is experiencing a severe crash in RoR (-17.7 C/min) immediately following the charge/early phase. The bean temperature is dropping rapidly despite 100% heat. This indicates the drum was likely under-charged or the thermal energy was insufficient to overcome the initial heat sink. I am maintaining 100% heat and keeping fan low (10%) to maximize conductive energy transfer and attempt to recover the RoR before the roast stalls completely.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 135C. With a bean RoR of 10.3C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure sufficient momentum through the Maillard phase while slightly increasing fan to 20% to begin preparing for convective heat transfer and smoke management as we approach the crack.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.95 — 'First crack has just begun (4 seconds in). The RoR is still quite high (11.3 C/min) for this stage. To manage the development phase and prevent an uncontrolled spike, I am cutting heat significantly and increasing airflow to shift toward convective heat transfer, which will help stabilize the roast and prevent scorching as we approach the target drop temperature. We are far from the target development percentage (12.7%), so we need to manage the momentum now.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=0% fan=100% drop=False conf=0.95 — 'Current development ratio is 9.47% (64s / 676s), approaching the 12.7% target. Bean temperature is 191C with a steady RoR of 13C/min. To reach the target drop temp of 196.3C without overshooting or stalling, I am cutting heat to 0% to coast into the finish while maintaining maximum airflow to ensure clean development and prevent baking. Drop is imminent.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (180-200C). The system is ready for the beans to be charged. No heat or fan is required during this pre-charge state.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 125C. With RoR at 14.3C/min and no fan usage yet, it is time to introduce airflow to manage the transition into the Maillard phase and prevent potential scorching as the bean density changes. Increasing fan to 25% will improve convective heat transfer while maintaining heat at 100% to keep momentum toward first crack.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=80% fan=50% drop=False conf=0.9 — 'The roast is approaching the end of the Maillard phase. With a bean RoR of 12 C/min and bean temp at 167.7 C, we are nearing first crack. I am reducing heat to 80% to manage the momentum and increasing fan to 50% to transition toward convective heat transfer, ensuring a clean, even development as we enter the final stage.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=90% fan=30% drop=False conf=0.95 — 'Current development ratio is 14.3% (85s / 595s), well below the 24.1% target. Bean RoR is 12 C/min, which is healthy for this stage. I am increasing heat to 90% to maintain momentum toward the 197 C drop target, while keeping fan at 30% to ensure sufficient convective heat transfer without stalling the roast. We are approaching the ceiling, so I will monitor closely for the next adjustment.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target environment temperature range (165.7C). It is ready for charging. I am setting heat and fan to 0 as per standard pre-charge protocol to ensure a clean start once the beans are dropped.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The bean RoR has flattened significantly (9.3 C/min and stalling at 134 C) despite 100% heat. The roast is stalling before reaching first crack. I need to introduce airflow to shift to convective heat transfer and help pull the roast through the Maillard phase, while maintaining maximum heat to recover momentum.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.95 — 'Development is at 19s/656s (~2.9%). The target is 14% (approx 92s total dev time). RoR is 11.7 C/min and stable. I am reducing heat to 50% to manage the momentum and increasing fan to 60% to shift toward convective heat, ensuring a controlled, steady climb toward the 197C drop target without stalling or spiking.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=0% fan=60% drop=False conf=0.95 — 'Current development ratio is ~7.1% (49s / 686s). The target is 14%, which equates to ~96 seconds of development time. With RoR at 12.7 C/min and bean temp at 189 C, we are approaching the drop target of 197 C. I am cutting heat to 0% to coast to the finish and maintaining fan at 60% to ensure clean convective heat and smoke evacuation without stalling the roast. We are not yet at the development target, so should_drop is false.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (RoR -7.377 C/min) despite 100% heat. This indicates a stall or a measurement error, but given the current state, I must maximize energy input to recover momentum before the roast stalls completely. Keeping heat at 100% and maintaining low fan to minimize convective cooling is the only viable path to recover the RoR.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 134°C. With RoR at 13.3°C/min, we are in a good position to transition into the Maillard phase. I am increasing the fan to 25% to begin introducing convective heat and preparing for the smoke that will accompany the upcoming Maillard phase, while maintaining full heat to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.95 — 'First crack has just begun (6 seconds ago). The RoR is still quite high (11 C/min) and heat is at 100% with no fan. To manage the development phase and prevent a runaway roast, I am cutting heat significantly and introducing airflow to shift to convective heat transfer, which will help stabilize the RoR as we approach the target drop temperature.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=100% fan=60% drop=False conf=0.95 — 'The current development ratio is 11.3% (66s / 583s), well below the 20.5% target. The RoR has flattened at 10°C/min, which is stable. To reach the target development without hitting the 197°C ceiling too quickly, I am maintaining heat to sustain momentum while increasing fan to 60% to shift toward convective heat and ensure clean development as we approach the final stages.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (91C) despite 100% heat, suggesting a potential sensor error or a severe stall. Given the very early stage of the roast (30s), I am maintaining maximum heat to recover momentum. Fan remains at 0 to maximize conductive heat transfer.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 132C with 0% fan. To prepare for the Maillard phase and ensure efficient heat transfer as we approach first crack, I am increasing the fan to 30% to introduce convective heat and begin clearing moisture/chaff, while maintaining full heat to keep the RoR from dropping prematurely.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just begun (4 seconds in). The RoR is still quite high (10.7 C/min) and heat is at 100% with no fan. To manage the development phase and prevent a spike, I need to introduce convective airflow to stabilize the roast and begin reducing heat to control the momentum toward the 197 C drop target.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.95 — 'Current development ratio is ~10.3% (64s / 621s), well below the 17.8% target. Bean RoR is 9.0 C/min, which is healthy but needs to be managed to avoid overshooting the 197 C drop target. I am reducing heat to 60% to gently taper the RoR and increasing fan to 70% to shift toward convective heat, ensuring a clean finish without stalling. We are approaching the drop window; will monitor for the 197 C target or the development ratio completion.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has plummeted to -16.0 C/min, which is highly abnormal for this stage of the roast. The bean temperature is dropping rapidly despite 100% heat. This suggests a sensor error or a severe stall. I am maintaining maximum heat to attempt to recover the momentum, but the situation is critical.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'RoR is slowing significantly (10.3 C/min) as we approach the Maillard phase. To maintain momentum without stalling, I am increasing airflow to 40% to shift toward convective heat transfer, while keeping heat at 100% to compensate for the increased cooling effect of the fan and ensure we reach first crack with sufficient energy.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=70% fan=80% drop=False conf=0.9 — 'The roast is in early development (12s post-FC). The current development ratio is ~1.8%, well below the 13.6% target. RoR is healthy at 9.7 C/min. To manage the momentum and ensure a smooth finish without hitting the 196 C ceiling too abruptly, I am reducing heat to 70% and increasing fan to 80% to shift toward convective heat transfer, preventing scorching while maintaining steady development.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.95 — 'The current development ratio is 10.1% (72s / 710s), approaching the target of 13.6%. With a bean temperature of 193°C and a RoR of 10.3°C/min, the roast is progressing steadily toward the 197.3°C drop target. Given the proximity to the target temperature and the development goal, maintaining current settings is appropriate to ensure a smooth finish without stalling or overshooting the 196°C ceiling.'

## prompt_version = v6

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=15 dir=0.905; fan MAE=10.91 dir=0.619; latency pre=1.56s preFC=1.12s FC=1.25s
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
    |         hold |     0 |    17 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-8.0s/-1.0°C; heat MAE=11.2 dir=0.875; fan MAE=11 dir=0.5; latency pre=1.25s preFC=1.13s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.78 dir=0.923; fan MAE=10 dir=0.654; latency pre=1.24s preFC=1.24s FC=1.39s
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
    |         hold |     1 |    21 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/-0.3°C; heat MAE=5.24 dir=0.95; fan MAE=8.81 dir=0.5; latency pre=1.15s preFC=1.16s FC=1.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.95 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.3 dir=0.962; fan MAE=15.19 dir=0.385; latency pre=1.01s preFC=1.23s FC=1.29s
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
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.962 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-3.0s/-0.7°C; heat MAE=10.83 dir=0.87; fan MAE=11.46 dir=0.522; latency pre=1.05s preFC=1.14s FC=1.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/+0.0°C; heat MAE=7.14 dir=0.8; fan MAE=15 dir=0.4; latency pre=0.82s preFC=1.15s FC=1.1s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.24 dir=0.9; fan MAE=9.29 dir=0.6; latency pre=0.9s preFC=1.1s FC=1.33s
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
    |         hold |     2 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-5.0s/-2.0°C; heat MAE=6.67 dir=0.9; fan MAE=10.24 dir=0.55; latency pre=1.02s preFC=1.19s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=0.667 P=0.5 R=1.0 timing=-20.0s/-4.0°C; heat MAE=12.17 dir=0.909; fan MAE=10.87 dir=0.591; latency pre=0.98s preFC=1.12s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=21     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.909 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-10.0s/-1.7°C; heat MAE=9.09 dir=0.952; fan MAE=5.91 dir=0.667; latency pre=0.92s preFC=1.11s FC=1.12s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.952 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.8 dir=0.917; fan MAE=7.8 dir=0.667; latency pre=1.05s preFC=1.16s FC=1.26s
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
    |         hold |     1 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-30.0s/-5.0°C; heat MAE=8.64 dir=0.857; fan MAE=17.73 dir=0.333; latency pre=1.1s preFC=1.24s FC=1.25s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-7.0s/-1.3°C; heat MAE=8.64 dir=0.857; fan MAE=12.95 dir=0.429; latency pre=1.03s preFC=1.21s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=18.33 dir=0.87; fan MAE=13.54 dir=0.478; latency pre=1.05s preFC=1.1s FC=1.29s
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
    |         hold |     2 |    17 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6 dir=1.0; fan MAE=8.6 dir=0.5; latency pre=0.96s preFC=1.2s FC=1.14s
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
    |         hold |     0 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=1.0 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=0.5 P=0.333 R=1.0 timing=-46.0s/-7.0°C; heat MAE=8.57 dir=0.95; fan MAE=7.86 dir=0.55; latency pre=1.27s preFC=1.12s FC=1.14s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=18     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.95 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-6.0s/-0.6°C; heat MAE=7.69 dir=0.88; fan MAE=10.38 dir=0.8; latency pre=1.02s preFC=1.18s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-5.0s/-0.6°C; heat MAE=7.69 dir=0.92; fan MAE=14.04 dir=0.36; latency pre=1.04s preFC=1.17s FC=1.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     2 |    22 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.5 P=0.333 R=1.0 timing=-31.0s/-4.0°C; heat MAE=15.83 dir=0.783; fan MAE=7.5 dir=0.652; latency pre=1.03s preFC=1.31s FC=1.33s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=21     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=0.5 P=0.333 R=1.0 timing=-53.0s/-9.0°C; heat MAE=8.46 dir=0.92; fan MAE=9.62 dir=0.6; latency pre=0.97s preFC=1.29s FC=1.32s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=23     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     1 |    21 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-13.0s/-2.3°C; heat MAE=8.85 dir=0.92; fan MAE=7.69 dir=0.6; latency pre=1.05s preFC=1.2s FC=1.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=9.03 dir=0.967; fan MAE=6.13 dir=0.6; latency pre=1.09s preFC=1.13s FC=1.06s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     0 |    26 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.967 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=0.5 P=0.333 R=1.0 timing=-47.0s/-7.0°C; heat MAE=12.5 dir=0.87; fan MAE=10.21 dir=0.565; latency pre=1.08s preFC=1.15s FC=1.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=21     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-25.0s/-2.0°C; heat MAE=6.79 dir=0.889; fan MAE=10.71 dir=0.556; latency pre=1.02s preFC=1.2s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.889 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.5 P=0.333 R=1.0 timing=-37.0s/-6.0°C; heat MAE=13.21 dir=0.889; fan MAE=11.79 dir=0.519; latency pre=1.23s preFC=1.13s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=25     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    22 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.889 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-27.0s/-5.0°C; heat MAE=14.58 dir=0.87; fan MAE=17.5 dir=0.348; latency pre=1.55s preFC=1.18s FC=1.35s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=4.44 dir=0.962; fan MAE=7.41 dir=0.615; latency pre=1.14s preFC=1.27s FC=1.16s
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
    |         hold |     1 |    21 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.962 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123°C and the environment temperature is 160°C. The charge guidance is 180°C-200°C. I am setting the heat to 100% to reach the target charge temperature efficiently. Fan is kept at 0% to allow the drum to heat up effectively before charging.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 131.3C and a healthy RoR of 14.6C/min, we are well-positioned. I am maintaining full heat to ensure sufficient momentum for the upcoming Maillard phase, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach first crack.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'First crack occurred 7 seconds ago. We are in the very early stages of development. The RoR is healthy (11 C/min), and we are far from the floor (minimum 60s development) and the ceiling (189 C target). Maintaining full heat and current fan to ensure momentum through the early development phase. No changes needed yet.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=40% fan=80% drop=False conf=0.9 — 'The roast is at 67 seconds into development with a bean temperature of 181°C. The RoR is stable at 8°C/min. We are approaching the target drop temperature of 189°C. Given the current development time and the steady progress, we are nearing the floor. I am reducing heat to 40% to manage the final approach and increasing fan to 80% to ensure convective heat transfer and smoke evacuation as we approach the drop window. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 121°C, which is well below the target charge range of 180°C-200°C. I am maintaining zero heat and fan to allow the drum to continue soaking and reaching the target charge temperature.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is still well before first crack. With bean temperature at 135.3C and a moderate RoR of 12C/min, I am maintaining full heat to ensure sufficient energy for the upcoming Maillard phase and first crack. Fan remains at 0% to maximize conductive heat transfer during this stage.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=70% fan=50% drop=False conf=0.9 — 'The roast is approaching the end of the Maillard phase. With a bean RoR of 10.7 C/min and a bean temperature of 173 C, we are nearing first crack. I am reducing heat to 70% to manage the momentum and increasing fan to 50% to transition toward convective heat transfer, ensuring a smooth entry into first crack without a spike.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=40% fan=80% drop=False conf=0.95 — 'The roast has reached 86 seconds of development and 185°C, which is approaching the target drop of 190°C. The RoR has slowed significantly to 7.3°C/min, indicating a healthy, controlled progression. Given the proximity to the target drop temperature and the sufficient development time, I am initiating the final approach. I am reducing heat to 40% to prevent any late-stage flick and increasing fan to 80% to ensure clean convective heat for the final seconds. Should_drop is set to false for this cycle, but we are within 5°C of the target.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (RoR -14.3 C/min) despite maximum heat input. This indicates a stall or a significant measurement anomaly following the charge. I am maintaining maximum heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling. The roast is in a critical recovery phase.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is still well before first crack. With bean temperature at 131C and a moderate RoR of 12C/min, I am maintaining full heat to ensure sufficient energy for the upcoming Maillard phase and first crack. I am keeping the fan at 0% to maximize conductive heat transfer and build momentum, but I will prepare to introduce airflow as we approach the 150-160C range to manage smoke and transition toward convective heat.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=40% drop=False conf=0.9 — "First crack occurred 17 seconds ago. The roast is still very early in the development phase (only 17s in). The RoR is healthy at 10C/min. I need to maintain momentum to reach the target drop temperature of 190C without stalling, but I will begin a gradual increase in fan speed to manage the convective heat transfer as we approach the end of the roast. Heat remains at 100% to ensure we don't stall before reaching the development floor."
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=0% fan=90% drop=False conf=0.9 — 'The roast is at 183.3C with 77 seconds of development time. The RoR is stable at 8.6C/min. We are approaching the target drop temperature of 190C. The recent spike in heat to 100% is unnecessary and risks a flick; I am cutting heat to 0% to coast to the drop temperature while maintaining high airflow to ensure convective development and prevent baking. We are within the development window and approaching the drop target.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C is within the 180-200C guidance). The system is ready for charging.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily at 140.7°C. With a RoR of ~13.5°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure sufficient momentum through the Maillard phase, while keeping fan low to preserve conductive heat transfer for now. No changes needed yet.'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is approaching first crack (176C indicated). With a RoR of 11C/min, I need to prepare for the exothermic transition. I am maintaining full heat to ensure momentum through the crack, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing to manage smoke/chaff as we enter the development phase.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.95 — 'The roast is at 52 seconds post-first crack with a bean temperature of 186°C. The target drop temperature is 191.3°C. We are approaching the floor for development (60s+), and the RoR is stable at 10°C/min. I am maintaining current heat and fan settings to ensure a smooth, controlled approach to the target drop temperature without risking a flick or stalling the roast. We are well below the ceiling and the flick guard, so we continue until the target drop temperature is reached.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 125.0°C, which is well below the target charge range of 180.0°C - 200.0°C. I am maintaining heat and fan at 0 to allow the drum to continue soaking up thermal energy to reach the target charge temperature.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase at 134.7C. With RoR at 11.7C/min and heat at 100%, we have sufficient momentum. I am maintaining full heat to ensure we reach first crack without stalling, but I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the crack.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — "First crack occurred 15 seconds ago. The roast is in the very early stages of development. RoR is stable at 9.7 C/min. I need to maintain momentum to reach the development target while managing the heat to avoid a flick. Keeping heat at 100% for now to ensure we don't stall, but I am increasing fan to 50% to transition toward convective heat transfer and prepare for the upcoming development phase. We are well below the floor and ceiling."
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=40% fan=70% drop=False conf=0.9 — 'The roast has reached 95 seconds of development and a development ratio of ~13.4%. While this is below the 19.5% target, the bean RoR has flattened significantly (6.0 C/min) and the bean temperature is approaching the drop target. Given the current heat setting (100%) and the risk of a flick or overshooting the ceiling, I am cutting heat to manage the final approach and increasing fan to convective mode to ensure even development. I am not dropping yet as we are still below the target development ratio, but we are approaching the window.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (9 seconds elapsed). The telemetry shows a significant, erratic drop in bean temperature (115C to 104C), likely due to a sensor reading error or extreme instability immediately post-charge. Since the heat is already at 100% and fan at 0%, I will maintain maximum heat to recover momentum and establish a stable drying phase. No fan is needed yet to allow the drum to build thermal energy.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 134C and a healthy RoR of 14C/min, we are approaching the transition to late Maillard. I am maintaining full heat to ensure sufficient momentum, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke evacuation needs as we approach first crack.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — "First crack occurred 10 seconds ago. We are in the very early stages of development (10s elapsed). The bean RoR is 10 C/min, which is healthy. We need to maintain momentum to reach the target drop temperature of 192.7 C without stalling, but we should begin preparing for the development phase by increasing airflow to transition toward convective heat transfer and manage the smoke from first crack. Keeping heat at 100% for now to ensure we don't crash the RoR, but increasing fan to 50% to improve airflow. We are well below the development floor."
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=60% fan=70% drop=False conf=0.9 — 'The roast is at 70s post-first crack with a bean temperature of 183C. The RoR has slowed significantly to 7.3C/min. We are approaching the target drop temperature of 192.7C. To ensure a smooth finish without a flick, I am reducing heat to 60% and increasing fan to 70% to transition to convective cooling and manage the remaining momentum. We are nearing the development floor, so I will prepare to drop as we approach the target temperature.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and is within the recommended charge temperature range (180-200C). The bean temperature is 188C. I am ready to charge the beans.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 145.7C and a healthy RoR of ~11.8C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but initiating a modest fan increase to 20% to begin transitioning toward convective heat transfer and preparing for the smoke evacuation needed during first crack.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=80% fan=40% drop=False conf=0.9 — 'First crack has just begun (5s in). The RoR is healthy at 10C/min. We are far from the development floor (approx 60s post-crack) and the target drop temp. Heat is currently at 100% with no fan; I need to introduce airflow to shift toward convective heat transfer to ensure even development and manage the upcoming exothermic energy release, while keeping heat high enough to maintain momentum toward the target.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.9 — 'The roast is at 65 seconds post-first crack with a bean temperature of 187.7°C. The RoR is 9.7°C/min, which is healthy and stable. We are approaching the target drop temperature of 193°C. Given the development time is already 65 seconds and the RoR is not showing signs of a flick, I am maintaining current settings to reach the target drop temperature smoothly. I will be ready to drop once the temperature hits the target range.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature drop (139C to 125C) is likely a probe artifact or a result of the charge, but the environment temperature is stable. Maintaining 100% heat is appropriate to build momentum for the Maillard phase. Fan remains at 0% to maximize conductive heat transfer during the initial phase.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The roast is stalling at 143C with 100% heat. The RoR has dropped to zero. I need to increase airflow to improve convective heat transfer and ensure the beans are moving properly to prevent scorching while maintaining maximum heat to push through the stall.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=80% fan=40% drop=False conf=0.9 — 'Development is only at 18 seconds post-first crack, well below the minimum development floor. The RoR is stable at 10C/min. I need to maintain momentum to reach the target drop temperature of 193C without stalling. Keeping heat at 80% and fan at 40% is appropriate to continue the development phase steadily.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=50% fan=70% drop=False conf=0.9 — 'Development time is 48s, which is below the 60s floor. RoR is healthy at 10.6 C/min. I am maintaining current settings to allow the roast to reach the development floor without stalling or flicking. Will reassess for drop once development time exceeds 60s and bean temp approaches 190-192 C.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature of 183°C. It is ready to be charged.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With bean temperature at 143C and a moderate RoR of 11.7C/min, I am maintaining full heat to ensure sufficient momentum through the Maillard phase. I am keeping the fan at 0% to maximize conductive heat transfer and build energy, but I will prepare to introduce airflow as we approach first crack to manage the transition to convective heat.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'Approaching first crack (typically ~180-182C on this probe). RoR is healthy at 10.3 C/min. Maintaining full heat to ensure sufficient momentum into first crack, but increasing fan to 50% now to prepare for the exothermic phase and ensure clean airflow as we approach the crack.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=40% fan=70% drop=False conf=0.9 — 'Development time is 52s, approaching the 60s floor. RoR is 10C/min and stable. Bean temp is 186C, well below the 193C target and 196C ceiling. I will reduce heat to manage the momentum and increase fan to 70% to shift toward convective heat, ensuring a smooth, non-baked finish as we approach the drop window.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (122C to 110C in 5 seconds), indicating a significant thermal stall or measurement artifact following the charge. Heat is already at 100%. I will maintain maximum heat to recover momentum and keep the fan at 0% to maximize conductive heat transfer until the bean temperature stabilizes and begins to climb steadily.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 129.3C and a healthy RoR, I am maintaining full heat to ensure sufficient momentum for the upcoming Maillard and first crack phases. Fan remains at 0% to maximize conductive heat transfer and build energy in the drum before the transition to convective cooling later.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=100% fan=40% drop=False conf=0.9 — "First crack occurred 14 seconds ago. The RoR is currently 10.3 C/min and stable, but the bean temperature is still well below the development floor. I need to maintain momentum to reach the target development time without stalling. Keeping heat at 100% and fan at 40% is appropriate for this early stage of development to ensure we don't crash the RoR before reaching the necessary development ratio."
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=0% fan=60% drop=False conf=0.95 — 'The roast has reached 104 seconds of development, well past the 60-second floor, and the development percentage is currently ~17.8%. While this is below the 24.3% target, the bean temperature is 185°C and rising. To reach the target development without hitting the ceiling (196°C) or causing a flick, I am cutting heat to 0% to coast and increasing fan to 60% to manage the convective heat and clear smoke. I am not dropping yet as the development percentage is still low, but I am preparing for a drop within the next 2-3 minutes.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (168C to 151C in 5 seconds) despite 100% heat. This indicates a sensor error or a massive thermal stall. Given the current state, I must maintain maximum heat to recover momentum and prevent the roast from stalling completely before reaching first crack. Fan remains at 0% to conserve all available energy.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 142C and a healthy RoR of 12.3C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum and increasing the fan slightly to 20% to begin preparing for convective heat transfer and smoke management as we approach the crack.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=80% fan=60% drop=False conf=0.9 — 'Development is only at 13 seconds post-FC, well below the floor. RoR is healthy at 11 C/min. I am maintaining heat to ensure momentum toward the target drop temp while keeping fan moderate to balance convective heat transfer without stalling the roast. We are far from the ceiling and the floor, so steady progress is the goal.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=80% fan=60% drop=False conf=0.9 — 'Development time is currently 43s, which is below the 60s floor. RoR is 10.3 C/min and stable. I am maintaining heat and fan to ensure we reach the development floor without stalling, while preparing to drop as soon as we hit the 60s mark or approach the target drop temp of 193.7 C.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roast is approaching the target drop temperature (180C current vs 194C target) and is in the final stages before first crack. Heat and fan are currently off, which is appropriate to manage the momentum as we approach the end of the roast. I will maintain these settings to allow the bean temperature to coast toward the target without risking a spike or stall.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With a bean RoR of 12.3 C/min and bean temp at 141.3 C, we are in the late Maillard phase. I am maintaining full heat to ensure sufficient momentum into first crack while slightly increasing the fan to 20% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=100% fan=40% drop=False conf=0.9 — "First crack occurred 6 seconds ago. The RoR is currently 11 C/min and stable. We are in the very early stages of development (6s in, target is ~14%). I need to maintain momentum to reach the target drop temp of 194 C without stalling, but I must prepare for the post-crack energy surge. I am keeping heat at 100% to ensure we don't stall, but increasing fan to 40% to transition toward convective heat and manage the environment temperature as we approach the drop window."
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=50% fan=50% drop=False conf=0.9 — 'The roast is currently at 36 seconds post-first crack, which is well below the minimum development floor (60s). The bean temperature is 187°C, approaching the target drop of 194°C. RoR is stable at 10°C/min. I will maintain current settings to ensure sufficient development time without risking a flick or stalling, keeping the heat/fan balance steady until we hit the 60-second mark.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (143C to 127C in 5 seconds), which is highly abnormal and suggests a sensor error or a massive stall. Given the heat is at 100% and the environment temperature is also dropping, I am maintaining maximum heat to recover momentum. I am keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling. I need to see if the bean temperature stabilizes and begins to rise again.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 140C and RoR at 12.7C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the upcoming smoke/chaff release, which will help stabilize the RoR as we approach the crack.'
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just begun (2 seconds ago). The roast is currently at 177C with 100% heat and 0% fan. I need to initiate the transition to convective heat transfer to manage the development phase and prevent a flick. I am reducing heat to 60% and increasing fan to 50% to stabilize the RoR and begin the development window. We are well below the floor, so should_drop is false.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=50% fan=70% drop=False conf=0.9 — 'The roast is at 186°C with 63 seconds of development time. The RoR is stable at 8.7°C/min. We are approaching the target drop temperature of 194°C. To ensure a smooth finish without a flick, I am reducing heat to 50% and increasing fan to 70% to shift toward convective cooling and manage the final approach. We are nearing the development floor, so I will prepare to drop as soon as the target temperature is reached.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (174C is close to the 180C minimum). I am recommending a heat setting of 0 and fan of 0 to stabilize the environment before charging. The user should charge the beans once the target charge temperature is reached.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is beginning to flatten (11.7 C/min). With the bean temperature at 141 C, we are approaching the Maillard phase. I am maintaining full heat to ensure sufficient momentum into first crack, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke management required as we approach first crack.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'First crack occurred 8 seconds ago. The roast is in the very early stages of development. The bean RoR is healthy (10 C/min), and the bean temperature (179 C) is well below the target drop temperature (194.3 C). I am maintaining full heat to ensure momentum through the development phase, but I will keep the fan off for now to maintain conductive heat transfer until the RoR begins to naturally decline. No drop yet.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=30% fan=100% drop=False conf=0.95 — 'The roast is at 188°C with 69 seconds of development time. The RoR is stable at 9°C/min. We are approaching the target drop temperature of 194.3°C. Given the current trajectory and the need to avoid overshooting the ceiling, I am maintaining heat at 30% to ensure a steady, controlled finish while keeping the fan at 100% for maximum convective efficiency. We are very close to the drop window; I will monitor closely for the next 2-3 minutes to hit the target.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=80% fan=20% drop=False conf=0.9 — 'The roast has just begun (1 second elapsed). The telemetry shows the heat and fan are currently at 0%, which is inappropriate for this stage. I am initiating heat to build momentum and setting a low fan to begin clearing moisture and preparing for the Maillard phase.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 130C and a healthy RoR of 14C/min, I am maintaining full heat to build momentum toward first crack. I am keeping the fan at 0% to maximize conductive heat transfer and ensure the drum is fully saturated with energy before the endothermic phase intensifies.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.95 — "First crack has just begun (1s elapsed). The RoR is healthy at 11.3 C/min. I need to maintain momentum to reach the development target while preparing for the transition to convective cooling. I am increasing the fan to 40% to begin clearing smoke and shifting toward convective heat transfer, while keeping heat at 100% for now to ensure we don't stall immediately after the crack. I will monitor for any RoR flick or rapid acceleration as we approach the floor."
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=0% fan=60% drop=False conf=0.95 — 'The roast has reached 92 seconds of development, which is well past the 60-second floor. The bean temperature is 185°C, approaching the target drop of 194.7°C. With the RoR at 8°C/min and heat at 100%, the roast is progressing steadily. To ensure a clean finish and avoid overshooting the ceiling, I am cutting heat to 0% and increasing fan to 60% to manage the final momentum and clear smoke. I am not dropping yet as we are still ~10°C from the target, but we are in the final window.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (180-200C). The current bean temperature is 112C, which is appropriate for this stage of preheating. No heat or fan is needed yet as we are preparing to charge.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 137.3C and a stable RoR of 12.3C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required during first crack.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack has just begun (3 seconds in). The roast is in the very early stages of development. The bean RoR is stable at 9 C/min. I need to maintain heat to ensure the roast progresses through the development phase while increasing fan speed to manage smoke and transition to convective heat transfer as the beans expand. I will keep heat high for now to maintain momentum but increase fan to 50% to prepare for the upcoming development phase. Drop is not yet appropriate as we are well below the floor.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.9 — 'The roast is at 190°C with 63 seconds of development time. The RoR is 11°C/min and stable. We are approaching the target drop of 195°C. Given the current development time and the proximity to the ceiling, I am reducing heat to manage the final approach and maintaining fan to ensure convective consistency. We are very close to the floor/target window, so I am preparing for the drop.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 176°C, which is within the recommended charge range (180°C-200°C). I am setting the heat and fan to 0 to maintain the current state while the roaster finishes stabilizing for the charge.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.7 C/min and bean temp at 138.7 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke evacuation needed during first crack.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'First crack occurred 12 seconds ago. The roast is currently in the very early stages of development. The bean RoR is stable at 10C/min, which is appropriate for this stage. I am maintaining full heat and moderate fan to ensure momentum is preserved through the early development phase, as we are well below the development floor and the target drop temperature.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=0% fan=70% drop=True conf=0.95 — 'The roast has reached 102 seconds of development, which is well past the minimum floor for development. The bean temperature is 188°C, approaching the ceiling of 196°C. With a RoR of 10.7°C/min, the roast is progressing steadily toward the target drop temperature. Given the bias toward dropping once the floor is met to avoid over-darkening, and the proximity to the ceiling, it is time to drop the roast.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 119.0°C, which is below the recommended charge range (180°C-200°C). I am maintaining heat and fan at 0 to allow the drum to continue soaking and reach the target charge temperature.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=70% drop=False conf=0.9 — 'The roast is stalling significantly at 140C with heat and fan at 100%. The bean RoR has dropped to 11.3 C/min and is flatlining. To regain momentum before first crack, I need to reduce the fan to increase conductive heat transfer and reduce the cooling effect of the airflow, while maintaining maximum heat.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack has just begun (2 seconds ago). The roast is currently at 181C with a RoR of 10C/min. We are well below the development floor (60s post-crack) and the target drop temperature. I am maintaining full heat to ensure momentum through the early development phase, but increasing fan to 50% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required during the development phase.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=40% fan=90% drop=False conf=0.9 — 'The roast is at 190.7°C with 62 seconds of development time. The RoR is stable at 9.7°C/min. We are approaching the target drop temperature of 195.3°C. Given the current momentum and the proximity to the target, I am cutting heat to 40% to manage the final approach and increasing fan to 90% to ensure convective heat transfer and smoke evacuation as we near the finish. We are very close to the development floor and target temperature, so I am preparing to drop.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early drying phase. The bean temperature drop (99C to 90C) is likely a probe artifact or a result of the initial charge, but the environment temperature is rising steadily. Maintain maximum heat to build momentum for the Maillard phase. Fan remains at 0% to retain heat and moisture during the drying stage.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is currently stalling at 137C with 100% heat and 0% fan. The RoR has dropped to 10.7C/min and is flattening. I need to maintain maximum heat to push through the end of the drying/early Maillard phase, but I will introduce a small amount of fan (20%) to improve airflow and heat distribution, preventing a stall while preparing for the upcoming Maillard phase.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack (179C). RoR is healthy at 10.3C/min. I am increasing fan to 30% to prepare for the exothermic reaction and ensure convective heat transfer, while maintaining 100% heat to carry momentum into the crack. This balances the need for energy with the need for airflow to manage smoke and prevent baking.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=100% fan=60% drop=False conf=0.95 — 'The roast is at 54 seconds post-first crack with a bean temperature of 189C. The development ratio is currently ~7.9%, approaching the 12.4% target. The RoR has stabilized at 10C/min. To reach the target drop temperature of 195.3C while ensuring sufficient development time (approaching the 60s floor), I am maintaining heat to keep the momentum steady but increasing fan to 60% to transition toward convective heat, ensuring a clean finish without flicking. I will be ready to drop as soon as the temperature hits the target or the 60-second floor is cleared.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature is dropping in the telemetry, likely due to the initial moisture release and thermal inertia. Heat is at maximum to drive the roast forward, and fan is low to maintain drum energy. Maintain current settings to build momentum toward the Maillard phase.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 13.3 C/min and a bean temperature of 139 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required as we approach first crack.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=50% fan=70% drop=False conf=0.9 — 'First crack occurred 6 seconds ago. We are in the very early stages of development (179C). The RoR is stable but needs to be managed to ensure a smooth, non-flicking finish. I am reducing heat further to prevent a flick and increasing fan to 70% to shift toward convective heat, ensuring even development as we approach the target drop window. We are nowhere near the floor yet, so should_drop is false.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=50% fan=90% drop=False conf=0.95 — 'The roast has reached 66 seconds of development with a stable RoR of 9 C/min. The development ratio is currently ~11%, which is below the target of 19.6%. I need to maintain heat to reach the development target while using the fan to manage the environment. Since the RoR is stable and not flicking, I will maintain current heat and fan settings to continue the development phase toward the target drop temperature.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is currently experiencing a significant drop in bean temperature (RoR -27 C/min) despite maximum heat input. This indicates a stall or a measurement anomaly likely caused by the bean probe not being fully covered or a thermal lag issue early in the roast. I am maintaining maximum heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 135°C. With heat at 100% and fan at 0%, we are building momentum for the Maillard phase. I am maintaining full heat to ensure we reach first crack with sufficient energy, but I will introduce a small amount of fan (20%) to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard/first crack window.'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — "First crack has just begun (3 seconds ago). The roast is in the very early stages of development. Heat is currently at 100% and fan at 20%. I need to maintain momentum to reach the development floor while preparing for the transition to convective cooling. I will keep heat high for now to ensure we don't stall immediately after crack, but I am increasing the fan to 40% to begin managing the environment and preparing for the convective phase. We are far from the drop floor (approx 60s post-crack)."
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=0% fan=90% drop=True conf=0.95 — 'The roast has reached 63 seconds of development with a steady RoR of 9.0 C/min. The bean temperature is 187 C, which is approaching the ceiling but still allows for a controlled finish. Given the development time is sufficient and the RoR is stable, I am initiating the drop to avoid the risk of over-darkening or hitting the flick guard.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 117°C, which is well below the target charge range of 180°C-200°C. I am setting heat to 100% to reach the charge temperature efficiently. Fan is kept at 0% to allow the drum to soak and reach thermal equilibrium.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is currently stalling at 137C with 100% heat. The RoR has dropped significantly (11C/min). I need to increase airflow to improve convective heat transfer and help push through the Maillard phase, while maintaining maximum heat to overcome the stall. If the RoR does not recover, the roast will bake.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just begun (3 seconds ago). The roast is in the very early stages of development. Heat is currently at 100% and fan at 10%; I need to reduce heat and increase airflow to manage the exothermic reaction of first crack and prevent a flick or over-development. I am nowhere near the floor (60s post-crack), so I will not drop yet. I am setting heat to 60% and fan to 40% to transition to convective heat and slow the RoR.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.9 — 'The roast is at 63 seconds post-first crack with a bean temperature of 189C. The RoR is 9.3 C/min and stable. We are approaching the target drop temperature of 196.3C. Given the current development time and the steady progress, I am reducing heat to 40% to manage the final approach and prevent a flick, while maintaining 80% fan for convective consistency. We are nearing the development floor; I will monitor for the drop signal as we approach 196C.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is currently experiencing a significant, erratic drop in bean temperature (RoR -17.7 C/min) despite maximum heat input. This suggests a sensor anomaly or a severe stall. Given the current bean temperature of 97C, the roast is far from first crack. I am maintaining maximum heat to attempt to recover momentum and keeping fan low to minimize convective heat loss while the bean temperature is this low.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily but slowly. At 135°C bean temperature and 376 seconds elapsed, the RoR (10.3°C/min) is quite low for this stage. To ensure we reach first crack with enough momentum to avoid a stalled roast, I am maintaining maximum heat. I am keeping the fan low (10%) to prioritize conductive heat transfer and build energy in the drum, as we are still well below the Maillard phase transition.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — "First crack has just begun (4 seconds ago). The roast is in the very early stages of development. With a bean RoR of 11.3 C/min and current temp of 178 C, I need to maintain momentum to reach the development floor while managing the heat to avoid a late-roast flick. I am keeping heat at 100% to ensure we don't stall immediately after crack, but I am increasing the fan to 40% to transition toward convective heat and prepare for the upcoming development phase."
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=0% fan=100% drop=False conf=0.95 — 'The roast is at 191°C with 64 seconds of development time. The RoR is stable at 13°C/min. We are approaching the target drop temperature of 196.3°C. Given the current trajectory and the proximity to the ceiling, I am cutting heat to 0% to coast into the drop, while maintaining maximum airflow to ensure clean convective heat transfer and prevent any flick. We are within the development window, so dropping as soon as we hit the target is appropriate.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current bean temperature is 99.0°C, which is well below the target charge range of 180°C-200°C. I will maintain zero heat and fan to allow the roaster to continue heating up toward the charge temperature.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 125.3°C and a healthy RoR of 14.3°C/min, we are well-positioned. I am maintaining full heat to ensure sufficient momentum for the upcoming Maillard phase, while keeping the fan at 0% to maximize conductive heat transfer and build internal bean energy before the transition to convective cooling later.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is approaching the transition to first crack. With a bean RoR of 12 C/min and a current temperature of 167.7 C, we are in the late Maillard phase. I am maintaining full heat to ensure sufficient momentum into first crack, while keeping the fan at 30% to maintain conductive heat transfer for body. I will look to increase the fan once first crack begins to manage smoke and transition to convective heat.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=40% fan=60% drop=False conf=0.95 — 'The roast has reached 85 seconds of development with a bean temperature of 185°C. The RoR is stable at 12°C/min. Given the target drop temperature of 197°C and the current trajectory, we are approaching the drop window. The recent heat increase to 90% is unnecessary and risks a flick; I am cutting heat to 40% to manage the momentum and increasing fan to 60% to shift toward convective heat and ensure clean development as we approach the final stretch. We are nearing the floor, so I will prepare to drop as soon as the RoR shows signs of slowing or we hit the target temperature.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 124.0°C and rising steadily. I am preparing to charge the roaster within the recommended range (180-200°C). No heat or fan is currently applied as the machine is warming up.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is currently stalling at 134C with 100% heat and 0% fan. The RoR has dropped to near zero. I need to increase airflow to improve heat distribution and convection, and maintain maximum heat to push through the stall and reach the Maillard phase.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.9 — 'The roast is only 19 seconds into development, well below the minimum development time. The RoR is healthy (11.7 C/min) and stable. I am reducing heat to 50% to manage the momentum and increasing fan to 60% to shift toward convective heat and clear smoke as we approach the development window. We are nowhere near the floor or ceiling yet.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=10% fan=60% drop=False conf=0.9 — 'Development time is 49s, which is approaching the minimum floor of 60s. Bean temperature is 189C, well below the 197C target and the 196C ceiling. RoR is stable at 12.7C/min. I am maintaining low heat (10%) to prevent a flick while allowing the roast to reach the development floor. No drop yet.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (-7.377 C/min RoR) despite 100% heat. This indicates a significant stall or measurement anomaly following the charge. I need to maintain maximum heat to recover momentum and prevent the roast from stalling, while keeping fan low to preserve conductive heat transfer.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 134C and RoR at 13.3C/min, we have sufficient momentum. I am initiating a modest increase in fan to 20% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany the upcoming Maillard phase, while maintaining full heat to ensure we don't stall before first crack."
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "First crack has just begun (6s ago). The RoR is currently 11 C/min, which is healthy for this stage. I am maintaining full heat and zero fan to ensure we don't stall the roast immediately after crack, but I am preparing to increase airflow as we progress to manage the convective heat transfer and clear smoke. We are well below the development floor and the ceiling, so we continue the roast."
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=0% fan=80% drop=False conf=0.95 — 'The roast has reached 66 seconds post-first crack with a bean temperature of 186°C. The RoR is stable at 10°C/min. Given the target drop temperature of 197°C and the current development time, we are approaching the floor. To manage the remaining development and avoid a flick, I am cutting heat to 0% and increasing fan to 80% to transition to convective cooling and prevent over-roasting as we approach the drop window. The roast is nearing the development target, so I am signaling to drop once the temperature hits the target or if the RoR stalls.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early drying phase. The bean temperature drop (101C to 91C) is likely a probe stabilization artifact following the charge, but the environment temperature is low. Maintaining maximum heat is necessary to build momentum for the Maillard phase. Fan remains at 0% to conserve energy and build drum heat.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With a bean RoR of 12.3 C/min and bean temp at 132.3 C, we have plenty of headroom before first crack. I am maintaining full heat to ensure sufficient momentum, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the moisture release that precedes first crack.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=50% fan=50% drop=False conf=0.9 — 'First crack has just begun (4 seconds ago). The roast is currently at 178C with 100% heat and 0% fan. To manage the development phase and prevent a flick or excessive heat buildup, I need to introduce convective airflow and reduce the heat input. Increasing the fan to 50% will help manage the smoke and transition to convective heat, while reducing heat to 50% will prevent the RoR from spiking as we approach the target drop temperature. We are well below the development floor, so we continue roasting.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=40% fan=70% drop=False conf=0.95 — 'The roast is at 64 seconds post-first crack with a bean temperature of 187°C. The RoR is stable at 9°C/min. We are approaching the target drop temperature of 197°C. To ensure proper development without risking a flick or overshooting the ceiling, I am reducing heat to manage the momentum and increasing fan to 70% to shift toward convective heat for a clean finish. We are nearing the development floor; I will monitor closely for the next 30-45 seconds before dropping.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the very early stages (86C), but the bean RoR has plummeted to -16C/min, indicating a severe stall or measurement error. Given the heat is already at 100%, I must maintain maximum heat to recover momentum and prevent the roast from stalling out completely. Fan is kept low to prioritize conductive heat transfer during this recovery phase.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 10.3 C/min and bean temp at 138 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 40% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required during first crack.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=90% fan=70% drop=False conf=0.9 — 'The roast is only 12 seconds into development, well below the minimum development floor. The RoR is healthy (9.7 C/min) and stable. I am maintaining high heat to ensure sufficient energy for development while increasing fan to 70% to transition toward convective heat transfer, which will help manage the roast as it approaches the target drop temperature. No drop yet.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.95 — 'The roast is at 193°C with 72 seconds of development time. The RoR is 10.3°C/min and stable. We are approaching the target drop temperature of 197.3°C. Given the current trajectory and the proximity to the ceiling, we are well within the development window. Maintaining full heat and fan is appropriate to finish the roast cleanly without stalling or flicking. I recommend dropping as soon as the target temperature is reached to avoid the ceiling.'

## prompt_version = v7

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-30.0s/-4.0°C; heat MAE=19.55 dir=0.81; fan MAE=13.64 dir=0.476; latency pre=1.31s preFC=1.16s FC=1.37s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    15 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-8.0s/-1.0°C; heat MAE=17.2 dir=0.833; fan MAE=14 dir=0.417; latency pre=1.17s preFC=1.28s FC=1.35s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.833 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=0.5 P=0.333 R=1.0 timing=-50.0s/-6.7°C; heat MAE=17.78 dir=0.808; fan MAE=13.15 dir=0.577; latency pre=1.2s preFC=1.19s FC=1.34s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=24     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    18 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.808 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.5 P=0.333 R=1.0 timing=-32.0s/-5.3°C; heat MAE=10 dir=0.85; fan MAE=9.52 dir=0.45; latency pre=1.21s preFC=1.23s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=18     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=0.667 P=0.5 R=1.0 timing=-54.0s/-8.7°C; heat MAE=17.96 dir=0.846; fan MAE=19.26 dir=0.269; latency pre=1.04s preFC=1.27s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=25     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.846 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.5 P=0.333 R=1.0 timing=-63.0s/-9.7°C; heat MAE=20 dir=0.783; fan MAE=15.62 dir=0.478; latency pre=1.17s preFC=1.25s FC=1.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=21     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/+0.0°C; heat MAE=11.9 dir=0.75; fan MAE=19.05 dir=0.35; latency pre=0.77s preFC=1.2s FC=1.28s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    12 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.75 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-18.0s/-2.3°C; heat MAE=13.33 dir=0.9; fan MAE=12.14 dir=0.5; latency pre=1.0s preFC=1.17s FC=1.31s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.9 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-5.0s/-2.0°C; heat MAE=8.33 dir=0.85; fan MAE=14.76 dir=0.35; latency pre=1.33s preFC=1.24s FC=1.4s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=0.667 P=0.5 R=1.0 timing=-20.0s/-4.0°C; heat MAE=20 dir=0.818; fan MAE=15.65 dir=0.409; latency pre=0.95s preFC=1.24s FC=1.41s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=21     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.818 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-10.0s/-1.7°C; heat MAE=15.91 dir=0.762; fan MAE=11.14 dir=0.429; latency pre=1.25s preFC=1.27s FC=1.31s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.762 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-14.0s/-2.0°C; heat MAE=20.4 dir=0.708; fan MAE=15.4 dir=0.458; latency pre=1.15s preFC=1.25s FC=1.28s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    14 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.708 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=14.55 dir=0.857; fan MAE=20.45 dir=0.286; latency pre=1.02s preFC=1.28s FC=1.36s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.5 P=0.333 R=1.0 timing=-37.0s/-6.3°C; heat MAE=12.5 dir=0.81; fan MAE=15.91 dir=0.429; latency pre=1.36s preFC=1.18s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=19     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.81 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=21.67 dir=0.783; fan MAE=18.12 dir=0.304; latency pre=1.42s preFC=1.18s FC=1.41s
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
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-29.0s/-5.0°C; heat MAE=14.4 dir=0.917; fan MAE=10.2 dir=0.417; latency pre=1.19s preFC=1.19s FC=1.35s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    18 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-16.0s/-3.0°C; heat MAE=11.9 dir=0.85; fan MAE=13.1 dir=0.4; latency pre=1.43s preFC=1.21s FC=1.28s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.5 P=0.333 R=1.0 timing=-66.0s/-9.3°C; heat MAE=16.15 dir=0.84; fan MAE=11.15 dir=0.88; latency pre=1.12s preFC=1.25s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=23     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.5 P=0.333 R=1.0 timing=-35.0s/-6.3°C; heat MAE=22.69 dir=0.76; fan MAE=17.88 dir=0.36; latency pre=1.02s preFC=1.33s FC=1.25s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=23     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     6 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.76 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.4 P=0.25 R=1.0 timing=-61.0s/-8.0°C; heat MAE=16.25 dir=0.87; fan MAE=8.75 dir=0.522; latency pre=1.42s preFC=1.18s FC=1.33s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=3      |  TN=20     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    16 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-23.0s/-4.0°C; heat MAE=12.12 dir=0.8; fan MAE=15 dir=0.44; latency pre=1.03s preFC=1.26s FC=1.3s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     4 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-13.0s/-2.3°C; heat MAE=11.92 dir=0.88; fan MAE=11.54 dir=0.48; latency pre=1.46s preFC=1.22s FC=1.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=0.667 P=0.5 R=1.0 timing=-25.0s/-5.3°C; heat MAE=18.39 dir=0.833; fan MAE=9.03 dir=0.467; latency pre=1.05s preFC=1.19s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=29     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    22 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.833 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=0.5 P=0.333 R=1.0 timing=-77.0s/-12.0°C; heat MAE=15.42 dir=0.826; fan MAE=12.92 dir=0.522; latency pre=1.08s preFC=1.31s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=21     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-25.0s/-2.0°C; heat MAE=6.43 dir=0.889; fan MAE=12.14 dir=0.444; latency pre=1.34s preFC=1.23s FC=1.14s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.889 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.4 P=0.25 R=1.0 timing=-97.0s/-15.3°C; heat MAE=30.54 dir=0.667; fan MAE=15.36 dir=0.481; latency pre=1.02s preFC=1.35s FC=1.25s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=3      |  TN=24     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     6 |    16 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.667 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-27.0s/-5.0°C; heat MAE=13.75 dir=0.783; fan MAE=21.88 dir=0.261; latency pre=1.26s preFC=1.35s FC=1.69s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.4 P=0.25 R=1.0 timing=-88.0s/-14.6°C; heat MAE=18.52 dir=0.808; fan MAE=9.44 dir=0.538; latency pre=1.28s preFC=1.23s FC=1.37s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=3      |  TN=23     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    18 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.808 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and is ready for charging. The current environment temperature (160C) is below the recommended charge range (180C-200C). I am recommending maximum heat to reach the target charge temperature quickly. Fan remains off to allow the drum to soak heat.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 131C. With an RoR of 14.6C/min, we are in a good position to approach the Maillard phase. I am initiating a moderate fan increase to 25% to begin transitioning toward convective heat transfer and preparing for the upcoming moisture release, while maintaining full heat to sustain momentum toward first crack.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack was detected 7 seconds ago (real-time lag suggests it started ~15-20s ago). The bean temperature is 173C, approaching the 178C FC signature. RoR is healthy at 11C/min. To manage the development phase and prevent a spike, I am cutting heat significantly to account for thermal lag and increasing fan to shift toward convective heat, ensuring a controlled, even finish toward the 189C drop target.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=40% fan=80% drop=False conf=0.95 — 'Bean temperature is 181C and rising steadily with an RoR of 8C/min. We are 67 seconds into development post-FC. Given the detection lag, the beans are well into development. To reach the 189C target without overshooting or stalling, I am cutting heat to 40% to manage the momentum and increasing fan to 80% to shift toward convective cooling and ensure clean development as we approach the drop. We are close enough to the target that we should be ready to drop within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 121°C, which is well below the recommended charge range (180°C-200°C). I am setting heat to 100% to continue preheating toward the target charge temperature. Fan remains at 0% to allow the drum to build thermal mass efficiently.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 135C and RoR at 12C/min, we are approaching the transition to first crack. It is time to introduce airflow to shift toward convective heat transfer and prepare for the upcoming crack. I am maintaining full heat for now to ensure momentum, but increasing fan to 30% to manage the environment and prepare for the exothermic phase.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=70% fan=50% drop=False conf=0.9 — 'Bean temperature is at 173C, approaching the 178C first crack signature. RoR is healthy at 10.7C/min. I am reducing heat to 70% to manage the momentum into first crack and increasing fan to 50% to shift toward convective heat and prepare for smoke evacuation. This coordinated adjustment prevents a spike in RoR as we enter the critical development phase.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=40% fan=85% drop=False conf=0.95 — 'The roast is at 185C with a steady RoR of 7.3C/min. Given the 86s development time and the 185C bean temp, we are well into development. With a target drop of 190C, we are only 5C away. To ensure a clean finish without stalling, I am cutting heat to 40% to manage the momentum and increasing fan to 85% to shift to convective cooling and clear smoke. We are very close to the drop point; I will prepare to drop as soon as we hit 190C.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has plummeted to -14.3 C/min, indicating a severe stall or measurement error, despite the heat being at 100%. The bean temperature is dropping rapidly. I need to maintain maximum heat to recover the momentum and keep the fan at 0% to minimize convective heat loss while the bean temperature is this low. The roast is in a critical state of stalling.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 131C. With 100% heat and 0% fan, the RoR is healthy (12C/min). To prepare for the Maillard phase and ensure even heat distribution before first crack, I am introducing moderate airflow (25%) while maintaining full heat to keep momentum. This shifts the heat transfer toward convection, which is safer as we approach the higher temperature ranges.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack was detected 17 seconds ago. The bean temperature (174.7C) is approaching the typical FC signature (178C). With a RoR of 10C/min, the roast is progressing well. I am reducing heat to 60% and increasing fan to 60% to manage the transition into the final development stage, ensuring convective heat takes over to prevent scorching while maintaining momentum toward the 190C drop target.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=0% fan=90% drop=True conf=0.95 — 'The bean temperature is at 183.3°C, well past the 178°C first crack signature, and the RoR is a healthy 8.6°C/min. With 77 seconds of development time already elapsed and the bean temperature approaching the 190°C target, the roast is nearing completion. Given the detection lag, the beans are further developed than the clock suggests. I am dropping now to avoid the bitter/ashy ceiling.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C). The heat and fan are currently off. I am ready to charge the beans.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With bean temperature at 140.7C and a healthy RoR, I am maintaining full heat to ensure sufficient momentum through the Maillard phase. I am increasing the fan slightly to 20% to begin preparing for better airflow management as we approach first crack, balancing convective heat transfer without stalling the RoR.'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'Bean temperature is at 176C, right at the threshold of first crack. RoR is healthy at 11C/min. I am reducing heat to 70% to prepare for the exothermic reaction of first crack and increasing fan to 30% to improve convective heat transfer and prepare for smoke evacuation. This coordinated move prevents a spike in RoR during crack.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=0% fan=60% drop=True conf=0.95 — 'Bean temperature is 186C, well past the 178C FC signature, and RoR is 10C/min. With 52s of development time (plus detection lag), the roast is sufficiently developed. The target drop temp is 191.3C, but given the momentum and the risk of hitting the 196C ceiling, I am dropping now to preserve flavor and avoid bitterness.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 156C, which is below the recommended charge range (180C-200C). I am setting heat to 100% to accelerate the preheat toward the target charge temperature. Fan remains off to conserve heat during the preheat phase.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 134.7C and a healthy RoR of 11.7C/min, we are well-positioned for the approach to first crack. I am maintaining full heat to ensure sufficient momentum, but I am initiating a small increase in fan (to 20%) to begin transitioning toward convective heat transfer and preparing for the smoke/chaff management required as we approach the crack.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack was detected 15 seconds ago. The bean temperature (173.7 C) is approaching the typical FC signature (178 C). With a RoR of 9.7 C/min, the roast is progressing well. I am reducing heat to 60% and increasing fan to 60% to manage the momentum and transition to convective heat transfer as we approach the target drop temperature of 191.7 C. We are not yet at the drop point.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=0% fan=80% drop=True conf=0.95 — 'The bean temperature is 183.0°C, well past the 178°C first crack signature. With a RoR of 6.0°C/min and 95 seconds of development time, the roast is progressing steadily toward the 191.7°C target. Given the thermal lag and the fact that development is already well underway, I am cutting heat to 0% and increasing fan to 80% to manage the momentum and ensure a clean finish without hitting the ashy ceiling. The roast is very close to the target drop temperature; I am recommending a drop now to avoid over-development.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature is currently stable/recovering after the initial charge. Maintaining maximum heat (100%) is appropriate to build momentum toward the Maillard phase. Fan remains at 0% to maximize conductive heat transfer during this early stage.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 134C. With 100% heat and 0% fan, the environment temperature is stable, but we need to prepare for the Maillard phase and the eventual approach to first crack. I am increasing the fan to 25% to introduce convective heat and begin clearing moisture/chaff, while maintaining 100% heat to keep the momentum toward the 178C FC threshold.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been detected (10s ago). The bean temperature (175.7C) is approaching the typical FC signature (178C). With a RoR of 10C/min, the roast is moving well. I am cutting heat significantly to manage the momentum and increasing fan to 60% to shift toward convective heat and clear smoke as we enter the development phase. We are still well below the drop temperature and the bitter ceiling.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=20% fan=80% drop=True conf=0.9 — 'The bean temperature is 183C, well past the 178C FC signature, and the RoR is slowing (7.3 C/min). With 70 seconds of development time already elapsed and the bean temperature approaching the 192.7C target, the roast is nearing completion. To avoid over-development and bitterness, I am cutting heat significantly and increasing fan to convective mode to finish the roast cleanly. Given the proximity to the target drop temperature and the inherent lag, I am recommending a drop.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (188C). It is ready to be charged.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With bean temperature at 145.7C and a healthy RoR, I am maintaining full heat to ensure sufficient energy for the upcoming endothermic phase. I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard phase.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been confirmed (178C). The RoR is healthy at 10C/min. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and increasing fan to shift toward convective heat transfer. This will ensure a controlled, even development toward the 193C target without risking a stall or an ashy finish.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=30% fan=80% drop=False conf=0.95 — 'The bean temperature is at 187.7C, approaching the target drop of 193C. With a RoR of 9.7C/min and 65 seconds of development time, the roast is progressing well. Given the detection lag of first crack, we are likely further into development than the clock suggests. To ensure a smooth finish without hitting the ashy ceiling, I am reducing heat to 30% to manage the momentum and increasing fan to 80% to shift toward convective cooling and clear smoke as we approach the drop. We are very close to the target drop temperature.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature is dropping rapidly in the recent samples (139C to 125C), which suggests a potential sensor fluctuation or a significant stall. I am maintaining maximum heat to recover momentum and keeping the fan at 0% to ensure maximum conductive heat transfer during this early stage.'
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The bean RoR has flattened significantly (12 C/min and stalling at 143 C). With the heat at 100% and fan at 20%, the roast is losing momentum before reaching the Maillard/FC transition. I am increasing fan to 40% to improve convective efficiency and ensure the bean mass is absorbing the heat effectively, while maintaining 100% heat to push through this plateau toward first crack.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=40% fan=70% drop=False conf=0.9 — 'First crack was detected 28 seconds ago. The bean temperature is 180C, which is past the typical FC signature. With a RoR of 10C/min, we are approaching the target drop temperature of 193C quickly. To manage the development and avoid the bitter ceiling, I am cutting heat significantly and increasing airflow to transition to convective cooling, preparing for a drop in the next 60-90 seconds.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=20% fan=90% drop=False conf=0.95 — 'Bean temperature is at 185.3C, well past the 178C FC signature, and RoR is a healthy 10.6C/min. With 48 seconds of development time already elapsed and the target drop of 193C approaching, the roast is progressing well. I am cutting heat to 20% to manage the momentum and increasing fan to 90% to maximize convective airflow for a clean finish. Given the proximity to the target temperature and the inherent detection lag, we are very close to the drop point.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.95 — 'The roaster is currently in the preheating phase with a bean temperature of 183°C, which is within the recommended charge range (180-200°C). The system is ready for charging. I am setting heat and fan to 0 as per the current state, awaiting the user to initiate the charge.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily toward first crack (approx 178C). With a bean RoR of 11.7 C/min and 100% heat, we are building momentum. I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke that will accompany the upcoming crack, while maintaining high heat to ensure we don't stall before reaching the target development range."
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — "Bean temperature is at 176C, right at the threshold for first crack. RoR is still healthy at 10.3C/min. I need to prepare for the upcoming crack by increasing airflow to improve convective heat transfer and manage smoke, while keeping heat high enough to maintain momentum through the crack. I will increase fan to 50% and maintain heat at 100% for now to ensure we don't stall before the crack begins."
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=40% fan=70% drop=False conf=0.95 — 'The bean temperature is at 186°C, well into the development phase post-first crack. With a RoR of 10°C/min and a target drop of 193°C, we are approaching the finish line. Given the detection lag of first crack, the beans are further developed than the 52-second timer suggests. I am cutting heat to 40% to manage the momentum and increasing fan to 70% to shift toward convective cooling and ensure a clean finish without stalling. I will be ready to drop as soon as the temperature hits the target range.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase. The bean temperature is dropping rapidly (122C to 110C in 5 seconds), which suggests a significant measurement anomaly or a severe stall. Given the heat is at 100% and fan is at 0%, I need to maintain maximum heat to recover momentum and drive the roast toward Maillard. I will keep heat at 100% and fan at 0% to ensure the drum retains all available energy for the recovery.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 129.3C and a healthy RoR of 13.77C/min, we are approaching the transition to mid-Maillard. I am maintaining full heat to ensure sufficient momentum, but initiating a modest fan increase (20%) to begin clearing moisture and preparing for the convective heat transfer needed as we approach first crack.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=100% fan=60% drop=False conf=0.9 — 'First crack was detected 14 seconds ago, but the bean temperature (172C) is still below the typical 178C signature for this roaster, suggesting the crack is just beginning. RoR is healthy at 10.3 C/min. I am maintaining high heat to ensure momentum through the early development phase while increasing fan to 60% to transition toward convective heat and prepare for the smoke/chaff increase that follows the crack. I will monitor for the 178C mark to gauge true development progress.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=0% fan=60% drop=False conf=0.95 — 'The bean temperature is at 185C, which is well into the development phase post-first crack. With a RoR of 9C/min and the target drop temperature of 193C, we are approaching the finish line. Given the thermal lag of the Hottop and the fact that we are already 104 seconds into development, I am cutting heat to 0% to coast to the target and increasing fan to 60% to ensure convective cooling and smoke evacuation as we approach the drop. We are close enough to the target that we should be ready to drop within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (168C to 151C in 5 seconds), which is highly anomalous and suggests a sensor error or a massive environmental shift. Given the heat is at 100% and fan is at 0%, the roaster should be gaining heat. I am maintaining maximum heat to stabilize the roast and keeping the fan off to prevent further heat loss until the temperature reading stabilizes and begins to climb again.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily at 142C. With a RoR of 12.3C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure we don't stall before the crack, while slightly increasing fan to 20% to begin preparing for convective heat transfer and better smoke management as we approach the Maillard phase."
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=40% fan=90% drop=False conf=0.9 — 'Bean temperature is 182.7C, well past the 178C FC signature. RoR is 11C/min, which is healthy but requires careful management to avoid overshooting the 193.7C target. I am reducing heat to 40% to manage momentum and increasing fan to 90% to shift to convective cooling and ensure clean development as we approach the drop. We are nearing the target temperature, so I am preparing for a drop shortly.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=40% fan=80% drop=False conf=0.95 — 'The bean temperature is at 187.3C, well past the 178C FC signature, and the RoR is still a healthy 10.3C/min. With 43 seconds of development time already elapsed and the target drop temp of 193.7C approaching, the roast is progressing rapidly. I am cutting heat to 40% to manage the momentum and increasing fan to 80% to shift to convective cooling and ensure clean development as we approach the drop. We are very close to the target; I expect to drop within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=0% fan=50% drop=False conf=0.9 — "The bean temperature is at 180C, which is the typical signature for first crack on this roaster. Even though first crack hasn't been explicitly detected yet, the bean temperature and the recent RoR behavior suggest we are entering the development phase. Heat is already off (0%), and fan is off. I need to engage the fan to 50% to manage the convective heat transfer and clear smoke as we progress toward the drop temperature of 194C. No heat is needed as the momentum is sufficient."
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.3 C/min and bean temperature at 141.3 C, we are approaching the first crack window (typically ~178 C). I am maintaining full heat to ensure sufficient momentum, but increasing the fan slightly to 20% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack was detected 6 seconds ago. The bean RoR is still quite high (11 C/min) and heat is at 100%. To prevent a runaway roast and ensure controlled development, I am cutting heat significantly and increasing airflow to shift toward convective heat transfer. This will manage the momentum as we approach the 194 C drop target.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=20% fan=70% drop=False conf=0.95 — 'The bean temperature is at 187C, well past the 178C FC signature, and the RoR is 10C/min. With 36 seconds of development elapsed and the target drop of 194C approaching, we are in the final stretch. To ensure a clean finish without hitting the 196C ashy ceiling, I am cutting heat to 20% to manage the momentum and increasing fan to 70% to shift toward convective cooling and clear smoke. We are very close to the drop point.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (143C to 127C in 5 seconds), which is highly anomalous and suggests a sensor error or a massive heat loss event. Given the heat is at 100% and the environment temperature is also dropping, I need to maintain maximum heat to recover the momentum. I will keep heat at 100% and fan at 0% to minimize convective loss while the drum recovers.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 140C. With a bean RoR of 12.7C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure we don't stall, but I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke that will accompany first crack. This will help ensure a cleaner roast as we approach the Maillard phase."
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just been detected. The bean temperature is at 177C, right at the threshold for FC. With heat at 100% and fan at 0%, the roast is at risk of a runaway RoR. I am cutting heat significantly and increasing fan to 50% to transition to convective heat transfer, manage the exothermic reaction, and prevent a spike in bean temperature as we approach the 194C drop target.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=40% fan=70% drop=False conf=0.95 — 'The bean temperature is at 186°C, which is well into the development phase post-first crack (detected at 496s). With a target drop of 194°C and a current RoR of 8.7°C/min, the roast is progressing steadily. Given the detection lag and the proximity to the 194°C target, I am reducing heat to 40% to manage the momentum and increasing fan to 70% to ensure convective airflow for the final stage. I anticipate reaching the drop temperature within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roaster is in the preheating phase, currently at 174C. The charge guidance is 180-200C. I am setting heat to 0 and fan to 0 to allow the drum to stabilize at the target charge temperature before the roast begins.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — "The roast is progressing steadily toward first crack. With bean temperature at 141C and RoR at 11.7C/min, we are in the late Maillard phase. I am increasing the fan to 30% to begin transitioning toward convective heat transfer and preparing for the smoke/chaff management required during first crack, while maintaining full heat to ensure we don't stall before reaching the crack."
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.9 — 'First crack was detected 8 seconds ago (real-time lag implies it started ~20s ago). Bean temperature is at 179C, right at the FC signature. RoR is 10C/min, which is healthy but needs to be managed to avoid overshooting the 194.3C target too quickly. I am cutting heat to 60% and increasing fan to 50% to transition to convective heat and prevent a spike, while keeping the roast moving toward the drop target.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature is at 188°C, which is well into the development phase post-first crack (detected at 494s). With a current RoR of 9°C/min and a target drop of 194.3°C, the roast is approaching the finish line. Given the thermal lag and the fact that we are already 69 seconds into development, the roast is sufficiently developed. I am recommending a drop now to avoid the risk of over-roasting or hitting the bitter ceiling.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=70% fan=20% drop=False conf=0.9 — 'The roast is in the early drying/yellowing phase with heat and fan currently at 0%. To initiate the roast and build momentum toward the Maillard phase, I am applying moderate heat and low fan to begin establishing convective airflow while managing the thermal lag of the element.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 130C. With an RoR of 14C/min, we are in a good position to approach the Maillard phase. I am maintaining full heat to ensure sufficient momentum, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release, which will help prevent stalling as we approach first crack.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just been detected. The bean temperature (173C) is approaching the typical FC signature (178C). With a high RoR (11.3 C/min) and heat at 100%, I need to proactively manage the energy to avoid a runaway roast. I am reducing heat to 70% and increasing fan to 50% to shift toward convective heat and prepare for a controlled finish as we approach the 194.7C drop target.'
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=0% fan=60% drop=False conf=0.95 — 'The roast is at 185C with a RoR of 8C/min, approaching the target drop of 194.7C. Given the 92s development time and the detection lag, the beans are well into development. To avoid the ashy ceiling and ensure a clean finish, I am cutting heat to 0% and increasing fan to 60% to transition to convective cooling and manage the final momentum. The roast is very close to the target; I will drop as soon as the temperature approaches the target range to avoid over-development.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 112.0°C and rising, but the heat is currently off. To prepare for charging, I am setting the heat to 100% to ensure the drum reaches the target charge temperature (180-200°C) efficiently. Fan remains off during preheat to maintain thermal stability.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 137.3C and a healthy RoR of 12.3C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing fan to 25% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required as we approach first crack.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been detected (3s elapsed). The bean temperature is 179C, which is right at the threshold for FC on this roaster. With 100% heat, the RoR is still 9C/min, which is too aggressive for the development phase. I am cutting heat significantly and increasing fan to 60% to shift to convective heat and manage the momentum, preventing a spike toward the 196C ceiling. Should_drop is false as we are only at the very beginning of development.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=0% fan=80% drop=True conf=0.95 — 'The bean temperature is at 190C, approaching the 195C target. Given the 11C/min RoR and the detection lag of first crack, the roast is well-developed. To avoid the 196C ashy ceiling, I am cutting heat to 0% to coast to the drop temperature while maintaining 80% fan to ensure convective cooling and smoke evacuation. Drop is imminent.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=0.9 — 'The roaster is currently in the preheating phase, but the bean temperature is already at 176°C, which is very close to the typical first crack threshold for this machine. Since the roast has not been charged yet (or the sensor is reading ambient/residual heat), I am setting the heat to 0 and fan to 0 to ensure the machine is ready for the charge. The user needs to charge the beans immediately to begin the roast cycle.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — "The roast is progressing steadily through the Maillard phase. With bean temperature at 138.7C and a healthy RoR of 12.7C/min, we are approaching the transition to first crack. I am increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required during first crack, while maintaining full heat to ensure we don't stall before the crack begins."
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack was detected 12 seconds ago. The bean temperature (172C) is just entering the FC range. RoR is currently 10C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to moderate the momentum and increasing fan to 60% to shift toward convective heat and clear smoke as development progresses. I am not dropping yet as the bean temperature is still below the typical development range.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=20% fan=90% drop=False conf=0.95 — 'The bean temperature is at 188°C and climbing with a RoR of 10.7°C/min. With a target drop of 195°C, we are very close to the finish. Given the detection lag of first crack, the roast is well-developed. To avoid hitting the 196°C ashy ceiling, I am cutting heat significantly to coast to the drop temperature while increasing fan to ensure convective airflow for a clean finish. Drop is imminent.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roaster is in the preheating phase. The current bean temperature of 119.0°C is appropriate for charging soon. I am setting heat to 100% to ensure the drum is fully saturated and ready for the charge, while keeping the fan off to maintain thermal stability during the final moments of preheating.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'The bean RoR has dropped significantly (11.3 C/min) and the bean temperature is stalling at 140 C despite maximum heat and fan. The roast is losing momentum before reaching first crack. I am maintaining maximum heat to push through the Maillard phase and keep the RoR from crashing further.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been detected (2s elapsed). The bean RoR is 10C/min, which is healthy but requires careful management to avoid overshooting the 195.3C target. I am cutting heat significantly and increasing fan to 60% to shift toward convective heat transfer, ensuring a controlled, steady climb to the drop temperature without risking a spike or stalling. Should_drop is false as we are only at the very beginning of development.'
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=40% fan=90% drop=False conf=0.95 — 'The bean temperature is at 190.7C and climbing toward the 195.3C drop target. With a RoR of 9.7C/min and 62 seconds of development already elapsed, the roast is well into the development phase. Given the thermal lag of the Hottop and the proximity to the 196C bitter/ashy ceiling, I am cutting heat to 40% to coast into the drop and increasing fan to 90% to ensure convective cooling and smoke evacuation as we approach the finish. Drop is imminent.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the early drying phase. The bean temperature drop in the recent samples is likely a probe stabilization artifact following the charge. Heat remains at 100% to build momentum for the Maillard phase, and fan is kept at 0% to retain energy during the initial moisture release.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The bean RoR has flattened significantly (stalled at 137C) despite 100% heat. The environment temperature is also plateauing. I need to increase airflow to improve convective heat transfer and pull more energy into the beans to push through this stall before first crack. I am increasing fan to 30% while maintaining max heat to drive the roast forward.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'Bean temperature is at 179C, indicating first crack is imminent or just beginning. With 100% heat and 0% fan, the roast is at risk of momentum-driven over-development. I am cutting heat significantly and increasing fan to 50% to transition to convective heat transfer, manage the RoR, and prepare for the drop as the bean temperature approaches the 195.3C target.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=0% fan=80% drop=True conf=0.95 — 'Bean temperature is at 189C and rising toward the 195.3C target. With 54 seconds of development time already elapsed and a steady RoR, we are approaching the finish line. To avoid overshooting the 196C ceiling due to thermal lag, I am cutting heat to 0% and increasing fan to 80% to manage the final momentum and clear smoke. The roast is sufficiently developed; drop as soon as the target temperature is reached.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the early drying phase. The bean temperature is dropping in the recent samples, likely due to the initial moisture release and thermal inertia. Maintaining maximum heat is appropriate at this stage to build momentum. Fan is kept low to retain heat and moisture for the initial phase.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With bean temperature at 139C and a healthy RoR of 13.3C/min, we are in the late drying/early Maillard phase. I am increasing the fan to 30% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management needed as we approach first crack, while maintaining full heat to keep the momentum steady.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=50% fan=70% drop=False conf=0.9 — 'First crack was detected 6 seconds ago (real-time lag implies it started ~15-20s ago). The bean temperature is at 179C, right at the start of the development phase. RoR is currently 9.8C/min, which is healthy but needs to be managed to avoid hitting the 196C ceiling too quickly. I am reducing heat to 50% to moderate the momentum and increasing fan to 70% to shift toward convective heat and ensure clean development. We are nowhere near the drop temperature yet, so we continue the roast.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature is at 188°C, which is well into the development phase post-first crack (detected at 522s). With a RoR of 9°C/min and the target drop temperature of 196°C, we are approaching the ceiling. Given the thermal lag and the fact that we are already 66 seconds into development, the roast is progressing quickly. I am cutting heat to 0% to coast to the drop temperature and increasing fan to 100% to maximize convective cooling and ensure a clean finish. Drop is imminent.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR is negative and dropping rapidly, indicating a stall or significant measurement error following the charge. With heat at 100% and fan at 0%, the roaster is at maximum energy input. I am maintaining maximum heat to recover momentum and keeping the fan off to ensure maximum conductive heat transfer during this recovery phase.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 135C. With RoR at 13C/min and no first crack yet, we need to maintain momentum while preparing for the transition to convective heat. I am increasing the fan to 25% to begin clearing moisture and preparing for the upcoming Maillard phase, while keeping heat at 100% to ensure we don't stall before reaching the first crack zone."
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been detected. The RoR is healthy at 10C/min. To manage the development phase and prevent an uncontrolled spike, I am cutting heat significantly and increasing airflow to shift toward convective heat transfer, ensuring a steady, controlled development toward the target drop temperature.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=40% fan=90% drop=False conf=0.95 — 'The bean temperature is at 187°C, which is well into the development phase post-first crack (detected at 589s). With a RoR of 9°C/min and the target drop temperature of 196°C, we are approaching the ceiling. Given the thermal lag and the fact that development is further along than the clock suggests, I am reducing heat to 40% to manage the final approach and maintaining high airflow (90%) to ensure convective heat and smoke evacuation. We are within 9°C of the target; dropping soon is appropriate.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 117°C, which is well below the target charge range of 180°C-200°C. I am setting heat to 100% to accelerate the preheat to the target charge temperature. Fan is kept at 0% to allow the drum to reach thermal equilibrium efficiently.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The bean RoR has flattened significantly at 137C, indicating a stall or a very slow approach to the Maillard phase. With heat at 100% and fan at 10%, the roaster is not effectively moving energy into the beans. I am increasing fan to 30% to improve convective heat transfer and keeping heat at 100% to push through this plateau and regain momentum before first crack.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been confirmed. The RoR is healthy (10.7 C/min) and the bean temperature (179.7 C) is just entering the development phase. To manage the momentum and ensure a controlled development, I am cutting heat significantly to prevent a spike and increasing fan to shift toward convective heat transfer, which will help maintain a steady, even development without stalling the roast.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.95 — 'The bean temperature is at 189C and climbing toward the 196.3C ceiling. With a RoR of 9.3C/min and 63 seconds of development, the roast is progressing well. To avoid hitting the 196C ashy ceiling too quickly or stalling, I am reducing heat to 40% to gently glide into the drop temperature while maintaining the 80% fan for convective consistency. Given the proximity to the target drop temperature and the thermal lag, I am preparing to drop shortly.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly (negative) despite maximum heat. This indicates a stall or a measurement error, but given the current bean temperature of 97C, the roast is in danger of stalling completely. I am maintaining maximum heat to recover momentum and keeping fan low to minimize convective cooling while the bean temperature is this low.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily through the Maillard phase. With a bean RoR of 10.3 C/min and bean temp at 135 C, we have sufficient momentum to approach first crack. I am maintaining full heat to ensure we don't stall, while slightly increasing the fan to 20% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany first crack."
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just been detected (4 seconds ago). The bean temperature is at 178C, which is the typical FC signature for this roaster. The RoR is still healthy at 11.3 C/min. To manage the development phase effectively and avoid a runaway finish, I am cutting heat significantly to anticipate thermal lag and increasing fan speed to shift toward convective heat transfer, ensuring even development without scorching as we approach the target drop temperature.'
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature is at 191.0°C and climbing toward the 196.3°C ceiling. With a RoR of 13°C/min and 64 seconds of development time already elapsed (plus the detection lag), the roast is well into the target development range. To avoid the ashy/bitter ceiling at 196°C, I am cutting heat to 0% to coast to the drop point, while maintaining maximum airflow to ensure clean development. Drop is imminent.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 99°C, which is well below the recommended charge range of 180°C-200°C. I will maintain zero heat and fan to allow the drum to continue soaking and reach the target charge temperature.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early stages (125C). Heat is at 100% with no fan, which is appropriate for this phase to build momentum. I will maintain full heat to continue the climb toward the Maillard phase, keeping the fan off to maximize conductive heat transfer until we approach the first crack zone.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack (typically ~178°C). RoR is healthy at 12°C/min. Maintaining full heat and moderate fan to ensure sufficient momentum through the crack, but preparing to increase airflow shortly to manage the exothermic phase. No drop yet.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=0% fan=70% drop=True conf=0.9 — 'The bean temperature is at 185C, well past the 178C FC signature. With 85 seconds of development time already elapsed and a RoR of 12C/min, the roast is progressing quickly. Given the hard ceiling of 196C and the thermal lag of the heating element, I am cutting heat to 0% and increasing fan to 70% to manage the momentum and prevent over-roasting. I am recommending a drop now as the roast is sufficiently developed and approaching the bitter/ashy threshold.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 124.0°C, which is below the charge guidance range (180°C-200°C). I am maintaining zero heat and zero fan to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — "The bean RoR has flattened significantly at 134C, indicating a stall or a very slow approach to the Maillard phase. With heat at 100% and fan at 0%, the roaster is at maximum energy input. I am increasing fan to 20% to improve convective heat transfer and help push through this plateau, while maintaining 100% heat to ensure we don't lose momentum before first crack."
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.9 — 'The roast is well into development (19s post-FC, bean temp 182C). RoR is 11.7C/min, which is healthy but needs to be managed to avoid overshooting the 197C target too quickly. I am reducing heat to 50% to moderate the momentum and increasing fan to 60% to shift toward convective heat and ensure clean development. Given the 197C target and the 196C hard ceiling, I will monitor closely; we are approaching the drop window.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=0% fan=60% drop=False conf=0.95 — 'The bean temperature is at 189°C, which is well into the development phase post-first crack. With a RoR of 12.7°C/min and the target drop temperature of 197°C, the roast is approaching the finish line quickly. Given the thermal lag of the Hottop and the proximity to the 196°C bitter/ashy ceiling, I am cutting heat to 0% to coast to the drop temperature and maintaining fan at 60% to ensure convective airflow without stalling the roast. I will drop as soon as the bean temperature hits 197°C.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-7.377 C/min) while the environment temperature is stable. This indicates a stall or a measurement anomaly, but given the current bean temperature of 106C, the roast needs more energy to progress through the drying phase. I am maintaining maximum heat and keeping the fan low to ensure we recover momentum and move toward the Maillard phase.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 134C. With RoR at 13.3C/min, we are in a good position to approach the Maillard phase. I am increasing the fan to 25% to begin introducing convective heat and preparing for the smoke/chaff management required as we approach first crack, while maintaining full heat to keep the momentum steady.'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just been detected (6s ago). The RoR is still healthy at 11C/min. To manage the development phase and prevent a runaway finish, I am cutting heat to 60% and increasing fan to 40% to transition to convective heat and clear smoke. The bean temperature is approaching the 178C FC signature; I will monitor closely to drop near 192-194C, well before the 196C ashy ceiling.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=0% fan=80% drop=False conf=0.95 — 'The bean temperature is 186C, well past the 178C FC signature, and the RoR is 10C/min. Given the 66s development time and the thermal lag, the roast is nearing the finish. To avoid the 196C ashy ceiling and ensure a clean finish, I am cutting heat to 0% and increasing fan to 80% to transition to convective cooling and prevent stalling while finishing the development. Drop is imminent.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (from 101C to 91C in 5 seconds), indicating a potential sensor anomaly or a significant stall. Since heat is already at 100% and fan is at 0%, I must maintain maximum heat to recover momentum and prevent the roast from stalling early. No fan is needed yet to keep the environment hot.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 132C. With a RoR of 12.3C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure sufficient momentum, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release, which will help stabilize the environment before first crack.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=50% drop=False conf=0.9 — "First crack has just been confirmed (at 557s). The bean temperature is at 178C, which is the typical signature for FC on this roaster. With a RoR of 10.7 C/min, the roast has good momentum. I am increasing the fan to 50% to shift toward convective heat and manage the smoke/chaff, while keeping heat at 100% for a moment to ensure the roast doesn't stall immediately upon the fan increase. I will look to drop as the temperature approaches 190-192C, well before the 196C ceiling, given the detection lag."
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=40% fan=70% drop=False conf=0.95 — 'The bean temperature is at 187C, well into the development phase post-first crack (64s elapsed). With an RoR of 9C/min, the roast is progressing steadily toward the target. Given the 196C hard ceiling and the detection lag, we are approaching the end of the roast. I am reducing heat to 40% to manage the momentum and increasing fan to 70% to shift toward convective cooling and clear smoke, preparing for a drop as we approach the 192-194C range to avoid the 196C bitter threshold.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (negative RoR), likely due to a measurement artifact or a significant thermal shock immediately following charge. With heat at 100% and fan at 10%, the roaster is configured for maximum energy input. I will maintain maximum heat to recover the momentum and keep the fan low to ensure the drum retains sufficient energy to push through the drying phase.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 10.3 C/min and bean temp at 138 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 40% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required during first crack.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=20% fan=80% drop=True conf=0.9 — 'The bean temperature (182.7C) is well past the first crack signature (178C), and the RoR is still positive (9.7C/min). Given the detection lag, the roast is further developed than the 12-second clock suggests. To prevent over-development and avoid the 196C bitter ceiling, I am cutting heat significantly and increasing fan to convective mode to manage the momentum. I am recommending a drop now as the bean temperature is approaching the danger zone and the development is sufficient.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature is at 193.0°C and rising with a RoR of 10.3°C/min. Given the 196°C hard ceiling and the detection lag of first crack, the roast is effectively at the end of its development. Holding any longer risks entering the ashy/bitter range. I am recommending an immediate drop to preserve the current roast profile.'

## prompt_version = v8

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-fixtures/artisan-01 (truth DTR 20.5%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=15.45 dir=0.857; fan MAE=12.5 dir=0.429; latency pre=1.44s preFC=1.14s FC=1.32s
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
    |         hold |     1 |    16 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=21; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-02 (truth DTR 17.9%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-8.0s/-1.0°C; heat MAE=10 dir=0.875; fan MAE=11.6 dir=0.417; latency pre=0.93s preFC=1.17s FC=1.33s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-03 (truth DTR 19.0%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=1.85 dir=0.962; fan MAE=11.11 dir=0.538; latency pre=1.07s preFC=1.19s FC=1.18s
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
    |         hold |     1 |    21 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.962 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-04 (truth DTR 15.7%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/-0.3°C; heat MAE=3.81 dir=0.95; fan MAE=5.71 dir=0.6; latency pre=0.98s preFC=1.21s FC=1.06s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.95 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-05 (truth DTR 19.5%, 27/27 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.19 dir=0.962; fan MAE=13.15 dir=0.423; latency pre=1.39s preFC=1.08s FC=1.21s
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
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=0.962 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-06 (truth DTR 20.7%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-3.0s/-0.7°C; heat MAE=9.17 dir=0.913; fan MAE=8.96 dir=0.565; latency pre=1.46s preFC=1.19s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.913 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-07 (truth DTR 17.2%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/+0.0°C; heat MAE=6.19 dir=0.85; fan MAE=15 dir=0.35; latency pre=1.1s preFC=1.34s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-08 (truth DTR 16.6%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.24 dir=0.85; fan MAE=8.81 dir=0.55; latency pre=0.92s preFC=1.12s FC=1.18s
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
- .artisan-fixtures/artisan-09 (truth DTR 15.3%, 21/21 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.19 dir=0.85; fan MAE=11.9 dir=0.4; latency pre=0.77s preFC=1.24s FC=1.4s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=20     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=0.85 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-10 (truth DTR 24.3%, 23/23 ok): drop F1=0.667 P=0.5 R=1.0 timing=-20.0s/-4.0°C; heat MAE=7.83 dir=0.909; fan MAE=13.7 dir=0.364; latency pre=1.23s preFC=1.13s FC=1.33s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=21     |
    (total ticks=23; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=22; diagonal agreement=0.909 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-11 (truth DTR 14.0%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-10.0s/-1.7°C; heat MAE=10 dir=0.905; fan MAE=5.91 dir=0.667; latency pre=1.02s preFC=1.23s FC=1.31s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    16 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-12 (truth DTR 14.4%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-14.0s/-2.0°C; heat MAE=7.6 dir=0.917; fan MAE=9.8 dir=0.542; latency pre=1.31s preFC=1.23s FC=1.16s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     0 |    19 |     1 |
    |        raise |     1 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-13 (truth DTR 19.9%, 22/22 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=2.27 dir=0.952; fan MAE=15.68 dir=0.286; latency pre=1.01s preFC=1.27s FC=1.39s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=21     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     0 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.952 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-14 (truth DTR 17.7%, 22/22 ok): drop F1=0.667 P=0.5 R=1.0 timing=-7.0s/-1.3°C; heat MAE=7.27 dir=0.905; fan MAE=13.41 dir=0.381; latency pre=1.24s preFC=1.26s FC=1.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=20     |
    (total ticks=22; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    16 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=21; diagonal agreement=0.905 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-15 (truth DTR 22.0%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.33 dir=0.913; fan MAE=8.54 dir=0.609; latency pre=1.47s preFC=1.3s FC=1.49s
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
    |         hold |     1 |    18 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.913 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-16 (truth DTR 13.4%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=1.6 dir=1.0; fan MAE=4.4 dir=0.75; latency pre=0.87s preFC=1.33s FC=1.5s
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
    |         hold |     0 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=1.0 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-17 (truth DTR 25.5%, 21/21 ok): drop F1=0.667 P=0.5 R=1.0 timing=-16.0s/-3.0°C; heat MAE=4.29 dir=1.0; fan MAE=5.24 dir=0.7; latency pre=1.29s preFC=1.19s FC=1.25s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=19     |
    (total ticks=21; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     0 |    16 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=20; diagonal agreement=1.0 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-18 (truth DTR 13.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-6.0s/-0.6°C; heat MAE=11.54 dir=0.88; fan MAE=0.77 dir=1.0; latency pre=1.09s preFC=1.28s FC=1.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     3 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-19 (truth DTR 12.4%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-5.0s/-0.6°C; heat MAE=9.62 dir=0.88; fan MAE=14.42 dir=0.36; latency pre=1.02s preFC=1.2s FC=1.37s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     0 |     0 |     0 |
    |         hold |     3 |    21 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-20 (truth DTR 19.6%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/+0.0°C; heat MAE=11.67 dir=0.87; fan MAE=3.12 dir=0.826; latency pre=1.15s preFC=1.1s FC=1.31s
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
    |        raise |     2 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-21 (truth DTR 16.5%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-23.0s/-4.0°C; heat MAE=10.77 dir=0.88; fan MAE=11.54 dir=0.44; latency pre=1.01s preFC=1.13s FC=1.16s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    20 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-22 (truth DTR 14.6%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-13.0s/-2.3°C; heat MAE=4.62 dir=0.92; fan MAE=7.5 dir=0.6; latency pre=1.26s preFC=1.13s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-23 (truth DTR 12.7%, 31/31 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.1 dir=0.967; fan MAE=4.19 dir=0.7; latency pre=1.02s preFC=1.21s FC=1.22s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=30     |
    (total ticks=31; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     0 |    26 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=30; diagonal agreement=0.967 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-24 (truth DTR 24.1%, 24/24 ok): drop F1=0.667 P=0.5 R=1.0 timing=-17.0s/-2.0°C; heat MAE=9.58 dir=0.87; fan MAE=4.79 dir=0.783; latency pre=0.8s preFC=1.18s FC=1.31s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=22     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    17 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.87 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-25 (truth DTR 14.0%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-25.0s/-2.0°C; heat MAE=4.64 dir=0.963; fan MAE=8.93 dir=0.667; latency pre=0.88s preFC=1.33s FC=1.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     0 |    22 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=27; diagonal agreement=0.963 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-26 (truth DTR 20.5%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-7.0s/-1.0°C; heat MAE=16.43 dir=0.815; fan MAE=10.54 dir=0.519; latency pre=1.12s preFC=1.08s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     2 |    20 |     2 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.815 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-27 (truth DTR 17.8%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=2.92 dir=0.957; fan MAE=13.33 dir=0.391; latency pre=1.49s preFC=1.26s FC=1.3s
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
    |         hold |     1 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.957 — the more informative control-behaviour view)
- .artisan-fixtures/artisan-28 (truth DTR 13.6%, 27/27 ok): drop F1=0.667 P=0.5 R=1.0 timing=-28.0s/-4.3°C; heat MAE=7.41 dir=0.923; fan MAE=3.15 dir=0.769; latency pre=0.86s preFC=1.18s FC=1.43s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=25     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    21 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-fixtures/artisan-01 charge @ 1s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123C and the environment temperature is 160C. The target charge temperature is between 180C and 200C. I am setting the heat to 100% to reach the charge temperature efficiently. Fan is kept at 0% to allow the drum to heat up effectively.'
    - .artisan-fixtures/artisan-01 maillard @ 301s bean=131.3°C ror=14.6 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With a bean RoR of 14.6 C/min and no fan usage yet, the environment temperature is climbing well. I am maintaining full heat to ensure sufficient momentum for the upcoming Maillard phase, but will introduce a small amount of fan to begin transitioning toward convective heat transfer as we approach the mid-roast.'
    - .artisan-fixtures/artisan-01 first-crack @ 511s bean=173.0°C ror=11.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack was detected 7 seconds ago. The bean temperature is 173C, well below the target drop of 189C. The RoR is currently 11C/min, which is healthy for this stage. I will maintain full heat to ensure momentum through the development phase, but I am increasing the fan to 50% to improve convective heat transfer and ensure even development as we approach the target.'
    - .artisan-fixtures/artisan-01 development @ 571s bean=181.0°C ror=8.0 real(heat/fan)=80/60: model heat=60% fan=70% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 8C/min. With 67 seconds of development time and a bean temperature of 181C, we are approaching the target drop of 189C. To ensure a smooth finish without a flick, I am reducing heat slightly to manage the momentum while maintaining airflow to ensure even development. We are approximately 12% into development; holding for another 60-90 seconds should hit the target development ratio.'
    - .artisan-fixtures/artisan-02 charge @ 1s bean=121.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 121°C and rising toward the charge window (180-200°C). No heat or fan is currently applied. I will maintain this state to allow the drum to continue soaking heat until the target charge temperature is reached.'
    - .artisan-fixtures/artisan-02 maillard @ 363s bean=135.3°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With the bean temperature at 135.3°C and a healthy RoR of 12°C/min, I am maintaining full heat to ensure sufficient energy for the upcoming first crack. I am introducing 20% fan now to begin transitioning toward convective heat transfer, which will help manage the bean surface temperature as we approach the end of the Maillard phase.'
    - .artisan-fixtures/artisan-02 first-crack @ 573s bean=173.0°C ror=10.7 real(heat/fan)=90/30: model heat=90% fan=30% drop=False conf=0.9 — 'RoR is healthy at 10.7 C/min and bean temp is 173 C. We are approaching first crack. Maintaining current settings to preserve momentum into the crack, as we are still 17 degrees from the target drop.'
    - .artisan-fixtures/artisan-02 development @ 663s bean=185.0°C ror=7.3 real(heat/fan)=80/70: model heat=50% fan=80% drop=False conf=0.9 — 'The roast is at 185C with a low RoR of 7.3C/min. Development time is 86s, which is ~13% of total time, approaching the target. Given the low RoR and the proximity to the 190C target, I am reducing heat to coast into the drop and increasing fan to ensure clean convective heat for the final seconds. I expect to hit the drop criteria within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-03 charge @ 121s bean=97.0°C ror=-14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (17s elapsed), but the bean temperature telemetry shows a significant, erratic drop (106C to 97C in 5 seconds). This is likely a sensor artifact or a massive stall. Given the heat is already at 100% and fan is at 0%, I must maintain maximum heat to recover momentum and stabilize the bean temperature.'
    - .artisan-fixtures/artisan-03 maillard @ 422s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but slowly. With bean temperature at 131C and RoR at 12C/min, we are in the mid-Maillard phase. To ensure we maintain momentum toward first crack without stalling, I am maintaining full heat. I am introducing 20% fan now to increase convective heat transfer and ensure even development as we approach the crack, while keeping the environment temperature from spiking too aggressively.'
    - .artisan-fixtures/artisan-03 first-crack @ 663s bean=174.7°C ror=10.0 real(heat/fan)=100/30: model heat=80% fan=50% drop=False conf=0.9 — 'First crack occurred 17 seconds ago. The RoR is currently 10.0 C/min, which is healthy but needs to be managed to avoid a flick. I am reducing heat to 80% and increasing fan to 50% to transition to a convective-dominant phase, smoothing the approach to the 190 C drop target. We are still well below the development floor.'
    - .artisan-fixtures/artisan-03 development @ 723s bean=183.3°C ror=8.6 real(heat/fan)=100/90: model heat=100% fan=90% drop=False conf=0.9 — 'The roast is at 183.3C with a development time of 77s (approx 12.4% development). The RoR is 8.6C/min and stable. Given the target drop of 190C and the current trajectory, we are approaching the end of the development phase. I am maintaining high heat to ensure we reach the target temperature without stalling, while keeping fan high for convective efficiency. We are not yet at the drop temperature or the target development percentage, so we continue.'
    - .artisan-fixtures/artisan-04 charge @ 30s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C). It is ready to be charged.'
    - .artisan-fixtures/artisan-04 maillard @ 302s bean=140.7°C ror=13.475 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean temperature of 140.7°C and a healthy RoR of 13.5°C/min, we are approaching the first crack window. Maintaining full heat is appropriate for now to ensure sufficient momentum, but I am increasing the fan slightly to 20% to improve convective heat transfer and prepare for the upcoming exothermic reaction.'
    - .artisan-fixtures/artisan-04 first-crack @ 482s bean=176.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The bean is approaching first crack with a healthy RoR of 11 C/min. I am maintaining full heat to ensure sufficient momentum through the transition, but increasing fan to 30% to prepare for the convective shift required during the development phase and to manage the environment temperature as we approach the crack.'
    - .artisan-fixtures/artisan-04 development @ 542s bean=186.0°C ror=10.0 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.9 — 'The roast is at 186C with a RoR of 10C/min. Development time is 52s (approx 10.3% of total time). The target drop is 191.3C. Given the current RoR, it will take roughly 30-40 seconds to reach the target temp, which will put the development percentage near the 15-16% target. Maintaining current settings to ensure a steady, controlled finish without a flick.'
    - .artisan-fixtures/artisan-05 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature is 156°C, which is below the recommended charge range of 180°C-200°C. Continuing to preheat to reach the target charge temperature.'
    - .artisan-fixtures/artisan-05 maillard @ 390s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early-to-mid Maillard phase (134.7C). With 0% fan, the environment temperature is stable. I am maintaining full heat to ensure sufficient momentum heading into first crack, while keeping fan at 0% to maximize conductive heat transfer and build the necessary energy for the upcoming crack.'
    - .artisan-fixtures/artisan-05 first-crack @ 630s bean=173.7°C ror=9.7 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'First crack was detected 15 seconds ago. The RoR is currently 9.7 C/min and stable. We are early in development (5 seconds in). Need to maintain momentum to reach the target drop temp of 191.7 C without stalling. Keeping heat at 100% and fan at 30% is appropriate to navigate the early development phase.'
    - .artisan-fixtures/artisan-05 development @ 720s bean=183.0°C ror=6.0 real(heat/fan)=100/50: model heat=50% fan=70% drop=False conf=0.9 — 'The RoR has flattened significantly (6.0 C/min) and the bean temperature is approaching the target drop. With 95 seconds of development time already elapsed (approx 13.3% of total roast time), we are nearing the target development ratio. Given the flat RoR and the proximity to the target drop temperature, I am reducing heat to coast into the finish and increasing fan to ensure clean development without stalling. I expect to drop within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-06 charge @ 31s bean=104.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (9 seconds elapsed), but the telemetry shows a significant and concerning drop in bean temperature (from 115C to 104C). This indicates the charge was likely too cool or the initial energy application is insufficient to overcome the thermal mass. I am maintaining maximum heat (100%) and keeping the fan off (0%) to maximize conductive energy transfer and recover the momentum as quickly as possible.'
    - .artisan-fixtures/artisan-06 maillard @ 333s bean=134.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With a bean RoR of 14 C/min and no first crack yet, I am maintaining full heat to ensure sufficient momentum for the upcoming transition. Fan remains at 0 to maximize conductive heat transfer during this phase.'
    - .artisan-fixtures/artisan-06 first-crack @ 543s bean=175.7°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'First crack occurred 10 seconds ago. RoR is healthy at 10C/min. We are at 175.7C, well below the 192.7C target. Maintaining full heat and current fan to sustain momentum through the early development phase. No drop yet.'
    - .artisan-fixtures/artisan-06 development @ 603s bean=183.0°C ror=7.3 real(heat/fan)=90/50: model heat=70% fan=70% drop=False conf=0.9 — 'The roast is progressing well with a stable RoR of 7.3 C/min. We are at 70 seconds of development (approx 12% development ratio), approaching the target drop temperature of 192.7 C. To maintain a gentle, controlled finish without risking a flick or baking, I am reducing heat to 70% and increasing fan to 70% to manage the convective heat transfer as we approach the final drop. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-fixtures/artisan-07 charge @ 1s bean=188.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase, and the bean temperature (188°C) is within the charge guidance range (180-200°C). The machine is ready to be charged.'
    - .artisan-fixtures/artisan-07 maillard @ 303s bean=145.7°C ror=11.803 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 145.7C and a healthy RoR of ~11.8C/min, I am maintaining full heat to ensure sufficient momentum for first crack. I am introducing 20% fan now to begin transitioning toward convective heat transfer, which will help manage the upcoming exothermic phase and prevent scorching as the bean density changes.'
    - .artisan-fixtures/artisan-07 first-crack @ 483s bean=178.0°C ror=10.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'First crack was detected 15 seconds ago. The RoR is currently 10 C/min, which is healthy for this stage. I am maintaining full heat to ensure momentum toward the 193 C drop target, but I am introducing 20% fan now to manage the convective environment and prevent any potential scorching as the beans become more porous. I will monitor for any sudden RoR spikes.'
    - .artisan-fixtures/artisan-07 development @ 543s bean=187.7°C ror=9.7 real(heat/fan)=50/60: model heat=50% fan=60% drop=False conf=0.95 — 'The roast is progressing well with a steady RoR of 9.7 C/min. At 187.7 C, we are approaching the target drop of 193 C. Development time is currently 65 seconds (approx 12% of total time), and we need to maintain momentum without stalling. Keeping heat at 50% and fan at 60% is appropriate to reach the target drop temperature smoothly. I will monitor for the RoR to continue its gentle decline toward the drop point.'
    - .artisan-fixtures/artisan-08 charge @ 30s bean=125.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — "The roast is in the early drying/yellowing phase. The telemetry shows a significant, erratic drop in bean temperature (139C to 125C in 5 seconds), likely due to a probe reading error or a massive thermal swing. Given the heat is at 100% and fan is at 0%, I need to maintain high energy to recover momentum and push through to Maillard, while keeping the fan off to ensure convective heat doesn't strip the bean surface too early."
    - .artisan-fixtures/artisan-08 maillard @ 301s bean=143.0°C ror=12.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'RoR has flattened significantly (12 C/min and stalling). To maintain momentum through the Maillard phase and avoid a stall before first crack, I am increasing airflow to improve convective heat transfer while keeping heat at maximum to push through the current plateau.'
    - .artisan-fixtures/artisan-08 first-crack @ 511s bean=180.0°C ror=10.0 real(heat/fan)=80/40: model heat=80% fan=40% drop=False conf=0.9 — 'The roast is 29 seconds post-first-crack (482s to 511s), which is well into the development phase. The bean temperature is 180C, still 13C away from the target drop of 193C. The RoR is stable at 10C/min. I am maintaining current heat and fan settings to ensure a steady, controlled climb toward the target drop temperature without risking a flick or stalling the roast.'
    - .artisan-fixtures/artisan-08 development @ 541s bean=185.3°C ror=10.6 real(heat/fan)=50/70: model heat=50% fan=70% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 10.6 C/min. At 185.3 C, we are approaching the target drop of 193 C. Development time is currently 48 seconds (approx 9% of total time), so we need to continue development to reach the target ratio. Maintaining current heat and fan settings is appropriate to ensure a smooth, controlled finish without risking a flick or crash.'
    - .artisan-fixtures/artisan-09 charge @ 1s bean=183.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (183C). It is ready to be charged.'
    - .artisan-fixtures/artisan-09 maillard @ 302s bean=143.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the late Maillard phase. With the bean temperature at 143C and a stable RoR of 11.7C/min, we are approaching the first crack. I am maintaining full heat to ensure sufficient momentum, but introducing 20% fan now to increase convective heat transfer and prepare for the upcoming exothermic reaction.'
    - .artisan-fixtures/artisan-09 first-crack @ 483s bean=176.0°C ror=10.3 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack. RoR is healthy at 10.3 C/min. Maintaining full heat and current fan to ensure momentum through the transition. Will look to adjust heat/fan once first crack is confirmed to manage the development phase.'
    - .artisan-fixtures/artisan-09 development @ 543s bean=186.0°C ror=10.0 real(heat/fan)=80/50: model heat=60% fan=70% drop=False conf=0.9 — 'The roast is at 186C with a steady RoR of 10C/min. Development time is 52s (approx 10% of total time), and we are approaching the target drop of 193C. To ensure a smooth finish without a flick, I am reducing heat to 60% and increasing fan to 70% to manage the convective energy as we approach the drop temperature. We are not yet at the drop temperature or the target development percentage, so we continue.'
    - .artisan-fixtures/artisan-10 charge @ 30s bean=110.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (122C to 110C in 5 seconds) despite 100% heat. This indicates a significant stall or probe error, but assuming the reading is accurate, the roast is losing momentum early. I am maintaining 100% heat to recover momentum and keeping fan at 0% to maximize conductive heat transfer during this recovery phase.'
    - .artisan-fixtures/artisan-10 maillard @ 302s bean=129.3°C ror=13.77 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 129.3C and a healthy RoR of 13.77C/min, I am maintaining full heat to ensure sufficient momentum heading into the Maillard phase. I am introducing 20% fan now to begin transitioning toward convective heat transfer, which will help manage the upcoming exothermic reaction and prevent scorching as the bean density changes.'
    - .artisan-fixtures/artisan-10 first-crack @ 512s bean=172.0°C ror=10.3 real(heat/fan)=100/40: model heat=100% fan=40% drop=False conf=0.9 — 'First crack occurred at 481s; we are currently at 495s (14s into development). RoR has dropped to 10.3 C/min and is stabilizing. With 21 C remaining to reach the target drop temp of 193 C, I need to maintain momentum without stalling. Keeping heat at 100% and fan at 40% is appropriate to navigate the development phase toward the target.'
    - .artisan-fixtures/artisan-10 development @ 602s bean=185.0°C ror=9.0 real(heat/fan)=100/20: model heat=80% fan=40% drop=False conf=0.95 — 'The roast is at 185C with a development time of 104s (approx 21.5% development ratio). The RoR is stable at 9C/min. Given the target drop of 193C and the current trajectory, we are approaching the end of the development phase. I am reducing heat to 80% to manage the final approach and increasing fan to 40% to ensure even heat distribution and prevent any potential flick as we approach the drop temperature. We are close enough to the target that we should be ready to drop within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-11 charge @ 30s bean=151.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (168C to 151C in 5 seconds), indicating a severe probe error or a massive thermal crash. Given the heat is at 100% and fan is at 0%, the roaster is struggling to maintain momentum. I am maintaining maximum heat to attempt to recover the RoR before first crack.'
    - .artisan-fixtures/artisan-11 maillard @ 332s bean=142.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.3 C/min and bean temp at 142 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing fan to 25% to improve convective heat transfer and prepare for the upcoming exothermic reaction.'
    - .artisan-fixtures/artisan-11 first-crack @ 542s bean=182.7°C ror=11.0 real(heat/fan)=80/60: model heat=80% fan=60% drop=False conf=0.9 — 'The roast is in the early development phase (13s post-crack). RoR is healthy at 11.0 C/min. With 11 degrees to go until the target drop temperature, I am maintaining heat to ensure steady momentum while keeping fan moderate to prevent a crash. No drop yet.'
    - .artisan-fixtures/artisan-11 development @ 572s bean=187.3°C ror=10.3 real(heat/fan)=80/60: model heat=60% fan=75% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 10.3 C/min. We are at 43 seconds of development (approx 7.7% of total time), approaching the target drop temperature of 193.7 C. To maintain a controlled finish and avoid a late-stage flick, I am reducing heat to 60% and increasing fan to 75% to manage the convective energy as we approach the drop. We are not yet at the drop temperature, so we continue.'
    - .artisan-fixtures/artisan-12 charge @ 153s bean=180.0°C ror=18.3 real(heat/fan)=0/0: model heat=20% fan=30% drop=False conf=0.9 — 'The roast is approaching the target drop temperature (180C current vs 194C target) with heat and fan currently at 0%. The RoR is positive but declining. I need to maintain gentle momentum to reach the target without stalling or crashing. Applying minimal heat and moderate fan to ensure convective heat transfer and prevent scorching as we approach the final stage.'
    - .artisan-fixtures/artisan-12 maillard @ 423s bean=141.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.3 C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure sufficient momentum, but increasing fan slightly to 20% to improve convective heat transfer and prepare for the upcoming exothermic reaction.'
    - .artisan-fixtures/artisan-12 first-crack @ 633s bean=182.0°C ror=11.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack was detected 6 seconds ago. The RoR has dropped to 11 C/min, which is appropriate for this stage. I am increasing the fan to 50% to manage the convective heat transfer and prevent a flick, while maintaining 100% heat to ensure we reach the target drop temperature of 194 C without stalling. We are still early in development (6 seconds in), so we are not ready to drop.'
    - .artisan-fixtures/artisan-12 development @ 663s bean=187.0°C ror=10.0 real(heat/fan)=50/50: model heat=20% fan=60% drop=False conf=0.9 — 'The RoR has flattened significantly (10 C/min) and the bean temperature is approaching the target drop of 194 C. With 36 seconds of development already elapsed and the RoR stabilizing, I am reducing heat to coast into the drop temperature to avoid a flick or overshooting the target. Drop is imminent.'
    - .artisan-fixtures/artisan-13 charge @ 30s bean=127.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (143C to 127C in 5 seconds), indicating a severe stall or probe error. Given the heat is at 100% and fan is at 0%, the environment is hot enough to recover. Maintaining 100% heat is necessary to reverse the momentum and push through the drying/early Maillard phase.'
    - .artisan-fixtures/artisan-13 maillard @ 301s bean=140.0°C ror=12.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 140C. With an RoR of 12.7C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but introducing 20% fan now to improve convective heat transfer and prepare for the upcoming exothermic phase, preventing potential scorching as the bean surface dries.'
    - .artisan-fixtures/artisan-13 first-crack @ 511s bean=177.0°C ror=10.0 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'First crack has just begun (2 seconds ago). The RoR is currently 10 C/min, which is appropriate for the start of development. I am maintaining full heat to ensure momentum through the initial development phase, but I will introduce moderate airflow (fan 30%) to transition toward convective heat transfer and prevent scorching as the beans become more porous. I will monitor for the RoR decline as we approach the target drop temperature.'
    - .artisan-fixtures/artisan-13 development @ 572s bean=186.0°C ror=8.7 real(heat/fan)=70/50: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is at 186C with a steady RoR of 8.7C/min. Development time is 63s (approx 11% of total time). To reach the 194C target, we need to maintain momentum without stalling. Reducing heat slightly to 60% and increasing fan to 60% will manage the convective heat transfer as we approach the drop, preventing a flick while ensuring we reach the target temperature smoothly. We are not yet at the drop temperature.'
    - .artisan-fixtures/artisan-14 charge @ 0s bean=174.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (174C is slightly below the 180C minimum, but the environment is stable). I am setting the heat to 0 and fan to 0 to prepare for the charge. Once the bean temperature stabilizes within the 180-200C range, the roast can commence.'
    - .artisan-fixtures/artisan-14 maillard @ 301s bean=141.0°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has stalled at 141C (11.7 C/min and flatlining). With heat at 100% and fan at 0%, the roaster is heat-saturated but the bean mass is struggling to maintain momentum. I am increasing fan to 25% to improve convective heat transfer and help push through the current plateau, while maintaining full heat to prevent a crash.'
    - .artisan-fixtures/artisan-14 first-crack @ 511s bean=179.0°C ror=10.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is in early development (8s post-FC). The RoR is healthy at 10C/min. With 15C remaining to reach the target drop temp, I need to maintain momentum without causing a flick. Keeping heat at 100% is appropriate for now to sustain the RoR, but I will introduce 20% fan to increase convective heat transfer and ensure even development as the bean density changes post-crack.'
    - .artisan-fixtures/artisan-14 development @ 572s bean=188.0°C ror=9.0 real(heat/fan)=30/100: model heat=30% fan=100% drop=False conf=0.95 — 'The roast is approaching the target drop temperature of 194.3C with a steady, manageable RoR of 9.0C/min. Development time is currently at 69 seconds (approx 12% of total time), and we are nearing the target development. Given the proximity to the target drop temperature and the risk of a flick, I am maintaining current heat to ensure a smooth finish without stalling, while keeping fan at maximum to ensure convective efficiency. I will recommend dropping once the bean temperature hits the target.'
    - .artisan-fixtures/artisan-15 charge @ 31s bean=117.3°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast has just begun (1 second elapsed). The heat and fan are currently at 0%. I need to initiate the roast by applying heat to establish momentum while keeping the fan off to allow the drum to soak and build initial energy.'
    - .artisan-fixtures/artisan-15 maillard @ 332s bean=130.0°C ror=14.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily at 130C. With 100% heat and 0% fan, the RoR is healthy at 14C/min. I am maintaining current settings to continue building momentum toward first crack, as we are still well below the transition phase.'
    - .artisan-fixtures/artisan-15 first-crack @ 543s bean=173.0°C ror=11.298 real(heat/fan)=100/20: model heat=100% fan=20% drop=False conf=0.9 — "First crack has just begun. The RoR is healthy (11.3 C/min) and the bean temperature is 173 C. I need to maintain momentum through the early development phase while preparing for the inevitable RoR decline. Keeping heat at 100% and fan at 20% is appropriate for the first minute of development to ensure the roast doesn't stall before the target drop temperature is approached."
    - .artisan-fixtures/artisan-15 development @ 634s bean=185.0°C ror=8.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is at 185C with a development time of 92s (approx 15% development). The RoR is stable at 8C/min. Given the target drop of 194.7C and the current trajectory, we need to continue development. Maintaining heat at 100% and fan at 30% is appropriate to sustain the momentum toward the target drop temperature without risking a crash or flick. We are not yet at the drop criteria.'
    - .artisan-fixtures/artisan-16 charge @ 31s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is approaching the target charge range (180-200C). No action is required yet as the roaster is still warming up.'
    - .artisan-fixtures/artisan-16 maillard @ 393s bean=137.3°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. RoR is stable at 12.3 C/min. Maintaining full heat is appropriate to build momentum before the approach to first crack, and keeping fan low preserves convective efficiency for the upcoming transition. No changes needed yet.'
    - .artisan-fixtures/artisan-16 first-crack @ 633s bean=179.0°C ror=9.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'First crack has just begun (3 seconds in). The RoR is healthy at 9C/min. I need to maintain momentum through the early development phase while managing the heat to avoid a late-stage spike. Keeping heat at 100% and fan at 30% is appropriate for now to ensure steady development. I will monitor for the RoR to begin its natural decline before adjusting further.'
    - .artisan-fixtures/artisan-16 development @ 693s bean=190.0°C ror=11.0 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.95 — 'The roast is at 190C with a steady RoR of 11C/min. Development time is 63s (approx 10.5% of total time). Given the target drop of 195C and the current momentum, we are approaching the end of the development phase. Reducing heat now will prevent a late-stage spike while maintaining the momentum to reach the target drop temperature safely. Drop is imminent.'
    - .artisan-fixtures/artisan-17 charge @ 1s bean=176.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has reached the target charge temperature range (176C is close to the 180C minimum). I am recommending to continue preheating to reach the optimal charge temperature of 180C before initiating the roast.'
    - .artisan-fixtures/artisan-17 maillard @ 272s bean=138.7°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 12.7 C/min and bean temp at 138.7 C, we are approaching the transition to first crack. Maintaining full heat is appropriate to keep momentum, but I am increasing fan to 20% to improve convective heat transfer and prepare for the upcoming exothermic phase.'
    - .artisan-fixtures/artisan-17 first-crack @ 452s bean=172.0°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is in early development (12s post-crack). The RoR is stable at 10C/min, which is appropriate for this stage. I am maintaining current heat and fan settings to ensure steady momentum toward the target drop temperature of 195C without risking a crash or a flick. We are well below the hard ceiling and the development ratio is still low.'
    - .artisan-fixtures/artisan-17 development @ 542s bean=188.0°C ror=10.7 real(heat/fan)=70/70: model heat=40% fan=80% drop=False conf=0.95 — 'The roast is at 188C with a 10.7C/min RoR. Development time is 102s (approx 19% of total time). With 7C remaining to the 195C target and a steady RoR, we are approaching the drop window. Reducing heat now will prevent a late-stage flick and allow for a controlled finish as we approach the target temperature.'
    - .artisan-fixtures/artisan-18 charge @ 1s bean=119.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature is 156C, which is below the recommended charge range of 180C-200C. I am setting heat to 100% to accelerate the preheat to the target charge temperature. Fan remains off to allow the drum to heat up efficiently.'
    - .artisan-fixtures/artisan-18 maillard @ 391s bean=140.0°C ror=11.3 real(heat/fan)=100/100: model heat=100% fan=100% drop=False conf=0.9 — 'The RoR has stalled at 140C (11.3 C/min and flatlining). With heat and fan already at 100%, the roast is struggling to maintain momentum. I am maintaining full power to push through the Maillard phase toward first crack.'
    - .artisan-fixtures/artisan-18 first-crack @ 632s bean=181.0°C ror=10.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — "First crack was detected 2 seconds ago. The RoR is healthy at 10C/min. I need to maintain momentum through the development phase while managing the heat to avoid a late-stage flick. Keeping heat at 100% and fan at 30% is appropriate for the very early development stage to ensure the roast doesn't stall immediately after crack. I will monitor for the RoR to begin its natural decline before adjusting further."
    - .artisan-fixtures/artisan-18 development @ 692s bean=190.7°C ror=9.7 real(heat/fan)=80/80: model heat=40% fan=80% drop=False conf=0.9 — 'The roast is approaching the target drop temperature of 195.3C with a steady, manageable RoR of 9.7C/min. Development time is currently 62 seconds (approx 9% of total time), and we are closing in on the target development. To ensure a smooth finish without risking a flick or overshooting the bitter ceiling, I am reducing heat to coast into the drop while maintaining airflow for consistency. Drop is imminent within the next 60-90 seconds.'
    - .artisan-fixtures/artisan-19 charge @ 30s bean=90.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early drying phase. The bean temperature readings are erratic and dropping, likely due to probe stabilization or sensor noise immediately post-charge. Heat is at maximum to establish momentum. Fan should remain off to build initial energy and prevent premature cooling. Maintain current settings to stabilize the roast.'
    - .artisan-fixtures/artisan-19 maillard @ 390s bean=137.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has stalled at 137C (10.7 C/min is low for this stage). I need to increase convective heat transfer to push through the end of the drying/early Maillard phase and maintain momentum toward first crack. Increasing fan to 25% while maintaining full heat will help move the heat into the beans more effectively.'
    - .artisan-fixtures/artisan-19 first-crack @ 631s bean=179.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'Approaching first crack (179C indicated). RoR is healthy at 10.3C/min. Maintaining full heat to ensure momentum through the crack, but introducing 20% fan now to transition to convective heat and prepare for the upcoming development phase.'
    - .artisan-fixtures/artisan-19 development @ 691s bean=189.0°C ror=10.0 real(heat/fan)=100/40: model heat=50% fan=60% drop=False conf=0.9 — 'The bean temperature is approaching the target drop temperature (189C vs 195.3C) and the RoR has flattened significantly (10C/min). Given the development time is already 54 seconds and the roast is progressing steadily, I am reducing heat to manage the final approach and increasing fan to ensure even heat distribution as we near the drop. We are close enough to the target that a gentle finish is appropriate.'
    - .artisan-fixtures/artisan-20 charge @ 30s bean=97.0°C ror=None real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the very early drying/yellowing phase. The bean temperature is dropping rapidly (likely due to probe noise or thermal equilibrium adjustment after charge), but heat is at maximum. Maintaining 100% heat is appropriate to build momentum for the Maillard phase. Fan is kept low to retain heat.'
    - .artisan-fixtures/artisan-20 maillard @ 331s bean=139.0°C ror=13.3 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'RoR is healthy at 13.3 C/min and bean temp is 139 C. We are approaching the Maillard phase. Maintaining full heat to drive momentum, but increasing fan slightly to 20% to improve convective heat transfer and prepare for the upcoming exothermic transition.'
    - .artisan-fixtures/artisan-20 first-crack @ 542s bean=179.0°C ror=9.836 real(heat/fan)=80/50: model heat=80% fan=50% drop=False conf=0.9 — 'The roast is in early development (6s post-crack). RoR has flattened significantly (9.8 C/min) and the bean temperature is 179 C. With a target drop of 196 C, there is still significant development time required. Maintaining current heat (80%) and fan (50%) is appropriate to sustain a steady, gentle momentum toward the target without risking a crash or a flick.'
    - .artisan-fixtures/artisan-20 development @ 602s bean=188.0°C ror=9.0 real(heat/fan)=50/90: model heat=30% fan=95% drop=False conf=0.9 — 'The roast is at 188°C with a low RoR of 9°C/min. Development time is 66 seconds, which is ~11% of total time, but the bean temperature is approaching the 196°C ceiling. Given the slow momentum and the proximity to the drop temperature, I am reducing heat to coast to the finish and increasing fan to ensure even development without stalling. Drop is imminent as we approach the target temperature.'
    - .artisan-fixtures/artisan-21 charge @ 61s bean=85.0°C ror=-27.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is currently in a severe stall/crash (RoR -27.0 C/min). The bean temperature is dropping rapidly despite maximum heat. This indicates a likely probe error or a massive thermal instability. I am maintaining maximum heat to attempt to recover the momentum, but the situation is critical.'
    - .artisan-fixtures/artisan-21 maillard @ 392s bean=135.0°C ror=13.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 135C. With heat at 100% and fan at 0%, the environment temperature is stable. I am introducing 20% fan now to begin transitioning to convective heat transfer as we approach the Maillard phase, while maintaining full heat to keep the RoR from dropping prematurely.'
    - .artisan-fixtures/artisan-21 first-crack @ 633s bean=178.0°C ror=10.0 real(heat/fan)=100/20: model heat=100% fan=40% drop=False conf=0.9 — 'First crack was detected 3 seconds ago. The bean temperature is 178C, well below the 196C ceiling. The RoR is healthy at 10C/min. I need to maintain momentum through the development phase while increasing airflow to manage the convective heat transfer as the beans become more porous. I will increase fan to 40% to improve airflow and keep heat at 100% to sustain the RoR.'
    - .artisan-fixtures/artisan-21 development @ 693s bean=187.0°C ror=9.0 real(heat/fan)=60/90: model heat=60% fan=90% drop=False conf=0.9 — 'The roast is at 187C with a steady RoR of 9C/min. Development time is 63s (approx 10% of total time). Given the target drop of 196C and the current steady momentum, we are approaching the end of the development phase. Maintaining current settings to allow a controlled, steady climb to the target drop temperature without risking a flick or stall.'
    - .artisan-fixtures/artisan-22 charge @ 1s bean=117.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 117.0°C, which is below the recommended charge range of 180.0°C - 200.0°C. Continue preheating until the target charge temperature is reached.'
    - .artisan-fixtures/artisan-22 maillard @ 392s bean=137.0°C ror=11.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has stalled at 137C (11 C/min), indicating a need for more convective energy to push through the late Maillard phase. I am increasing fan speed to 30% to improve heat transfer and maintain momentum, while keeping heat at 100% to prevent a crash before first crack.'
    - .artisan-fixtures/artisan-22 first-crack @ 632s bean=179.7°C ror=10.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'First crack was detected 10 seconds ago. The RoR is currently 10.7 C/min, which is healthy for this stage. I am maintaining full heat and low fan to ensure momentum through the early development phase, as the bean temperature is still 16.6 C away from the target drop temperature. No changes needed yet.'
    - .artisan-fixtures/artisan-22 development @ 692s bean=189.0°C ror=9.3 real(heat/fan)=60/80: model heat=50% fan=80% drop=False conf=0.9 — 'The roast is progressing well with a steady RoR of 9.3 C/min. Development time is currently 69 seconds (approx 10% of total time), and the bean temperature is 189 C. With a target drop of 196.3 C, we are approaching the end of the roast. I am reducing heat slightly to 50% to manage the momentum as we approach the target, while maintaining fan at 80% to ensure even heat transfer and avoid any potential flick. We are not yet at the drop point.'
    - .artisan-fixtures/artisan-23 charge @ 211s bean=97.0°C ror=-17.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is experiencing a severe, erratic drop in bean temperature (RoR -17.7 C/min) despite maximum heat input. This indicates a sensor error or a massive thermal instability. I am maintaining maximum heat to attempt to stabilize the bean temperature and recover the momentum, while keeping fan low to minimize convective cooling losses.'
    - .artisan-fixtures/artisan-23 maillard @ 571s bean=135.0°C ror=10.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 135°C and RoR at 10.3°C/min, we are in a good position. I am maintaining full heat to ensure momentum is preserved as we approach first crack, while keeping fan low to maximize conductive heat transfer and build body. No changes needed yet.'
    - .artisan-fixtures/artisan-23 first-crack @ 811s bean=178.0°C ror=11.3 real(heat/fan)=100/20: model heat=100% fan=20% drop=False conf=0.9 — "First crack just started (4s ago). RoR is healthy at 11.3 C/min. I need to maintain momentum through the development phase while managing the heat to avoid a late-stage flick. Keeping heat at 100% and fan at 20% is appropriate for the very beginning of development to ensure the roast doesn't stall, but I will prepare to reduce heat and increase fan in the next minute as the bean temperature climbs toward the target."
    - .artisan-fixtures/artisan-23 development @ 871s bean=191.0°C ror=13.0 real(heat/fan)=50/100: model heat=30% fan=100% drop=False conf=0.95 — 'The roast is at 191C with a steady RoR of 13C/min. We are approaching the target drop temperature of 196.3C. Given the current momentum and the proximity to the ceiling, I am reducing heat to 30% to gently coast to the drop temperature without risking a flick or overshooting the bitter threshold. We are very close to the target drop; I will monitor for the final 5 degrees.'
    - .artisan-fixtures/artisan-24 charge @ 1s bean=99.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and has reached the target charge temperature range (180-200C). Ready to charge.'
    - .artisan-fixtures/artisan-24 maillard @ 302s bean=125.3°C ror=14.3 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With bean temperature at 125.3C and a healthy RoR of 14.3C/min, I am maintaining full heat to ensure sufficient momentum before the approach to first crack. Fan remains at 0% to maximize conductive heat transfer during this phase.'
    - .artisan-fixtures/artisan-24 first-crack @ 512s bean=167.7°C ror=12.0 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With a bean RoR of 12 C/min and bean temp at 167.7 C, we are in the late Maillard phase. Maintaining full heat is appropriate to ensure sufficient momentum for the upcoming crack, while keeping fan at 30% preserves convective efficiency without cooling the drum prematurely. No changes needed yet.'
    - .artisan-fixtures/artisan-24 development @ 603s bean=185.0°C ror=12.0 real(heat/fan)=90/30: model heat=60% fan=50% drop=False conf=0.9 — 'The roast is at 185C with 85 seconds of development time. The RoR is stable at 12C/min. With a target drop of 197C, we have 12C to go. Given the current momentum and the proximity to the hard ceiling, I am reducing heat to 60% to manage the final approach and increasing fan to 50% to ensure convective heat transfer and prevent stalling or baking as we approach the drop. We are not yet at the drop temperature.'
    - .artisan-fixtures/artisan-25 charge @ 62s bean=124.0°C ror=29.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 165.7C, which is below the charge guidance range (180C-200C). I am maintaining zero heat and fan to allow the roaster to continue climbing toward the target charge temperature.'
    - .artisan-fixtures/artisan-25 maillard @ 453s bean=134.0°C ror=9.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The RoR has stalled at 9.3 C/min and the bean temperature is stagnant at 134 C despite 100% heat. The roast is stalling in the mid-Maillard phase. I need to increase airflow to improve convective heat transfer and pull more energy into the beans to recover momentum before first crack.'
    - .artisan-fixtures/artisan-25 first-crack @ 723s bean=182.0°C ror=11.7 real(heat/fan)=70/40: model heat=50% fan=60% drop=False conf=0.9 — 'RoR has flattened significantly (11.7 C/min) and the bean temperature is approaching the target. With 19 seconds of development post-crack, we are entering the final stage. Reducing heat further to 50% to manage the momentum and increasing fan to 60% to ensure convective heat transfer and prevent stalling while approaching the drop temperature. We are close to the target drop temp; preparing for the drop.'
    - .artisan-fixtures/artisan-25 development @ 753s bean=189.0°C ror=12.7 real(heat/fan)=10/60: model heat=0% fan=60% drop=False conf=0.95 — 'The roast is at 189C with a development time of 49s (approx 7% of total time). The RoR is stable at 12.7C/min. Given the target drop of 197C and the need to avoid the 196C bitter ceiling, I am reducing heat to 0% to coast into the drop. The development ratio is still low, but the bean temperature is approaching the limit. I will hold fan at 60% to maintain airflow without crashing the RoR. Drop is not yet indicated as we are 8C away from target.'
    - .artisan-fixtures/artisan-26 charge @ 151s bean=106.0°C ror=-7.377 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (RoR -7.377 C/min) despite 100% heat. This indicates a significant stall or probe error following the charge. I am maintaining maximum heat to recover momentum and keeping fan low to minimize convective cooling while the bean mass stabilizes.'
    - .artisan-fixtures/artisan-26 maillard @ 451s bean=134.0°C ror=13.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 134C. With an RoR of 13.3C/min, we are in the mid-Maillard phase. It is time to introduce airflow to transition toward convective heat transfer and prepare for the upcoming first crack. Maintaining full heat for now to sustain momentum, but increasing fan to 25% to manage the environment and ensure even development.'
    - .artisan-fixtures/artisan-26 first-crack @ 661s bean=176.0°C ror=11.0 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'First crack was detected 6 seconds ago. The RoR is currently 11 C/min and stable. I need to maintain momentum to reach the target drop temperature of 197 C without stalling, but I should introduce some airflow to manage the convective heat transfer as we progress through development. Keeping heat at 100% for now to support the RoR, while increasing fan to 30% to ensure even development.'
    - .artisan-fixtures/artisan-26 development @ 721s bean=186.0°C ror=10.0 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is at 186C with a development time of 66 seconds (approx 11% development ratio). The RoR has flattened significantly (10C/min) and is approaching the target drop temperature of 197C. To avoid a crash or a flick while approaching the ceiling, I am reducing heat to manage the momentum and increasing fan to ensure convective heat transfer for the final stretch. I will hold for a few more minutes to reach the target development ratio and temperature.'
    - .artisan-fixtures/artisan-27 charge @ 31s bean=91.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early drying phase. The bean temperature is dropping rapidly (likely due to probe stabilization after charge), and the environment temperature is low. Maintaining maximum heat is necessary to build momentum for the Maillard phase. Fan should remain off to maximize conductive heat transfer and prevent premature cooling.'
    - .artisan-fixtures/artisan-27 maillard @ 332s bean=132.3°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. Bean RoR is healthy at 12.3 C/min. With the bean temperature at 132.3 C, we are approaching the mid-Maillard transition. I am maintaining full heat to ensure sufficient momentum, but introducing 20% fan now to begin transitioning toward convective heat transfer, which will help manage the upcoming exothermic phase and prevent scorching as we approach first crack.'
    - .artisan-fixtures/artisan-27 first-crack @ 573s bean=178.0°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=40% drop=False conf=0.9 — 'First crack was detected 4 seconds ago. The RoR is currently 10.7 C/min, which is healthy for this stage. I am increasing the fan to 40% to transition to convective heat and manage the energy as we approach the target drop temperature of 197 C. Heat remains at 100% for now to maintain momentum, but I will look to reduce it in the next adjustment as the bean temperature climbs toward the ceiling.'
    - .artisan-fixtures/artisan-27 development @ 633s bean=187.0°C ror=9.0 real(heat/fan)=80/50: model heat=80% fan=50% drop=False conf=0.9 — 'The roast is at 187C with a steady RoR of 9C/min. Development time is 64s (approx 10.3% of total time). Given the target drop of 197C and the current trajectory, we are approaching the end of the development phase. Maintaining current heat and fan settings to ensure a smooth, controlled finish without risking a flick or stalling. Will monitor closely for the 197C target.'
    - .artisan-fixtures/artisan-28 charge @ 61s bean=86.0°C ror=-16.0 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is in the very early stages (19 seconds elapsed), but the bean temperature is dropping rapidly (-16 C/min RoR). The current heat is at 100%, but the bean temperature is falling, suggesting a potential sensor issue or a massive heat sink effect. I am maintaining maximum heat to stabilize the bean temperature and prevent a stall, while keeping fan low to maximize conductive heat transfer.'
    - .artisan-fixtures/artisan-28 maillard @ 422s bean=138.0°C ror=10.3 real(heat/fan)=100/20: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean temperature of 138C and a healthy RoR of 10.3C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing fan speed slightly to 30% to improve convective heat transfer and prepare for the upcoming exothermic phase.'
    - .artisan-fixtures/artisan-28 first-crack @ 692s bean=182.7°C ror=9.7 real(heat/fan)=90/60: model heat=90% fan=60% drop=False conf=0.9 — 'Development is only 12 seconds in, with a bean temperature of 182.7C. The RoR is healthy at 9.7C/min. I need to maintain momentum to reach the target drop temperature of 197.3C without stalling. Keeping heat high and fan at 60% provides sufficient convective energy to finish the roast smoothly.'
    - .artisan-fixtures/artisan-28 development @ 752s bean=193.0°C ror=10.3 real(heat/fan)=100/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature is at 193°C and rising with a healthy RoR of 10.3°C/min. We are approaching the hard ceiling of 196°C. With 72 seconds of development time already elapsed, we are very close to the target drop temperature. Given the momentum and the proximity to the bitter ceiling, I am dropping now to avoid over-roasting.'