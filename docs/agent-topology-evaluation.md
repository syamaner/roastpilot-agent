# Agent Topology Evaluation Log

Evidence for `docs/agent-topology.md` §15, which requires the topology to be
evaluated across five representative **archetypes** before it becomes the
validated default (the §16 evaluation-evidence criterion). One entry per real
story; no synthetic cases. Measurements come from tool results and session
transcripts, not impressions.

Every D-number in this log is defined in `roastpilot-plan/roastpilot-agent/plan.md`
unless stated otherwise. The qualifier matters because `roastpilot-cloud/factory.md`
independently defines the same D103-D129 range for unrelated decisions, so a bare
number in that range resolves two ways; see `docs/state/registry.md`.

The requirement is archetype COVERAGE, not a case count, and a single story may
cover more than one — Case 1 below covers two. Stated explicitly because the two
readings diverge: counting entries, five archetypes across four stories would
look like the bar was cleared a case early. What has to be true is that every
archetype has been exercised by a real story, not that five rows exist.

Archetypes still needed after the entries below: a simple single-slice task that
should bypass the planner, a cross-repository change, and a previously failed or
heavily reworked task.

**Plus a re-validation of the two RP-B already touches.** Case 1 exercises the
ambiguous-multi-slice and safety-sensitive archetypes but is FAILURE evidence —
no planner ran on it, so it tests neither the trigger-wins rule §6 now carries
nor the Fable planner itself. Those two are **provisional, not cleared**: a later
story must exercise them UNDER the revised rule. Listing only the three above
would let §16 signal readiness after three more stories without Fable ever having
been run on an ambiguous or safety-sensitive case.

---

## Case 1 — RP-B (#709), the ambient-aware fan doctrine

Date: 8 Aug 2026. PR: **#731** (the doctrine). Plan decisions produced:
**D126, D127** (`roastpilot-agent/plan.md` — see the registry on why a bare
D-number is ambiguous across plan files).

**Scope: this entry measures PR #731 ONLY**, and the boundary is deliberate. RP-B
also shipped #739 (the eval set, D129) and #741 (ambient freshness, D128), and an
earlier revision widened this entry to cover all three. That was a mistake worth
recording, because it is a measurement lesson rather than a clerical one: widening
the declared scope silently obliges every count below it — rework, spawns, review
lenses, decisions — to be re-derived over the wider set, and four consecutive
review rounds each found another place where the wider claim implied content that
was not there. **A case entry should cover the unit whose evidence was actually
gathered.** #741 in particular deserves its own entry rather than being folded in:
six post-open rounds, three of them fixing defects introduced by its own previous
fixes, and the first use of an operator stopping rule.

**Measurement granularity, stated once so §16 can consume this entry safely.**
Not every number here shares the same unit, and the mismatch is a property of the
telemetry rather than a loose end:

| Measurement | Unit | Why |
|---|---|---|
| Preventable/healthy rework | **#731** | Derived from that PR's own merged history |
| Agent spawns | **#731** (three) | Attributable per PR from the roster |
| Plan corrections, missed boundaries | **#731** | Traceable to specific findings |
| **Tokens and cost** | **The whole 8-9 Aug session** | Transcripts are session-scoped and carry no per-PR attribution, so these figures also price #739 and #741 work |

Splitting the cost figures per PR is not possible from the available data, and
inventing an apportionment would be worse than declaring the boundary. §16 should
therefore read the rework and spawn results as #731 evidence and the tier-cost
comparison as session evidence. The two review lenses disagreed about which unit
this entry should use — one proposed narrowing to #731 and then argued the
opposite once it was narrowed — and the operator settled it: **hold the narrowing,
state the limitation.**

One measurement is deliberately carried across the boundary, because suppressing
it would flatter the case: **#741 merged without the `qa` pass its diff mandated**
(837 test lines against AGENTS.md's 600-line threshold). It had its mandatory
pre-open `safety-reviewer` pass, but not the test-quality lens. Not opened as an
issue — the tests were mutation-verified in-session and CI plus codecov were green
— but recorded here so the story is not read as fully compliant on #731's reviews
alone.

> **Reading this later: `docs/state/registry.md` lagged this entry.** AGENTS.md
> sends every session to the registry first, and at the time this case was
> written it still read "RP-B is next" with "next free plan decision number:
> **D126**" — while D126 and D127 were already spent. That is not a hypothetical
> drift: a cold-start session on 9 Aug followed the documented kickoff order,
> read the registry, and was handed a spent decision number; it only avoided
> reusing D126 because #709's comments corrected it. Recorded here as evidence
> rather than silently fixed, because the registry going stale mid-story is
> itself a finding about the handoff, and the same gap will recur on the next
> multi-session story unless the registry is updated as decisions are consumed
> rather than at story end.

Archetypes covered: **ambiguous multi-slice feature** and **safety-sensitive
design**, simultaneously.

### Was the planner invoked only when the §6 triggers applied?

**No planner was invoked. That followed §6 as currently written, and the
outcome shows §6 is wrong** — those are the two halves of one finding, and this
entry is evidence for changing the rule rather than for the decision it
produced. A later reader resolving the §6 trigger/skip conflict should take the
revised guidance below, not the skip. The §6 triggers nominally fired: multiple dependent PR slices, a
privilege/safety boundary on `AdvisorContext`, and extensive decision history to
reconcile (#707/#709/#711/#498/#342/#726/#705/#580 plus D122/D124/D125, all
`roastpilot-agent/plan.md`).

The §6 *SHOULD NOT* conditions bit harder. The design was already authoritative
and complete: ratified on 6 Aug, with the c11 design posted on #709 on 7 Aug and
the D125 eval correction restated at kickoff. A planner would have restated an
approved brief, which §6 names explicitly. The residual design work — how the
offline eval path would carry ambient — took exactly the "handful of repository
reads" §6 gives as the skip criterion (five tool calls).

**Finding for §6:** the trigger list and the skip list can both fire on the same
task, and the text as written does not say which dominates — which is the defect,
because it leaves the call to whoever is reading. This case supplies the answer:
**the trigger list has to win.** A settled design is exactly what the skip list
was reaching for here, and it still produced three plan corrections, because a
settled design says nothing about whether the separately-ratified artifacts agree
with each other. The precedence rule added to §6 in this PR is that answer written
down.

### Did plans need material rewrite?

Not applicable (no planner). But the **PM-authored plan needed material
correction three times** — **two** caught by review, **one** self-caught at
implementation time (item 1 below). The distinction matters and an earlier
revision blurred it by calling all three review catches: that inflates the
review-catch count, and it makes the later "independent review caught every one
of the PM's own misses" claim untrue as stated. What the evidence supports is
narrower and still useful: *no* plan defect survived to merge, and the two that
the PM did not notice were both caught by the Codex lens.

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
skipped it on grounds §6 explicitly sanctions — which is the point. The rule,
not the judgement applying it, is what failed here.

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
  Two of the six are the safety findings the cost section below cites by name:
  graduated fan-brake steps that would have rationed c3's own heat-at-floor
  emergency brake (PR #731 pre-open findings table, finding 1 — "Graduated
  steps rationed the brake in the heat-at-floor emergency c3 exists to
  handle", fixed with an explicit precedence carve-out), and an absent-ambient
  fallback that defaulted the step bound *soft* — silently weakening the
  doctrine in a hot room whenever the ambient reading was missing, the
  opposite of fail-safe (PR #731 finding 2 — "Absent-ambient fallback
  defaulted *soft*, so a fail-soft probe softened doctrine in a hot room",
  fixed by removing the soft default and pinning a regression test). Full
  findings table: PR #731 body.
- **Preventable (post-open, should have been caught earlier): four**, where an
  earlier revision of this line reported two. Understating this understated the
  case's headline measurement. From #731's merged history:
  1. the **c3 contamination** (Codex P1) — the doctrine's context was populated
     on every roast including the live default, so any c3 baseline the RP-B
     comparison is measured against was contaminated;
  2. **`pr-triage` returned NOT-MERGEABLE** on a blocker: `step_max_pp` was
     `le=100.0`, so the whole-multiple rule alone accepted a 100 pp *ordinary*
     step — a full floor-to-ceiling fan slam, precisely what the doctrine
     exists to prevent, moved out of the prose and into the config. The same
     pass caught three claimed-but-not-delivered edits by diffing the commit
     message against the file;
  3. a later **Claude round on the guard for that blocker**: the ceiling test
     read the field's constraint metadata but asserted against a hardcoded
     `20.0`, so a partial loosening to 30.0 would have passed. The test for the
     blocking issue did not actually guard the declared constraint;
  4. the stale **`15.0` doc comment**.

  Items 2 and 3 are the sharper misses, and they compound: a blocker reached
  post-open, and then its guard did too. Item 4 is the neatest, though — the PR
  violated the two-copies discipline it repeatedly invokes as its own design
  principle.
- **Avoidable churn:** one self-inflicted CI failure — editing the PR body while
  a review run was in flight. **The mechanism, corrected after review, is
  cancellation and not a doomed run.** `claude-code-review.yml` subscribes to
  `edited` and its job condition excludes only Dependabot, so the replacement is
  a valid same-identity review; what bites is `concurrency` with
  `cancel-in-progress: true`, scoped per PR, which kills the in-flight
  `synchronize` run. The head is then briefly without valid approval until the
  replacement finishes. An earlier revision of this line said the `edited` run
  "fails by design", conflating this with the genuine by-design failure on PRs
  that edit a workflow file. **Operational rule, restated accurately: do not edit
  PR metadata while a review run is in flight — not because the new run is
  invalid, but because it discards a run that was already most of the way
  through.**

### Tokens and cost per model tier

Measured from this session's transcripts, **deduplicated on `message.id`** — the
transcripts repeat each assistant message roughly twice, so a straight sum
inflates every figure by about 2×. Snapshot near session end; the session was
still running, so treat these as magnitudes, not a final ledger.

Costs are **list-price references**, not a bill: this is a subscription account.
Rates used are Opus 5 $5/$25 per MTok, Sonnet 5 $2/$10 (the intro rate current
until 31 Aug 2026), cache write 1.25× input, cache read 0.1× input.

| Agent | Tier | Cost | Msgs | Cache read | Output |
|---|---|---:|---:|---:|---:|
| PM main loop | opus | **$86.21** | 497 | 141,979,495 | 411,303 |
| `eng-732` | sonnet | $13.09 | 221 | 52,853,889 | 11,918 |
| `safety-b1` | opus | $2.90 | 43 | 3,824,664 | 6,258 |
| `qa-b1` | sonnet | $2.67 | 83 | 9,435,244 | 2,925 |
| `triage-731` | sonnet | $0.52 | 17 | 1,231,056 | 842 |
| `triage-739` | sonnet | $0.36 | 14 | 818,550 | 636 |

**Finding 1 — the orchestrating loop is the cost, not the reviewer tier.** The PM
main loop is **81.5%** of the $105.75 total. Any cost analysis of this topology
that omits it is not an analysis of the topology.

**Finding 2 — within that loop, re-reading context outweighs generating it about
7:1.** Cache reads cost $70.99 against $10.28 of output. The lever on a session
like this is its **length and delegation depth**, both of which the topology
controls directly, and neither of which is the model a reviewer runs on.

**Finding 3 — the delegation pin does show a defensible saving, but not the one
originally claimed here.** Work delegated to Sonnet totalled **$16.64**; pricing
that *same observed token volume* at Opus rates gives ~$41.60, so the delegated
work ran at roughly **40%** of its lead-tier cost. This is a valid counterfactual
because it prices identical work at two rates. It is still an approximation: a
different tier might have used a different volume to do the same job, which this
does not model.

**What this section deliberately does NOT claim.** An earlier draft argued that
the Opus reviewer consumed ~39% of the Sonnet reviewer's cache-write volume and
called that evidence for the `safety-reviewer` pin. That argument was wrong twice
over, and both errors are worth recording because they are easy to repeat:

1. **Cache-write volume is not cost.** Converted to cost, the two reviewers land
   at 1.09× — a dead heat — because the Opus tokens cost ~2.5× more, which
   cancels the volume gap the claim rested on. The direction also inverts once
   Sonnet's intro pricing lapses. A conclusion that flips sign on a pricing date
   cannot support a durable pin.
2. **The comparison was invalid regardless of units.** `safety-b1` (43 messages)
   and `qa-b1` (83 messages) are different roles doing different amounts of
   different work. Comparing their totals measures the two TASKS, not the two
   tiers. Replacing a wrong tier-cost claim with a tidy "cost-neutral" one would
   repeat the same error with a friendlier number.

**So the `safety-reviewer` Opus pin stands on VALUE, with no cost claim attached
in either direction.** The Opus lens found the brake-rationing gap and the
fail-soft defect named under "Preventable rework vs healthy review catches"
above (PR #731 findings 1 and 2), and at $2.90 the pin is close enough to free
that cost is simply not the axis worth arguing about.

**How both errors were caught: a second party recomputed the numbers from
source.** Not by reviewing the prose — the surrounding argument was coherent, the
magnitudes plausible, the units apparently right. This is a cheap, concrete
control the topology can adopt: **any number heading into a decision record or a
doc gets recomputed by someone who did not derive it.** It ran in both directions
here, catching an error on each side.

**What §15's three measurement axes actually got, stated per axis** — because a
detailed table on one axis reads as coverage of all three, and this section had
claimed more than its own data supports:

| §15 axis | Status | Why |
|---|---|---|
| Model cost | **Measured** (list-price reference, not a bill) | Per-agent, recomputed from source by a second party |
| Total tokens | **Partial — not a complete ledger** | The table carries cache-read and output only; **cache-write and uncached-input columns are absent**, and the snapshot was taken while the session was still running |
| Latency | **Not evaluated** | The transcripts carry message counts and token volumes but no per-call wall-clock timestamps, so no figure could be recomputed from source |

The cost column is the more trustworthy of the two numeric axes because it was
derived per agent and independently recomputed; the token figures are magnitudes
for comparing tiers against each other, and should not be quoted as this case's
total token consumption. A future case wanting to satisfy the total-token
criterion properly needs a final end-of-session snapshot with every input
category, not a mid-flight one.

### Unnecessary agent spawns

None — and the in-scope count is **three**, where an earlier draft of this
section said two and a later one said four. Both were wrong, in opposite
directions, and the second is the more instructive: after narrowing this entry to
#731 I reconciled every textual `#739`/`#741` mention but never re-audited the
AGENT ROSTER, where the scope is buried in a name. **`eng-732` is named for issue
#732, which PR #741 closes** — it implemented the freshness slice, not #731. The
usage table above spans the whole session and therefore prices #739 and #741 work
too; it is retained for cost completeness, not as this case's inventory.

**Five spawned across the session, three in this entry's #731 scope:**

| Agent | Why it was spawned | Redundant? |
|---|---|---|
| `eng-732` | Implementer for the freshness slice | No — but **out of this entry's scope**: that slice is #741, not #731 |
| `safety-b1` | Required lens (controller/safety diff) | No — §10 names it a control layer |
| `qa-b1` | Required lens (test quality) | No — same |
| `triage-731` | Author-independent triage of PR #731 | No — D23 forbids self-triage |
| `triage-739` | Author-independent triage of PR #739 | No — same, but **out of this entry's scope** (sibling PR; included in the cost table only) |

§10 rules out subagents spawned "merely to re-check work the active model already
verifies", and names independent safety, QA and author-independent triage as
deliberate control layers rather than redundancy; each of the three in-scope
spawns is one of those. Note also that **§10's "three" is a CONCURRENCY cap, not a
budget for total spawns** — the earlier draft cited it against a total, which
would have read as a ceiling this case was near when in fact it does not bound
totals at all.

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
so.

**Corrected after review (the recommendation above misdiagnosed the gap).**
`docs/agent-team-worktrees.md:83-98` ALREADY records a 9 Jul reviewer
`git checkout --` incident and already forbids reviewers from running
tree-mutating git commands. The control exists; it was never routed to the
Bash-capable roles that keep re-entering it — `.claude/agents/qa.md` contains
nothing about worktrees, checkout, or mutation. So the actionable gap is
DELIVERY into the agent definitions, not another runbook note. **A re-scope to
that effect is proposed in a comment on #733** (route the snapshot-by-`cp` and
worktree/basetemp controls into the agent definitions for `qa`,
`safety-reviewer`, `pr-triage`, and the engineer roles); as of this writing
#733's own title and body are unchanged — the re-scope exists as a comment,
not yet as an edit to the issue itself.

Two further data points arrived the same night on the sibling PR #741. They sit
outside this entry's measured scope and are recorded here anyway, because the
control they bear on is repo-wide rather than per-case, and both show it failing
in ways a prohibition alone cannot fix.
First, the concurrent-pytest basetemp collision produced a failure that a
reviewer correctly escalated as a P1 flake; the cost of that class is not the
phantom failure but the real investigation it justifies each time. Second, the
same `git checkout --` destruction recurred — this time self-inflicted by the
PM, as the restore step of a mutation-testing cycle, silently discarding a
review-requested edit whose commit message then claimed it. That is the
sharpest form of the finding: **mutation testing REQUIRES a restore step, and
`git checkout --` is the obvious way to write one**, so a prohibition with no
stated alternative will keep being re-entered. The alternative that belongs in
the agent definitions is snapshot-and-restore by file copy, plus verifying
record-level claims against the committed tree (`git show HEAD:path`) rather
than the working tree.

### Contradictions introduced between plan, prompt, and repository

Three found, all resolved and recorded rather than left latent. Every D-number in
this list is `roastpilot-agent/plan.md`'s:

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

The topology performed well where it was exercised: delegation moved real volume
to Sonnet at roughly 40% of its lead-tier list cost, both review lenses found
real defects the other missed (the safety lens found the two defects named
under "Preventable rework vs healthy review catches" above; the Codex lens
found the two that changed ratified decisions), and of the PM's three plan
defects **two were caught by independent review and one the PM caught itself** at
implementation time — plus, twice, a number the PM had asserted without
recomputing, which recomputation caught. An earlier revision of this sentence
said review caught every PM miss; that contradicted the corrected tally above and
overstated the independent-review result.

Its weakest point in this case was **planning**, which is the part that was
skipped. Every defect that reached the PR came from a join between artifacts,
which is the failure mode the read-only planner exists to address.

The cheapest control this case surfaced is not a model pin at all: **have a
second party recompute any number before it becomes durable.** Two wrong figures
survived ordinary review here — one in this document — because a wrong number
reads exactly like a right one. Recomputation caught both; prose review caught
neither.
