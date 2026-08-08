# Agent Topology Evaluation Log

Evidence for `docs/agent-topology.md` §15, which requires the topology to be
evaluated on five representative cases before it becomes the validated default
(the §16 evaluation-evidence criterion). One entry per real story; no synthetic
cases. Measurements come from tool results and session transcripts, not
impressions.

Archetypes still needed after the entries below: a simple single-slice task that
should bypass the planner, a cross-repository change, and a previously failed or
heavily reworked task.

---

## Case 1 — RP-B (#709), the ambient-aware fan doctrine

Date: 8 Aug 2026. PR: #731. Plan decisions produced: D126, D127.
Archetypes covered: **ambiguous multi-slice feature** and **safety-sensitive
design**, simultaneously.

### Was the planner invoked only when the §6 triggers applied?

**No planner was invoked, and that was the right call**, though it is worth
recording that the §6 triggers nominally fired: multiple dependent PR slices, a
privilege/safety boundary on `AdvisorContext`, and extensive decision history to
reconcile (#707/#709/#711/#498/#342/#726/#705/#580 plus D122/D124/D125).

The §6 *SHOULD NOT* conditions bit harder. The design was already authoritative
and complete: ratified on 6 Aug, with the c11 design posted on #709 on 7 Aug and
the D125 eval correction restated at kickoff. A planner would have restated an
approved brief, which §6 names explicitly. The residual design work — how the
offline eval path would carry ambient — took exactly the "handful of repository
reads" §6 gives as the skip criterion (five tool calls).

**Finding for §6:** the trigger list and the skip list can both fire on the same
task, and when they do, the skip list should win where the *design* is settled
and only *mechanism* is open. The current text does not say which dominates. A
one-line precedence rule would remove the judgement call.

### Did plans need material rewrite?

Not applicable (no planner). But the **PM-authored plan needed material
correction three times**, all caught by review rather than by planning:

1. The ratified design said the doctrine's threshold "comes from the
   `ambient_temp_c` context field". That field is the *measurement*; no boundary
   existed anywhere, so the doctrine as specified had no decision boundary at
   all. Caught by the PM at implementation time, escalated to the operator (D126).
2. The PM silently dropped D124's ratified ~15 pp step bound when it collided
   with the section's no-digits test guard. Caught by the local Codex pass.
3. The eval plan (c3 vs c11) contradicted D122's ratified single-variable rule,
   because c11 inherits seven unpromoted teachings. Caught by Codex post-open,
   became D127.

**The pattern is the finding.** Each of these lived in the *join* between two
individually-correct artifacts: construction vs eval plan, ratified number vs
test guard, context field vs comparison boundary. None was visible from either
side alone. This is the strongest argument the evaluation has produced *for*
invoking the planner on the next comparable story: a read-only pass whose whole
job is reconciling artifacts is aimed exactly at join defects, and the PM
skipped it on the reasonable-but-wrong basis that the design was settled.

**Revised guidance for the next case:** treat "the design is settled" as
insufficient grounds to skip the planner when the story spans a *construction*
and an *evaluation protocol* that were ratified separately.

### Requirements or boundaries missed

One, by the PM, and it was material: the doctrine's context fields were
populated on **every** roast, including the live default `c3` which never
teaches them. The PR claimed "no behaviour change for c3"; that held for the
control path (the safety reviewer traced it) but not at the advisor's input,
where a real room temperature and a named fan-step bound were being serialised
into an untaught prompt — contaminating the very c3 baseline RP-B is measured
against. Caught post-open by Codex; fixed by adopting #567's inert-by-default
posture.

Notably #567 had already reasoned about this exact question and recorded the
answer. The PM did not find that precedent while designing, only while
responding to the finding.

### Preventable rework vs healthy review catches

- **Healthy (found before the PR opened, folded into the first push):** 6
  safety findings, 1 qa test-design improvement, 2 local Codex findings. None
  of these became post-open rework, which is the shift-left target working.
- **Preventable (post-open, should have been caught earlier):** the c3
  contamination and the stale `15.0` doc comment. The doc comment is the
  sharper miss: the PR violated the two-copies discipline it repeatedly
  invokes as its own design principle.
- **Avoidable churn:** one self-inflicted CI failure. Editing the PR body while
  a review run was in flight cancelled the legitimate `synchronize` run and
  started an `edited` run that fails by design, leaving the head with no valid
  approval until the cancelled run was re-run. **Operational rule earned: do
  not edit PR metadata while a review run is in flight.**

### Tokens and cost per model tier

Measured from this session's subagent transcripts. Cache reads dominate raw
input and are cheap, so the meaningful columns are cache-write and output.

| Agent | Model | Cache write | Output | Messages |
|---|---|---:|---:|---:|
| `qa-b1` | `claude-sonnet-5` | 919,691 | 46,915 | 190 |
| `safety-b1` | `claude-opus-5` | 358,238 | 40,379 | 92 |

**The pin produced the intended cost shape.** The Opus reviewer consumed ~39% of
the Sonnet reviewer's cache-write volume while carrying the harder judgement;
the Sonnet agent absorbed the high-volume mechanical work (repeated pytest runs,
coverage runs, independent mutation testing). This is the §4 allocation
behaving as designed, and it is evidence for keeping `safety-reviewer` on Opus
rather than "saving" it.

### Unnecessary agent spawns

None. Two subagents for a safety-sensitive cross-boundary change, both required
lenses under the Code Review Rubric, well inside the §10 cap of three. No agent
was spawned to re-check work the active model already verified.

### Unauthorized mutations by a planning or review role

**One, and it is the most important governance finding here.** The `qa` agent
ran `git checkout origin/main -- .` in the shared checkout, overwriting the
entire working tree. It self-reported immediately, saved a patch of the
uncommitted diff, and stopped writing; the PM verified independently that
nothing was lost (both commits intact, folded changes present in the committed
blobs).

This is precisely the gap §7 documents and accepts: read-only roles are
read-only **by tool list**, and `Bash` is not covered. The spec says the
operational control is not running such roles under permissive parent modes.
This case shows the residual risk is not theoretical — the destructive command
was a *reasonable-looking* verification step (checking whether a failure was
pre-existing on `main`), not misuse.

**Recommendation:** §7's operational control is necessary but not sufficient.
Read-only reviewers should be given an explicit instruction to verify against a
separate worktree rather than the shared checkout, and the runbook should say
so. Filed as #733.

### Contradictions introduced between plan, prompt, and repository

Three found, all resolved and recorded rather than left latent:

- Plan vs repo: D124's "threshold comes from `ambient_temp_c`" was not
  implementable as written (D126).
- Plan vs hardware: D124's ~15 pp step is not representable on a 0-10 integer
  fan scale quantised by `(value + 5) // 10` (D126).
- Plan vs plan: D124's construction and D122's single-variable rule were jointly
  unsatisfiable under the briefed c3 baseline (D127).

The topology's own artifacts stayed consistent: no agent definition, model pin,
or `AGENTS.md` prose was touched, so `tests/test_agent_model_pins.py` was
unaffected throughout.

### PM output style

Not evaluated. The `RoastPilot Operator` output style was not selected for this
session, so the §15 style measurements (handoff consistency, whether blockers
stay visible, Default-vs-Operator token deltas) remain outstanding. Recording
this explicitly so a later reader does not mistake this entry for style
evidence.

### Net assessment

The topology performed well where it was exercised: model pins produced the
intended cost split, both review lenses found real defects the other missed
(the safety lens found the brake-rationing and fail-soft-softening defects; the
Codex lens found the two that changed ratified decisions), and independent
review caught every one of the PM's own misses.

Its weakest point in this case was **planning**, which is the part that was
skipped. Every defect that reached the PR came from a join between artifacts,
which is the failure mode the read-only planner exists to address.
