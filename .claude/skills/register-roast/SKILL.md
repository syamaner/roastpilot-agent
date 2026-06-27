---
name: register-roast
description: Post-roast data closeout in one step — capture the operator's rating (the D42 label) and register the completed roast as a labelled replay fixture (store_to_fixture). Use after a roast to "register the roast", "rate and save the roast", or feed it to the corpus.
---

The closeout the `roast-review` skill prompts: turn a finished roast into a
rated, labelled corpus/eval example (D42 / D44). Run for the latest completed
run unless given an id.

## 1. Resolve the run

!`curl -sf -m5 http://127.0.0.1:8000/api/roasts | python3 -c "import json,sys; r=sorted(json.load(sys.stdin).get('runs',[]),key=lambda x:x.get('started_at_utc',''),reverse=True); print(r[0]['id'], r[0].get('outcome'), r[0].get('started_at_utc','')[:19]) if r else 'no runs'"`

Use that `<RID>` (a completed run). Pick a short `<slug>` for the bean (e.g. `colombia-huila-roast5`).

## 2. Rate it (the D42 label)

Capture the operator's **score** (e.g. 1–5) + **notes** (taste, what to change).
Persist it on `roast_runs` (`operator_rating` / `operator_notes`):
- If the server exposes a rating endpoint, POST it; otherwise have the operator
  set it in the UI (the **RoastRating** widget on the roast detail / history page)
  and confirm it saved. The rating is the corpus LABEL — do not skip it.

## 3. Register the fixture (D44)

Emit the labelled replay fixture (the format the bake-off / eval consume):

!`echo 'python scripts/store_to_fixture.py "$ROASTPILOT_DB" --out-dir ~/roasts/fixtures/<slug> --run-id <RID>'`

Run it with the real `<RID>` + `<slug>` (`$ROASTPILOT_DB` defaults to
`~/roasts/roastpilot.sqlite3`). It writes `roast.jsonl` + a labelled
`summary.json` carrying the **degree** classification (`core_medium ≤195 /
soft_medium (195,197] / over >197`) and the `operator_rating` / `operator_notes`.

## 4. Confirm

- The fixture parses and carries the label fields:
  !`echo 'python -c "from scripts.bakeoff_replay import load_roast; e=load_roast(\"<out-dir>\"); print(e)"  # spot-check it loads with the label'`
- Report the fixture path. **It is gitignored — real stores / fixtures are never
  committed** (privacy invariant); only aggregates + anonymised ids go in the repo.
- Note: validate store→fixture conversions on a REAL store, not synthetic (the
  telemetry/event clock mismatch silently reads the wrong row otherwise).
