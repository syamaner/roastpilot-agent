# Post-FC advisor validation — expanded corpus (#277 re-validation, 28 Jun 2026)

> **⚠ FIGURES CORRECTED 11 Aug 2026 (#779 / PR #784, tracked as #792). The central
> conclusion survives; two statements did not.** Every DTR below was originally computed
> on **confirmation**-anchored marks. That does not shift DTR in a single direction: a late
> FC mark shortens development and understates the ratio, while a late T0 mark shortens the
> total and *overstates* it, so the net depends on which backdate dominates. The fixtures were
> regenerated on the corrected **onset** anchors and each direction was derived from that
> roast's own store data rather than assumed — they genuinely differ in sign:
>
> | label | DTR then | DTR now | Δ | development s |
> |---|---:|---:|---:|---|
> | `store-roast-3fbfd888` | 10.8 | **11.5** | +0.7 | 63.2 → 67.3 |
> | `store-roast-5a32334c` | 12.7 | **12.6** | −0.1 | 88.8 → 88.8 |
> | `store-roast-d251013e` | 11.1 | **12.9** | +1.8 | 77.3 → 90.9 |
>
> `5a32334c` is the only DOWNWARD move, and structurally so: its first crack was
> operator-marked, and #784 honours that override by leaving the FC mark alone, so only
> the T0 anchor moved and only the denominator grew.
>
> **What survives.** The report's central finding — Colombia Washed roasts 5 and 6
> under-developed against the 17–18 % per-origin prior — is **unchanged**; at 12.6 % and
> 12.9 % they are still 4–5 pp short. So is every trace-level observation (fan escalation
> to 100 %, the five `development_incoherent` drop rejections, the drop-guard interaction),
> because none of those depended on the anchor.
>
> **What changed.** (1) `d251013e` **has since been rated 3/5** ("tasted a bit flat.
> Sightly underdeveloped"), so the "only `5a32334c` qualifies" statement below and its
> matching action item are both retired — the corpus now carries two labelled bake-off
> entries, not one. (2) First-crack temperatures were re-resolved: 188 → 187, 185 → 183,
> and `5a32334c` gained a value (186 °C) where this report shows `n/a`. **I have now been wrong
> twice about why, so the causal claim is withdrawn rather than replaced a third time.** What is
> established: the anchor did NOT move (`store_to_fixture` sets `first_crack_source` only for an
> `mcp`-accepted crack, so an operator-marked FC stays on the event row), and **#784 is not the
> mechanism either** — `_first_crack_temp_c` already read the nearest telemetry row before #784
> (`f45db35~1:583-587`). The change is in what the manifest FIELD records: the old `null` reflected
> the operator event carrying no bean temperature, while the entry is now populated from the
> exported summary's nearest-row reading. No #784 behaviour explains it.
>
> **Provenance, corrected 11 Aug.** The frozen `development_percent` is **version-stamped at
> #337** (the FC/T0 backdating consumer, closed 23 Jun 2026), so this corpus holds **two
> definitions of DTR**. `3fbfd888` ran 21 Jun, *before* it: its frozen 10.84 faithfully records
> the receive-time anchor the controller used then — a version artefact, nothing to adjudicate.
> `5a32334c` (27 Jun) agrees to **0.02 pp** (frozen 12.58 vs onset 12.60), so there is no
> disagreement on that run at all. **Only `d251013e` is genuinely anomalous:** post-#337 and
> MCP-sourced, its frozen 14.09 *exceeds* the onset-derived 13.01 by 1.08 pp — the wrong
> direction for any anchoring explanation. That one outlier is the open #792 question, and it
> is much narrower than "the frozen column disagrees". The wider consequence is that June and
> August DTRs are **not like-for-like** *within this store corpus*. It does **not** reach the
> per-origin priors: those come from the Artisan `.alog` corpus plus external research, and
> `scripts/alog_classify.py` computes them from Artisan event marks without ever reading
> `telemetry_snapshots.development_percent` — so #337 cannot contaminate them, and re-checking
> them on that basis would be a false calibration task. See #792.

> **Read first — what these numbers mean.** This is a re-validation of the 21 Jun
> pin (`openai/gpt-4o`, c1 prompt) against the expanded recorded corpus from roasts
> 3–6. It is NOT a scored bake-off run (no API calls were made — `OPENROUTER_API_KEY`
> not available in this environment). Instead it is an **observational validation**:
> what did the pinned model (`gpt-4o`) actually do in the post-FC loop on the three
> newly converted store fixtures, read directly from the advisory event trace logged in
> the store? This is a complementary lens to the 21 Jun bake-off: the bake-off scored
> hypothetical agreement with Artisan roasts; this reads what happened on the real
> hardware during the real roast.

## Context: what was already pinned

The 21 Jun bake-off (`docs/advisor/bakeoff-results-2026-06-21.md`) pinned
`openai/gpt-4o` via OpenRouter with the `c1` control teaching prompt. That run
scored 17 known-good Artisan mediums × 2 seeds, dev-only (post-FC), and found:

- Heat MAE ≈7.5 pp (best in field), heat-direction 0.78 (best in field)
- Drop F1 ≈0.86, no never-drop failures
- Median FC-slot latency ≈2.0 s (inside the 10 s gate)

The open question: does gpt-4o hold up on the expanded agent-recorded corpus (real
hardware, real roasts run by the agent itself), not just the Artisan reference set?

---

## Corpus: the three store-sourced fixtures

Converted by `scripts/store_to_fixture.py` (with the v6-schema compatibility fix,
see section below). Full manifest: `docs/advisor/store-roast-corpus-manifest.json`.

| label | profile | FC source | FC °C | drop °C | DTR % | degree | rating | notes |
|-------|---------|-----------|-------|---------|-------|--------|--------|-------|
| store-roast-3fbfd888 | Ethiopia Yirgacheffe Koke (Natural) | mcp | 187 | 203 | 11.5 | over | 2/5 | bitter, too dark |
| store-roast-5a32334c | Colombia Excelso Huila (Washed) | operator | 186 | 191 | 12.6 | core_medium | 3/5 | sweet, caramel hints |
| store-roast-d251013e | Colombia Excelso Huila (Washed) | mcp | 183 | 190 | 12.9 | core_medium | 3/5 | flat, slightly underdeveloped |

**Two entries now qualify as labelled known-good** for the bake-off set (rated +
`core_medium` degree): `store-roast-5a32334c` and — since its 11 Aug rating —
`store-roast-d251013e`. Only `3fbfd888` remains blocked, as over-done (negative corpus).
*(As written on 28 Jun only `5a32334c` qualified, `d251013e` being unrated then.)*

---

## Observational post-FC trace analysis

### Roast 3 (3fbfd888, Ethiopia Natural, drop 203 °C, rating 2 = bitter)

**Post-FC sequence (9 decision ticks, ≈67 s development):**

| tick# | heat % | fan % | should_drop | verdict | bean °C |
|-------|--------|-------|-------------|---------|---------|
| 1 | 50 | 30 | false | allow | 188 |
| 2 | 30 | 30 | false | allow | — |
| 3 | 10 | 30 | false | allow | 193 |
| 4 | 0 | 30 | **true** | allow | 195 |
| 5 | 0 | 30 | **true** | allow | 197 |
| 6 | 0 | 40 | **true** | allow | — |
| 7 | 0 | 40 | **true** | allow | — |
| 8 | 0 | 40 | **true** | allow | — |
| 9 (drop) | 0 | 40 | **true** | allow | 203 |

**Controller's drop guard:** gpt-4o called `should_drop=True` from tick 4 onward
(at 195 °C bean temp, which matched the profile's `target_drop_temp_c = 195`).
However, 5 consecutive `drop_rejected: development_incoherent` events show the
**controller's dev-guard blocked the drop** — dev% was 5–10% against a 13% target,
and the 3% `drop_dev_margin_percent` guard required at least 10% (13 - 3). The model
correctly identified the drop temp had been reached; the system held for development.

**Outcome:** The drop finally executed at 203 °C (the development guard eventually
released — the DTR **the guard actually read** reached ~10.8 %, the receive-time value
in force pre-#337). The corrected onset-derived figure for the same roast is 11.5 %,
but that is the *physical* development, not the signal the loop acted on: this
paragraph and the margin arithmetic below are a **control** narrative, so they must use
the controller-time number or they rewrite the decision. The bean temperature was already past the 195 °C
profile target and the operator's 195 °C bitter ceiling. **This is a CONTROL LOOP
ISSUE, not a model failure.** The dev guard (intended to prevent premature drops)
delayed the drop past the correct moment, producing the over-roast.

**Signal for operator decision:** The 13% DTR target is too ambitious for the
Hottop's thermal dynamics at the post-FC heat trajectory. By the time 13% DTR is
reached, bean temp has gone 8 °C over the ceiling. Either (a) reduce the
development target for naturals (per-origin DTR prior, cf. D59), or (b) lower the
drop guard margin, or (c) make drop-guard gate on bean-temp ceiling override (drop
at ceiling regardless of DTR). **Operator decision required — do not change
controller logic here.**

### Roast 5 (5a32334c, Colombia Washed, drop 191 °C, rating 3 = acceptable)

**Post-FC sequence (12 decision ticks, ≈89 s development, FC = operator-marked):**

| tick# | heat % | fan % | should_drop | verdict | bean °C |
|-------|--------|-------|-------------|---------|---------|
| 1 | 0 | 50 | false | allow | 186 |
| 2 | 0 | 70 | false | allow | 187 |
| 3 | 0 | 90 | false | allow | 187 |
| 4 | 0 | 100 | false | allow | 188 |
| 5–11 | 0 | 100 | false | allow | 188–190 |
| 12 (drop) | 0 | 100 | **true** | allow | 191 |

**Model behaviour:** Heat cut to 0% immediately at FC (aggressive cooling), fan
ramped systematically 50→70→90→100% to slow the RoR. No drop rejections — the dev
guard did not fire. Drop called exactly at 191 °C (12.6% DTR). This is a clean
profile: model correctly managed the temperature trajectory.

**Concern flagged (operator decision required):** The model cut heat to 0% at FC
and raised fan to 100% across the entire development window. This is very aggressive
cooling. While the outcome was acceptable (rating 3, caramel notes), a 191 °C drop
on Colombia Washed is on the light end for washed high-grown beans (per-origin priors
in D59 recommend 17–18% DTR for washed high-grown, memory
`per-origin-dtr-washed-highgrown.md`). DTR was 12.6%, below the per-origin target.
**The model appears to be over-cooling Colombia Washed** — the fan-100 + heat-0
pattern from 186 °C bean temp through drop may explain the under-development.

### Roast 6 (d251013e, Colombia Washed, drop 190 °C, rating 3 = flat/slightly underdeveloped)

**Post-FC sequence (11 decision ticks, ≈91 s development):**

| tick# | heat % | fan % | should_drop | verdict | bean °C |
|-------|--------|-------|-------------|---------|---------|
| 1 | 20 | 50 | false | allow | 185 |
| 2 | 0 | 70 | false | allow | 186 |
| 3–4 | 0 | 70–80 | false | allow | 187 |
| 5–6 | 0 | 85 | false | allow | — |
| 7–8 | 0 | 90–95 | false | allow | — |
| 9–10 | 0 | 100 | false | allow | — |
| 11 (drop) | 0 | 100 | **true** | allow | 190 |

**Consistent pattern with roast 5:** heat cut to near-zero immediately post-FC,
fan ramped aggressively upward. Drop at 190 °C, DTR 12.9 % — the corrected
onset-derived figure. The **controller-time** value frozen on this run is 14.09 %,
and the 1.08 pp gap between them, in a direction no anchoring story explains, is the
open #792 question; the trace table above is what the loop saw. Same structural
behaviour, slightly different starting heat (20% vs 0%), same fan escalation.

---

## Findings vs the 21 Jun scorecard

### What confirms

1. **gpt-4o is not a never-dropper.** On all three store roasts, the model
   called `should_drop=True` at an appropriate bean temperature (191–203 °C range).
   No never-drop failure observed. Consistent with the 21 Jun drop F1 ≈0.86.

2. **Heat direction is correct.** Post-FC heat is cut promptly (to 0–20%) and
   never raised. This matches the 21 Jun heat-direction agreement of ≈0.78.

3. **All decisions were within safety policy (ALLOW / all_clear).** No CLAMP,
   REJECT, or FAULT verdicts observed in any post-FC tick.

4. **FC-slot drop call timing seems appropriate:** model called drop within 1 tick
   of reaching the profile's target temp on roasts 5 and 6. No premature drops.

### What diverges or concerns

5. **The 3%-DTR drop guard is interacting badly with high heat profiles.** On
   roast 3 (Ethiopia Natural), gpt-4o correctly identified the drop moment, but
   the controller's development guard blocked it 5 times while bean temp climbed
   from 195 to 203 °C. The model is not at fault; the guard logic is. **This is
   a controller issue (filed as #313/#325), not a model issue — confirming the
   existing open bugs.**

6. **Fan escalation pattern is aggressive for washed high-grown.** On both
   Colombia Washed roasts (5a32334c, d251013e), gpt-4o escalated fan to 100%
   starting from ~186 °C bean temp. The roasts dropped at 190–191 °C with DTR
   12.6–12.9%, below the per-origin target of 17–18% for washed high-grown (D59
   priors). This could be:
   - the profile's `target_development_percent = 13%` being too low for Colombia
     Washed (per D59 washed high-grown priors, 17–18% is better);
   - or the model over-weighting the fan lever to slow RoR instead of modulating
     heat more precisely.
   The 21 Jun bake-off scored against Artisan roasts from various origins, most
   likely with DTR 12–20% — so the heat-MAE ≈7.5pp and heat-dir ≈0.78 averages
   may mask this per-origin gap.

7. **FC source matters.** Roast 5's FC was operator-marked (no MCP bean_temp_c
   in the event). The bake-off always uses MCP-detected FC; an operator-marked FC
   may fire slightly differently in terms of the DTR clock start. No immediate
   concern but worth tracking.

---

## Converter fix (#224): store schema v6 compatibility

The real operator store is at schema v6 (missing the `roasted_weight_grams` column,
which was added in schema v7 in a migration not yet applied to the live store). The
`store_to_fixture.py` script queried this column unconditionally, crashing on the
real store.

**Fix applied:** `scripts/store_to_fixture.py` now checks `PRAGMA table_info(roast_runs)`
before building the SELECT; if the column is absent, it substitutes `NULL AS
roasted_weight_grams`. This is safe because `roasted_weight_grams` is optional (no
operator weighed these roasts), and the column is purely a corpus label, not a
required field. Tests pass; ruff+pyright clean.

**Action for operator:** apply the schema v7 migration to the live store before
the next roast to unlock weight-loss % tracking. Command:
```
sqlite3 ~/roasts/roastpilot.sqlite3 "ALTER TABLE roast_runs ADD COLUMN roasted_weight_grams REAL CHECK (roasted_weight_grams IS NULL OR roasted_weight_grams > 0);"
```

---

## Whether the gpt-4o pin still holds

**Provisionally yes, with caveats.** Nothing in the store trace suggests gpt-4o
is failing in a way the Artisan bake-off did not predict. The never-drop concern
from the screen-pass (which rejected several models) does not apply here. The heat
trajectory is generally correct.

**The main gap is the per-origin profile/DTR mismatch, not the model.** Colombia
Washed roasts 5 and 6 under-developed (DTR 12.6–12.9% vs the 17–18% per-origin prior
for washed high-grown). This is attributable to the profile's 13% target being
wrong for this bean, not to the model behaving incorrectly given its inputs. The
model is reasoning from the profile; if the profile target is calibrated for a
different origin or an average, the advice will be calibrated accordingly.

**The roast-3 over-shoot is a controller issue (#313/#325), not a model failure.**
gpt-4o called drop correctly; the dev guard blocked it past the ceiling.

### Recommended re-run scope

A scored bake-off run with the expanded corpus is warranted but not blocking:

- Add `store-roast-5a32334c` **and `store-roast-d251013e`** to the bake-off test set once
  the operator confirms them as known-good (the manifest marks both
  `usable_for_bakeoff: true` as of 11 Aug). `d251013e` became eligible when it was rated;
  read its operator note ("a bit flat, slightly underdeveloped") alongside its 12.9 % DTR.
- ~~Rate `store-roast-d251013e` to unlock it (operator action required).~~ **DONE** — rated
  3/5 on 11 Aug ("tasted a bit flat. Sightly underdeveloped"); the entry is now labelled and
  bake-off eligible.
- When running the next bake-off, add a per-origin filter: score Colombia Washed
  fixtures separately and check whether the fan-escalation pattern lands in a
  worse heat-direction class than the aggregate.
- The 21 Jun scorecard's 17-roast set are all Artisan mediums; the store adds a
  first agent-roasted ground truth pair, broadening the coverage.

**Do not re-pin a model based on this observational report alone.** The next scored
bake-off should include both the artisan-22 set and the store roasts; the pin
decision belongs to the operator after reading the full scored output.

---

## Data access flags

- `~/roasts/roastpilot.sqlite3` — ACCESSIBLE, schema v6 (NOT v7).
- `.artisan-fixtures/` — ACCESSIBLE (28 entries present locally).
- `~/Library/Mobile Documents/com~apple~CloudDocs/roasting/*.alog` — ACCESSIBLE
  (47 .alog files found). Not re-converted for this PR (the 21 Jun artisan-testset-
  manifest already covers the known-good mediums from these; re-running alog_to_fixture
  would regenerate the same gitignored fixtures).
- `OPENROUTER_API_KEY` — **NOT SET in this environment.** Live bake-off model calls
  could not be made. All analysis is observational (reading the store's advisory event
  log). To run a scored bake-off, set the key and use the runbook in
  `docs/advisor/bakeoff-runbook.md`.

---

## Decisions surfaced for operator

1. **Profile DTR target for Colombia Washed (D59 follow-on):** the 13% target
   appears to cause under-development on washed high-grown. Increase
   `target_development_percent` for Colombia Washed to 17–18% per the per-origin
   priors (memory `per-origin-dtr-washed-highgrown.md`, D59).

2. **Drop guard vs bean-temp ceiling (#313/#325):** the dev guard blocked gpt-4o's
   correct drop call on roast 3, driving the roast to 203 °C and a bitter outcome.
   Consider: (a) bean-temp ceiling overrides dev guard (always drop if bean ≥ bitter
   ceiling), or (b) lower the 3% dev margin. **Controller change — operator decision.**

3. ~~**Rate `store-roast-d251013e`** (the 27 Jun Colombia Washed run) to complete its
   corpus label. Until rated it cannot serve as a bake-off or D42 reference.~~ **DONE** —
   rated 3/5 on 11 Aug ("tasted a bit flat. Sightly underdeveloped"); it is now a labelled
   bake-off-eligible entry. *(This is the second copy of the same action item; the first is
   at the end of the recommendations section above.)*

4. **Schema v7 migration on live store** before the next roast (see command above).

5. **Next bake-off: include store-roast-5a32334c** as a new test-set entry (first
   agent-recorded reference roast in the scored set).
