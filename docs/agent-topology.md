# Claude Agent Topology Specification

Status: Accepted; implemented for the subagent fleet; §15 evaluation still
pending (revised 9 Aug 2026).
Slice 1 (the read-only planning-architect and the operator output style) and
Slice 2 (the ten existing roles re-pinned from `sonnet`/`opus` aliases to full
model IDs with explicit effort, plus the matching `AGENTS.md` model-selection
guidance) have both landed.
Slice 2 updated each existing agent's `model` pin and
`effort`, and the corresponding model-selection guidance in `AGENTS.md` (so the
prose and the pins agree rather than contradict); it preserves their current
tools and capabilities, except that `product-pm` was narrowed to the read-only
`product-auditor` (#802 — its documentation-write scope moved to the lead, and
its brief-writing role was superseded by the D152 `story-planner` contract), and
`ui-reviewer` keeps its Playwright MCP access. The §4 reference table describes generic
role archetypes, not a tool-set remap of the existing fleet. Operator note:
`claude-opus-5` and `claude-fable-5` availability was confirmed for this
environment before Slice 1 shipped, so the planning-architect's Fable pin
resolves; the Slice-2 model-availability gate covers the fleet re-pin
validation. Exact model IDs and explicit effort are set for every named-subagent
role, `AGENTS.md` agrees, the inline workflow stages in `review-branch.mjs` are
pinned, and a consistency test (`tests/test_agent_model_pins.py`) guards the pins
against drift and against committed settings that would defeat them (the
repository side of the §5 override surface). The §7 worktree and copy-restore
rules are now delivered by #733 into all eleven Bash-capable role definitions
and guarded for presence. One §16 adoption criterion remains outstanding: the
§15 evaluation across all five archetypes — not a five-case count, since a
single story may cover more than one. RP-B (#709) supplies
evidence on two (ambiguous multi-slice, safety-sensitive), but **as a FAILURE
case, which does not yet validate them**: no planner was invoked on it, so it
tests neither the trigger-wins rule added to §6 above nor the Fable planner's
model and contract on either archetype. Counting those two as validated would
let the topology be marked ready on the strength of an entry that records the
rule being broken. They need a subsequent applicable story run UNDER the
revised rule; three archetypes remain unexercised regardless
(§15; the §16 evaluation-evidence criterion). The formerly-third item, the
Product PM main-session pin, was resolved by operator decision on 9 Aug 2026:
the main-session model is chosen deliberately per fresh terminal (an operator
setting, `model: claude-opus-5` or `--model`; `default` remains an alias, not a
pin), and the guard-tested subagent pins are the reproducibility mechanism, so
the fleet's model assignments do not depend on the main-loop choice (§5). The
live-environment and organisation side of the §5 override
surface (`CLAUDE_CODE_SUBAGENT_MODEL`, `availableModels`, organisation
restrictions) is likewise a standing operator check. The topology is in effect
for the subagent fleet but not yet the validated default.  
Audience: Repository owners, Claude PM/orchestrator sessions, agent authors  
Scope: Claude Code model selection, planning, delegation, review, and authority boundaries

## 1. Purpose

This specification defines a cost-aware Claude agent topology in which:

- Claude Opus 5 owns product orchestration, scope, authority, and integration
  (recommended model for the role; the main-session model itself is a
  per-terminal operator decision — see §5).
- Claude Fable 5 is invoked selectively as a read-only planning specialist —
  and, as the `story-planner` contract writer, on every story delegated to
  Codex-MCP (D152, §6).
- Implementation defaults to Codex-MCP for contracted slices (D152, §4/§10);
  Claude Sonnet 5 performs fallback and uncontracted implementation and the
  routine review work.
- High-consequence safety or architecture review remains an independent Opus 5 responsibility.

The design uses Fable where long-context planning and ambiguity resolution provide material value without making it the default model for every task.

## 2. Normative language

The terms **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## 3. Core principles

1. **The human owns product authority.** The human approves material scope, architecture, safety, cost, and irreversible decisions.
2. **Opus owns orchestration** (Opus 5 recommended; see §5 for the per-terminal operator decision on the main-session model). The Opus PM maintains intent, chooses the execution primitive, adjudicates plans, assigns work, integrates results, and escalates genuine forks.
3. **Fable advises on plans.** The Fable planner investigates and recommends. It does not implement, mutate repositories, contact external systems, or manage workers.
4. **Workers receive bounded tasks.** Implementation agents receive one coherent responsibility, explicit boundaries, acceptance criteria, and relevant evidence.
5. **Authors do not adjudicate their own work.** Review and triage remain independent from implementation.
6. **Model choice and topology are one decision.** Every delegation names the role, exact model ID, effort, scope, and authority.
7. **Evidence precedes status.** Completion and progress claims MUST be grounded in current tool results.
8. **Prompts request conclusions, not hidden reasoning.** Agents provide evidence, assumptions, decisions, and concise rationales; they MUST NOT be asked to reproduce private chain-of-thought.
9. **Presentation is separate from authority and workflow.** Output styles MAY standardize the main conversation's tone and response shape. Repository policy belongs in project instructions, task procedure belongs in skills, and role-specific contracts belong in agent definitions.

## 4. Reference topology

| Role | Exact model | Default effort | Authority | Default tools |
|---|---|---:|---|---|
| Product PM / orchestrator (main session) | `claude-opus-5` recommended (the main-session model is a deliberate per-terminal operator choice — operator decision, 9 Aug 2026, see §5; set via the `model` setting or `--model claude-opus-5`; `default` is an alias, not a pin) | `high` | Scope, routing, adjudication, integration, escalation | Repository and coordination tools required by the task |
| Planning architect (named subagent) | `claude-fable-5` | `high` | Advisory only | Read, Grep, Glob, Bash (no Edit/Write; read-only by convention, §7) |
| Story planner / contract writer (named subagent) | `claude-fable-5` | `high` | Advisory only — writes the implementation contract required before any Codex-MCP delegation (D152) | Read, Grep, Glob (no shell, no Edit/Write; read-only by construction) |
| Backend/frontend implementer — default (contracted slices) | Codex-MCP (D152 — external family, own subscription; not a Claude pin) | n/a (Codex-side) | Writes only inside assigned scope | Codex's own MCP-side toolset |
| Backend/frontend implementer — Claude fallback | `claude-sonnet-5` (`engineer-be`/`engineer-fe`) | `high` | Writes only inside assigned scope | Read, Grep, Glob, Bash, Edit, Write |
| Mechanical contract/simulation checker | `claude-sonnet-5` | `medium` | Read-only verdict | Read, Grep, Glob, Bash |
| QA/product/security reviewer | `claude-sonnet-5` | `high` | Read-only findings | Read, Grep, Glob, Bash |
| Safety/critical architecture reviewer | `claude-opus-5` | `xhigh` | Read-only findings | Read, Grep, Glob, Bash |
| Independent PR triage | `claude-sonnet-5` | `high` | Adjudicates findings; does not author fixes | Read, Grep, Glob, Bash |

`xhigh` MAY be used for the Fable planner when the task is genuinely long-horizon, cross-repository, safety-sensitive, or architecture-defining. It SHOULD NOT be the default for ordinary planning.

The implementer row's Codex-MCP default (D152) is an external-family harness, not a Claude model pin: the exact-ID discipline of §5 and §16 binds the Claude roles — including the Claude fallback implementer pins — and `tests/test_agent_model_pins.py` asserts exactly those. Codex delegation is bound by D152's contract rule instead.

## 5. Model pinning

1. Agent definitions MUST use full model IDs when reproducibility is intended:
   - `claude-opus-5`
   - `claude-fable-5`
   - `claude-sonnet-5`
   The Opus PM is the main session, not a subagent, so its model is set through
   the `model` setting or `--model claude-opus-5`. Operator decision (9 Aug
   2026): the main-session model is chosen deliberately per fresh terminal
   rather than mandated here — Opus 5 remains the recommended PM model (§1,
   §4), and a deliberately chosen non-Opus main session is compliant; the
   guard-tested subagent pins are the reproducibility mechanism, so the fleet's
   model assignments do not depend on the main-loop choice. `default` remains an alias, not a pin — it resolves to
   Sonnet 5 on Pro, Team Standard, and Enterprise seat accounts, so it is never
   a way to obtain Opus deliberately.
2. Family aliases such as `opus`, `fable`, and `sonnet` MUST be described as aliases, not pins.
3. Before relying on per-agent model selection, the orchestrator MUST account for the whole override surface, in Claude Code's documented precedence order: (1) `CLAUDE_CODE_SUBAGENT_MODEL`, (2) the per-invocation `model` parameter passed with the Agent tool, (3) the agent-frontmatter `model`, then (4) the main conversation's model. Separately, `availableModels` / `enforceAvailableModels`, an organisation default model, or organisation model restrictions can silently substitute a pinned ID for a different permitted version, or fall back to the inherited model. A pin holds only in the absence of all of these.
4. The effective model SHOULD be verified through Claude Code status/telemetry or provider logs when audit-grade proof is required. A model's self-report is not proof.
5. Effort MUST be selected separately from response verbosity. Prompts SHOULD state the desired output length directly.

## 6. When to invoke the Fable planner

This section governs the `planning-architect`. The `story-planner` contract
writer (D152) is a distinct, mandatory step for every story delegated to
Codex-MCP: compiling the implementation contract is not the deep planning this
section rations, so it is exempt from the bypass rules below and does not count
against the one-planning-subagent rule at the end of this section. On a complex
story the two compose in sequence — `planning-architect` (when triggered) plans,
the Opus PM adjudicates, then `story-planner` compiles the adjudicated plan into
the contract.

The Opus PM SHOULD invoke Fable when one or more of these conditions hold:

- The task crosses repositories or major architectural layers.
- Different interpretations would produce materially different implementations.
- The work contains multiple coherent PR slices with dependencies.
- A safety, security, data-governance, or privilege boundary needs design before implementation.
- Three or more independent downstream tracks need a shared plan.
- The task requires reconciling extensive history, decisions, or competing constraints.
- A failed previous approach requires re-planning from evidence.

The Opus PM SHOULD NOT invoke Fable when:

- The task is a small, well-specified, single-slice change.
- Acceptance criteria and design are already authoritative and complete.
- Planning would require only a handful of repository reads.
- The principal work is mechanical execution rather than judgment.
- Fable would merely restate an existing approved brief.

**When both lists fire, the trigger list wins.** The two lists above are not mutually exclusive and a real story can satisfy both at once — an authoritative, complete design whose story nevertheless spans multiple dependent slices, a safety boundary, or artifacts ratified separately from one another. In that case the PM SHOULD invoke Fable. In particular, "the design is settled" **MUST NOT** on its own be sufficient grounds to skip the planner when a story spans a *construction* and an *evaluation protocol* that were ratified independently: a settled design says nothing about whether the artifacts agree with each other, and the join between two individually-correct artifacts is precisely what a read-only reconciliation pass exists to inspect. Evidence: Case 1 in the evaluation log, where all three *plan* defects that reached the PR lived in such a join. The claim is deliberately limited to that class: the same case also produced implementation and test defects (the evaluation log's "Preventable" list), which a pre-implementation read-only planner could not have caught, so they are not evidence for this rule.

Only one planning subagent SHOULD run for a single decision problem. Multiple planners MAY be used only for genuinely independent competing hypotheses, with an explicit cap set before spawning.

## 7. Planning architect contract

**Every read-only role MUST verify against its own `git worktree`, never the shared checkout**, and MUST NOT run tree-mutating git commands (`git checkout --`, `git restore`, `git stash`, `git reset`, `git clean`) in a tree it does not own. Where a role needs to mutate a file and restore it — mutation testing is the normal case — it MUST snapshot and restore **by file copy**, never by git, because `git checkout --` silently discards uncommitted neighbouring edits along with the mutation. A prohibition without that alternative is not an operable control: the restore step is required, so the forbidden command keeps being reinvented. Claims about what a commit contains MUST be verified against the committed tree (`git show HEAD:path`), not the working tree.

This is a requirement on the role definitions themselves, not only on this document: a control recorded in a runbook that no agent definition carries has not been delivered. Evidence, including three occurrences: Case 1's governance finding in the evaluation log.

The Fable planning architect MUST:

- Work read-only. Read-only is enforced by the tool list: the planner has no `Edit` or `Write` tool, the same posture as the repository's other read-only roles (`safety-reviewer`, `qa`, `pr-triage`), and it runs under `permissionMode: plan`. `Bash` is for inspection only (`git log`/`show`/`diff`/`blame`, ripgrep, file reads), never mutation. A hard OS-level guarantee against a determined `Bash` write is out of scope for this posture and would have to apply to every read-only role equally; the operational control is to run read-only roles under `default` or `plan` parent sessions, not `acceptEdits`, `auto`, or `bypassPermissions`. A pattern-matching Bash guard was evaluated and rejected: a denylist is bypassable (command wrappers, substitution, `git -C`) and a strict allowlist is disproportionate for an advisory role.
- Read the authoritative repository instructions, specifications, current state, and relevant history.
- Distinguish confirmed evidence from assumptions and unknowns.
- Preserve decisions already made by the human or authoritative plan.
- Recommend one approach when enough evidence exists.
- Identify materially different interpretations that require human input.
- Produce an ordered, reviewable implementation plan.
- Stop after handing the plan to the Opus PM.

It MUST NOT:

- Edit files, create branches, commit, push, open pull requests, or post externally.
- Start implementation or perform unrelated cleanup.
- Quietly widen or narrow the requested scope.
- Spawn subagents or agent teams. Claude Code subagents are leaf workers; the main session owns delegation.
- Ask to expose or reproduce internal reasoning.

### Required planning output

Every Fable plan MUST contain:

1. Objective and explicit boundaries.
2. Evidence consulted, with repository paths or external sources.
3. Confirmed facts, assumptions, and unresolved questions.
4. Recommended design and only the rejected alternatives whose trade-offs affect the decision.
5. Ordered implementation slices, each with:
   - scope and non-scope;
   - dependencies;
   - approximate logic size;
   - acceptance criteria;
   - required tests;
   - required reviewers;
   - completion evidence.
6. Risks, rollback or containment considerations, and operator gates.
7. A concise implementation handoff for the Opus PM.

## 8. Opus PM/orchestrator contract

The Opus PM MUST:

1. Determine whether the request needs planning, direct execution, a focused subagent, or an agent team.
2. Give the planner the complete objective, intent, constraints, authority boundary, and relevant repository location.
3. Review the plan against authoritative sources rather than accepting it automatically.
4. Resolve conflicts and escalate only decisions that materially alter scope, architecture, safety, cost, or irreversible state.
5. Approve a bounded implementation brief before spawning writing agents.
6. Keep concurrent writing agents on disjoint file surfaces, preferably in isolated worktrees. Do not assume `isolation: worktree` engaged: it has silently no-op'd for background agent-team teammates in this repository, so verify with `git worktree list`, create an explicit `git worktree` per teammate per `docs/agent-team-worktrees.md` when isolation is needed, and serialise on any shared surface as the fallback. The same provisioning duty extends to **read-only** roles even though they write nothing: §7 requires each to verify against its own worktree, and creating that worktree is itself a repository mutation the role is forbidden to perform, so the PM MUST create it before invoking the role and confirm it with `git worktree list`. A role prompt that carries the §7 rule is therefore necessary but not sufficient — unprovisioned, the role can only start in the shared checkout. Provisioning means the *reviewed* bytes, not merely some worktree: create it at the committed SHA under review, pass its absolute path in the invocation, and require the role to self-locate against that path on every command, since cwd resets between bash calls. A worktree that exists and appears in `git worktree list` still permits a verdict rendered against the shared checkout or the base branch.
7. Ensure independent review and triage before declaring completion.
8. Report the outcome first and ground claims in current evidence.

The Opus PM MUST NOT use a planning subagent as a substitute for its own product authority or ask the planner to make human-only decisions.

## 9. Execution flow

```text
Human request
    |
    v
Opus PM: orient, identify authority and complexity
    |
    +-- simple and settled --> story-planner contract --> bounded implementation
    |                          (Codex-MCP default, D152; Sonnet fallback)
    |
    +-- complex/ambiguous --> read-only Fable plan
                              |
                              v
                         Opus adjudication
                              |
                              v
                     story-planner contract
                              |
                              v
                     bounded implementation
              (Codex-MCP default, D152; Sonnet fallback)
                              |
                              v
                 independent domain review and QA
                              |
                              v
                    independent findings triage
                              |
                              v
                      Opus integration/handoff
```

Agent teams SHOULD be reserved for independent work that benefits from peer communication. Focused subagents SHOULD be used when only a summarized result is needed. Sequential or same-file work SHOULD remain single-owner.

## 10. Delegation and cost controls

- The orchestrator MUST set a concurrency cap before starting parallel workers.
- Default maximum: three concurrent workers, excluding the orchestrator.
- The orchestrator SHOULD prefer one capable worker over several redundant workers.
- A worker MUST NOT spawn another worker.
- Subagents MUST NOT be created merely to re-check work the active model already verifies adequately.
- Independent safety, security, QA, and author-independent triage are deliberate control layers and are not considered redundant self-verification.
- Fresh verifier agents SHOULD receive findings in bounded batches rather than one new agent per minor finding.
- Effort SHOULD be lowered before weakening acceptance criteria or removing independent review.
- Implementation delegation defaults to Codex-MCP for contracted slices (D152, 9 Aug 2026; adopted from roastpilot-cloud's D145 on its results). Delegation without a `story-planner` contract is forbidden (fail-closed). The Codex subscription is one weekly-capped pool shared across repositories: both repositories' every-PR pre-open review floors are protected first, implementation delegation flexes, and below the budget-stop threshold implementation delegation stops entirely (the Claude fallback implementers take over; the orchestrator still never implements). The threshold's authoritative statement (roughly 20% remaining allowance) lives in the AGENTS.md implementation-delegation bullet; this section defers to it so the two copies cannot silently diverge.
- The Claude safety floor is NOT cut for credits: a Codex-authored diff gets the same mandatory domain reviewers, and on such a diff the Claude reviewers are the cross-family adversarial lens (the local `codex review` is then same-family).

## 11. Refusal and failure handling

Claude Fable 5 and Claude Opus 5 run safety classifiers for offensive cybersecurity and for biology or life-sciences content (Fable 5 also for summarised-reasoning extraction). A flagged request re-runs automatically: a cybersecurity flag on either model falls back to Claude Opus 4.8, while an Opus 5 biology flag ends in a refusal with no fallback. Any model, Sonnet 5 included, may also refuse outright. A refusal, or a silent fallback, is a model outcome, not a successful review. Because the Opus PM and the Opus 5 safety reviewer are themselves classifier-gated, this section applies to them too, not only to the planner and workers.

1. A missing, malformed, refused, or schema-invalid planner/reviewer result MUST fail closed.
2. The orchestrator MUST report which component failed and which scope remains unreviewed.
3. Security-oriented Fable planning SHOULD have an explicit fallback path, such as a separately defined `claude-opus-4-8` planning fallback where permitted by policy.
4. A fallback MUST retain the same read-only tools, scope, and output contract.
5. Repeated refusal or infrastructure failure MUST be escalated rather than silently treated as a clean result.
6. When a safety- or security-sensitive review runs on Opus 5, the orchestrator MUST confirm the effective model (via the `modelUsage` field or the `system/init` model), because a cybersecurity flag silently moves the review to Opus 4.8. A review whose effective model cannot be confirmed is treated as unreviewed scope.

## 12. Prompt and context hygiene

- Keep the universal project instruction file to a compact invariant and authority kernel.
- Put long operational procedures in on-demand skills or path-scoped rules.
- Maintain one authoritative copy of each mutable fact.
- Do not duplicate version numbers, tool counts, branch-protection state, or workflow semantics across role prompts without a consistency mechanism.
- Memory SHOULD use one lesson per file with a short index entry.
- Correct or retire memory entries when later evidence supersedes them.
- Do not save information already authoritative in repository files.
- Long prompts SHOULD state intent and boundaries, then rely on model capability rather than narrating every possible behaviour.

## 13. Output-style policy

Claude Code output styles modify the main conversation's system prompt. They do
not apply to named subagents, which receive their own system prompts. A fork is
the exception because it inherits the parent's full system prompt.

1. The Opus PM main conversation SHOULD use a project-defined, opt-in
   `RoastPilot Operator` output style for consistent operator-facing responses.
2. The style MUST set `keep-coding-instructions: true`. A custom style without
   this field removes Claude Code's built-in software-engineering instructions.
3. The style MUST control presentation only: outcome ordering, evidence
   labelling, brevity, progress updates, findings order, and handoff shape.
4. The style MUST NOT carry repository invariants, permissions, model routing,
   acceptance criteria, safety policy, or mutable project facts.
5. Named subagent definitions MUST retain their own output contracts. In
   particular, the Fable planner's detailed planning schema MUST remain in the
   planning-agent prompt rather than relying on the PM's output style.
6. A Fable planner MUST run as a named subagent, never a fork. A fork skips the
   subagent tool filters and receives the main conversation's exact tool pool, so
   a fork of the write-capable Opus PM is itself write-capable and cannot be the
   read-only planner. It also inherits the PM's concise output style, which
   suppresses the planning detail the plan schema requires.
7. Human-facing response style MUST NOT substitute for machine-readable output
   validation. Automated consumers SHOULD use a structured schema, such as
   Claude Code's JSON-schema support, and fail closed on invalid output.
8. The built-in Explanatory and Learning styles SHOULD NOT be the topology
   default because they intentionally increase output and Learning introduces
   interactive human coding. Proactive MAY be used only when its action bias is
   compatible with the current authority and permission boundaries.
9. Style changes take effect only after `/clear` or a new session and SHOULD be
   tested independently from model and effort changes.

### Suggested project output style

Store the shared definition at
`.claude/output-styles/roastpilot-operator.md`. Select it through `/config` or an
`outputStyle` setting. The definition MAY be committed for team reuse while the
selection remains local and opt-in.

The authoritative definition lives in
`.claude/output-styles/roastpilot-operator.md`. Per §12 (one authoritative copy
of each mutable fact) this document does not restate its frontmatter or body,
which would drift; see that file for the exact content and §13 above for the
normative requirements (presentation-only, `keep-coding-instructions` retained).
Its purpose: a concise, evidence-grounded PM presentation style that leads with
the outcome, separates facts from assumptions, and never hides a blocker,
safety-relevant fact, or dissent.

## 14. Suggested planning-agent definition

The authoritative definition lives in `.claude/agents/planning-architect.md`.
Per §12 this document does not restate its frontmatter (model, effort, permission
mode, tools), which would drift; the file is the live source and the §4 reference
table is the normative design statement for the role's model and effort. Its
purpose: a read-only, advisory planning specialist that reads the authoritative
sources first (`AGENTS.md`, the active epic via `docs/state/registry.md`, the
plan in `~/git/roastpilot-plan`, and the bearing history), returns the planning
contract from §7, and provides conclusions and evidence rather than hidden
chain-of-thought.

## 15. Evaluation plan

Before adopting this topology as the default, evaluate it on a representative set:

- one simple single-slice task that should bypass the `planning-architect` (the D152 `story-planner` contract still runs when the slice is Codex-delegated, and its doing so is compliant, not a planning-trigger violation);
- one ambiguous multi-slice feature;
- one cross-repository change;
- one safety- or security-sensitive design;
- one previously failed or heavily reworked task.

Measure:

- whether the `planning-architect` was invoked only when the trigger criteria applied (a `story-planner` run on a Codex-delegated slice is the mandatory D152 contract step, not a Fable-trigger event, and never counts against this metric);
- plan acceptance without material Opus rewrite;
- requirements or boundaries missed;
- preventable implementation rework;
- total tokens, latency, and model cost;
- unnecessary agent spawns;
- unauthorized mutations by planning/review roles;
- contradictions introduced between plans, prompts, and repository state.
- whether the PM style improves handoff consistency without hiding blockers,
  evidence, or safety-relevant detail;
- whether named subagent outputs remain governed by their agent contracts and
  unaffected by the main conversation's style;
- confirmation that the planner always runs as a named subagent and never as a
  fork, since a fork would inherit the PM's write tools and its concise style;
- input/output token and latency deltas for Default versus RoastPilot Operator.

Model and effort changes MUST be evaluated against these cases rather than adopted solely from general guidance.

Evidence is recorded in `docs/agent-topology-evaluation.md`, one entry per real
story. Recorded so far: **RP-B (#709)**, which supplies FAILURE evidence on the
ambiguous-multi-slice and safety-sensitive archetypes — no planner ran on it, so
both remain **provisional** until a later story exercises them under the
trigger-wins rule in §6. Still outstanding: those two re-validations, plus a
simple single-slice task that should bypass the `planning-architect`, a cross-repository
change, and a previously failed or heavily reworked task. The PM output-style measurements in this section remain
unevaluated, because the `RoastPilot Operator` style was not selected for the
RP-B session.

Two findings from the RP-B entry bear on this specification directly rather than
on the story. Both are now **normative text in the sections they govern** — §6's
precedence rule and §7's worktree requirement — with the evidence, the reasoning
and the numbers left in the evaluation log. Restating them here would be a
second copy of a mutable fact, which is what §12 forbids and what splitting the
log out was meant to avoid.

1. §6's trigger list and skip list can both fire on one task; see the precedence
   rule now stated in §6, and Case 1's §6 finding in the evaluation log for the
   evidence behind it.
2. §7's read-only posture depends on an operational control a reasonable agent
   can breach; see the worktree requirement now stated in §7, and Case 1's
   governance finding in the evaluation log.

## 16. Acceptance criteria

This topology is ready when:

- Exact model IDs and explicit effort levels are used for every defined Claude subagent role (the main-session model is a deliberate per-terminal operator choice — operator decision, 9 Aug 2026, see §5; the Codex-MCP default implementer is an external-family harness bound by D152's contract rule, not by a Claude pin — see the note under the §4 table).
- No override in the documented precedence chain (`CLAUDE_CODE_SUBAGENT_MODEL`, the per-invocation `model` parameter, the frontmatter `model`, `availableModels` / `enforceAvailableModels`, an organisation default, or organisation restrictions) silently defeats role-level model selection.
- The Fable planner is read-only by tool restriction: no `Edit` or `Write` tool (the same posture as the repository's other read-only roles), `permissionMode: plan`, and it runs only as a named subagent, never a fork. A hard guarantee against a determined `Bash` write is an operational control (do not run read-only roles under permissive parent modes), not a per-agent mechanism. The `story-planner` (D152) holds the same posture with no `Bash` tool at all, so its read-only guarantee is stronger than `permissionMode` alone provides.
- The Opus PM remains the sole agent authority for routing and plan adjudication.
- Simple tasks demonstrably bypass the `planning-architect` (the D152 `story-planner` contract still applies to Codex-delegated stories — §6).
- Complex plans use the required output contract.
- Planning refusals and unusable results fail closed with a documented fallback or escalation.
- Writing agents have bounded, non-overlapping ownership.
- Review and triage remain independent of the author.
- Prompts do not request hidden reasoning.
- The PM output style is presentation-only, retains Claude Code's built-in
  coding instructions, and is selected deliberately rather than forced globally.
- Named subagents retain explicit role-specific output contracts.
- Automated consumers use schema validation rather than relying on prose style.
- Evaluation evidence supports the selected models and effort levels.
- **The §7 worktree, copy-restore and committed-tree rules are delivered into the
  role definitions they bind**, not only stated here. Delivered by **#733**
  (Aug 2026).
  §7 binds every **read-only** role, so the criterion is satisfied only when all
  eight carry the controls: `mcp-contract-checker`, `planning-architect`,
  `pr-triage`, `qa`, `safety-reviewer`, `security-reviewer`, `sim-roast-runner`
  and `ui-reviewer`. (`story-planner`, added by D152, is read-only but carries
  no shell at all — no Bash, no Edit/Write — so the §7 mutate-then-restore
  hazard cannot arise for it and it does not gate this criterion. The §7
  own-worktree requirement still binds its READS: its agent definition
  requires the orchestrator to name the committed implementation-base tree
  and sha, and to `ESCALATE` when none is supplied, so a contract cannot be
  compiled from a stale or dirty checkout.)
  All eleven Bash-capable role definitions carry the controls, and
  `tests/test_agent_worktree_controls.py` guards their **presence**, not
  comprehension. Carrying the rule in all
  eight prompts does not on its own satisfy this criterion: because a read-only
  role cannot create its own worktree, the lead-side provisioning and
  `git worktree list` verification required by §8 item 6 must be in force too,
  or every invocation still starts in the shared checkout with eight compliant
  prompts. The two Bash-capable
  WRITING roles (`engineer-be`, `engineer-fe`) fall outside §7 as
  written, but the mutate-then-restore hazard is identical for them — an
  implementer runs mutation tests as readily as a reviewer — so #733 covered
  all eleven even though only the eight gate this criterion. Enumerated
  rather than sampled because an earlier draft named four: a partial list can be
  fully satisfied while most bound roles remain able to repeat the incident. Added because §7 declares those rules normative while
  the roles it binds are silent on them, so without this criterion the topology
  could become the validated default with a safety control documented but not
  in force — the exact gap §7's own text names.

## 17. Non-goals

This specification does not:

- authorize autonomous merging, deployment, publication, or destructive operations;
- define repository-specific safety invariants or coding standards;
- require Fable for every plan;
- make the planner a team lead;
- replace domain reviewers with model self-verification;
- use output styles to encode repository policy, permissions, or agent authority;
- force one presentation style onto every named subagent;
- guarantee effective runtime model selection without telemetry or provider evidence.

## 18. References

- Claude Opus 5 prompting: <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>
- Claude Fable 5 prompting: <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>
- Claude Sonnet 5 prompting: <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>
- Claude Code subagents: <https://code.claude.com/docs/en/sub-agents>
- Claude Code output styles: <https://code.claude.com/docs/en/output-styles>
- Claude Code programmatic output: <https://code.claude.com/docs/en/headless>
- Claude Code model configuration: <https://code.claude.com/docs/en/model-config>
- Claude Code agent teams: <https://code.claude.com/docs/en/agent-teams>
- Claude Code worktrees: <https://code.claude.com/docs/en/worktrees>
- Claude model IDs and versioning: <https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions>
