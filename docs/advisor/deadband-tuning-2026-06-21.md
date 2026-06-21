# Post-FC deadband threshold tuning (21 Jun 2026, #277)

Data-grounded tuning of `ControllerConfig.post_fc_deadband_threshold_percent`
(the post-FC coherence gate's reversal-magnitude floor, #276) from the
operator's OWN recorded roasts. Deterministic, no API key, no network.

- Script: `scripts/deadband_tune.py` (reproduce with
  `python scripts/deadband_tune.py`).
- Test set: the 17 known-good medium Artisan roasts
  (`FULL_MEDIUM_FIXTURE_NAMES`, the same eval set the bake-off uses).
- Phase: DEVELOPMENT only (first crack -> drop), the as-built D35 advisor scope.
  Lever COMMAND sequences taken from the raw fixture telemetry between the
  `first_crack_detected` and `beans_dropped` events (boundaries from
  `bakeoff_replay.load_roast`, so they match the eval's ground truth).
- Raw `.artisan-fixtures` data is local-only (gitignored); only the aggregates
  below are committed.

## What the gate does

`coherence.evaluate_lever_coherence` damps a per-lever direction REVERSAL when
`abs(delta) < post_fc_deadband_threshold_percent`. The #218 thesis: damp
incoherent flip-flop (the `30<->40<->30` staircase) while ALLOWing the operator's
intentional, decisive moves. The right threshold is therefore the LARGEST value
that still passes essentially all of the operator's real reversals (we never want
to damp an intentional move) while still catching sub-threshold jitter.

## Measured reversal distribution

The Hottop's heat / fan levers are quantised to **10 percentage points** — every
recorded move is a multiple of 10 pp, so the operator's smallest possible
reversal is 10 pp.

### HEAT lever

- 102 non-zero moves across 17 roasts; move magnitude min 10 / median 10 /
  p90 20 / max 50 / mean 14.4 pp.
- **21 direction reversals**, in 10/17 roasts. Reversal magnitudes (pp):
  `[10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 20, 20, 20, 30, 40, 50]`
  (min 10 / median 10 / p90 30 / max 50 / mean 15.7).

### FAN lever

- 86 non-zero moves across 17 roasts; move magnitude min 10 / median 10 /
  p90 30 / max 50 / mean 17.2 pp.
- **11 direction reversals**, in 7/17 roasts. Reversal magnitudes (pp):
  `[10, 10, 10, 10, 20, 20, 20, 20, 30, 30, 50]`
  (min 10 / median 20 / p90 30 / max 50 / mean 20.9).

Takeaway: the operator's post-FC reversals are relatively rare and every one is
**>= 10 pp**. Reversals are NOT predominantly tiny jitter — many are decisive
(20-50 pp), and even the smallest (10 pp) are deliberate, single-step moves at
the roaster's lever granularity.

## Per-threshold damping table

For each candidate threshold, replaying the operator's real development sequences
through the production gate: how many of the operator's REAL reversals are
damped (intentional moves we'd suppress — want 0) vs how many reversals fall
below the threshold (the jitter band the gate is meant to catch).

### HEAT

| threshold | operator reversals damped | allowed | sub-threshold |
| --- | --- | --- | --- |
| 5 | 0 | 21 | 0 |
| 8 | 0 | 21 | 0 |
| 10 | 0 | 21 | 0 |
| 12 | 13 | 6 | 13 |
| 15 | 13 | 6 | 13 |
| 20 | 13 | 6 | 13 |

### FAN

| threshold | operator reversals damped | allowed | sub-threshold |
| --- | --- | --- | --- |
| 5 | 0 | 11 | 0 |
| 8 | 0 | 11 | 0 |
| 10 | 0 | 11 | 0 |
| 12 | 4 | 7 | 4 |
| 15 | 4 | 7 | 4 |
| 20 | 4 | 7 | 4 |

The boundary is sharp and identical for both levers: at threshold **<= 10** the
gate damps **zero** of the operator's real reversals (a 10 pp reversal needs
`10 < threshold` to be damped, so it passes at 10). At threshold **>= 11** the
13 heat + 4 fan single-step (10 pp) intentional reversals start being damped.

## Recommendation: 10 (changed from the placeholder 15)

**Set `post_fc_deadband_threshold_percent` default to 10.** It is the largest
value that damps ZERO of the operator's real intentional reversals while still
catching any sub-10-pp (sub-granularity) jitter. The evidence:

- The operator's post-FC reversals are all >= 10 pp, so threshold 10 damps zero
  real moves.
- The prior placeholder **15** would have damped **13 heat + 4 fan** real
  operator reversals — exactly the decisive, intentional moves the gate is
  required to let through (D35 §1). 15 was a guess and it was too high.
- A lower value (5 / 8) is equivalent on this data (also damps zero) but buys
  nothing: at 10 pp granularity there is no reversal strictly between 0 and 10
  to catch, so 10 maximises the headroom for "jitter would have to be smaller
  than a single lever step to be damped" without ever touching a real move.

### Honest caveat (the limit of a pure-magnitude deadband)

Because the operator's intentional reversals AND the #218 `30<->40<->30` twiddle
are **both 10 pp** on this roaster, a pure-magnitude deadband at 10 pp
granularity **cannot** separate them — any threshold high enough to damp the
twiddle (>= 11) also damps the operator's real single-step reversals. So the
magnitude floor is deliberately set to NOT damp 10 pp moves; what actually bounds
the #218 staircase is the gate's #276 direction-advancing oscillation damping (a
repeated alternation keeps re-reversing the advanced direction and stays damped),
not this threshold. The threshold's job here is narrowed to its honest role:
suppress only sub-lever-step noise, never a real operator move.

## `post_fc_min_confidence` (0.2) — confirmed, unchanged

`post_fc_min_confidence` gates on the **model's** advice confidence, which is not
present in the operator's recorded telemetry — the operator data cannot tune it.
It stays at the conservative fail-closed default **0.2** (a near-zero-confidence
recommendation holds; legitimate advice passes), which the recorded data neither
supports changing nor contradicts. No change.
