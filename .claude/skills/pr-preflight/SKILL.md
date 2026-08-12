---
name: pr-preflight
description: Run the pre-open gates, size check, self-critique, and minimum sufficient risk-routed independent review before opening a draft PR.
---

Run this on the PR branch before `gh pr create`. D158 replaces the former fixed
Claude-reviewer roster plus mandatory local-Codex pass with the risk-routed
procedure below. The GitHub exact-head Claude approval, ready-head Codex review
and wait, conversation resolution, CI, CodeQL handling, and Codecov rules in
`AGENTS.md` remain unchanged.

## 0. Orient

Record the branch, merge base, and current head:

```bash
git branch --show-current
git merge-base origin/main HEAD
git rev-parse HEAD
git diff --stat origin/main...HEAD
```

Confirm the story has a ratified `story-planner` contract and a PR-slice plan.

## 1. Deterministic gates

Run the gates for every surface the diff touches:

- Python: `.venv/bin/python -m ruff check .`,
  `.venv/bin/python -m ruff format --check .`,
  `.venv/bin/python -m pyright`, and `.venv/bin/python -m pytest`.
- Python coverage: `.venv/bin/python -m pytest --cov=roastpilot_agent
  --cov-branch --cov-report=term-missing`; cover every changed line and branch. Use
  `# pragma: no cover` only for a demonstrably unreachable path and record why.
- Web: from `web/`, run `npm run lint`, `npm run typecheck`, `npm test`, and
  `npm run build`.
- Cross-boundary contract changes: run both Python and web gates and regenerate
  affected contract fixtures before opening.
- Recorded-data ingestion or replay changes: validate against a real,
  non-committed store or log and put only a sanitised result summary in the PR.

Fix failures and rerun the affected gates before continuing.

## 2. Size and change-shape check

- Measure from the merge base. Target about 400 changed logic lines; exclude
  tests and separately committed data, fixtures, generated output, and prose.
  Agent, skill, and workflow definitions are executable logic, not prose.
- More than 600 changed test lines triggers `qa`.
- Split unrelated or independently shippable responsibilities at the ratified
  slice boundary. Explain a cohesive logic diff that materially exceeds the
  target; re-plan an unexplained one.
- Keep data, fixtures, snapshots, generated output, and research separate from
  logic when independently reviewable.

## 3. Adversarial self-critique

Inspect the diff for edge cases, failure direction, behavioural coverage,
observability gaps, dead code, debug residue, and the architecture invariants in
`AGENTS.md`. Check `docs/recent-fixes.md`. When a finding is one instance of a
class, sweep the repository and fix the class rather than only the symptom.

## 4. Minimum sufficient independent review

The Codex parent selects one independent, diff-focused review and adds only the
contract- or diff-triggered lenses in `AGENTS.md`. A contract-predicted reviewer
may be added to by the actual diff, never removed. Do not run the full Claude
roster or duplicate deterministic gates with model review.

Before relying on a reviewer, verify that its CLI or service is authenticated
and usable. If it is unavailable, stop and ask the operator; do not silently
substitute self-review or another same-family lens. Give the reviewer only the
ratified contract, relevant diff and tests, and minimum context for its lens.
The lead or `pr-triage`, never the author implementer, adjudicates substantive
findings before turning them into lead-authored repair instructions.

After any gate or review finding changes the tree:

1. Apply the adjudicated repair and commit it.
2. Rerun the affected deterministic gates on that commit.
3. Rerun every triggered reviewer whose evidence the change invalidated.
4. Repeat until the committed tree is clean and the review evidence names the
   current `HEAD`.

Do not push a fold that has not completed this loop.

## 5. Draft and ready-head handoff

Push only the clean, reviewed commit, open the PR as a draft, and record its head
SHA. Independently triage all runner, Claude, Codex, Codecov, and human findings.
A head-moving repair re-enters steps 1 and 4 before it is pushed.

Move the PR to ready only when the task or operator authorizes it. The ready
transition starts the GitHub Codex review; a draft-phase verdict does not satisfy
that post-ready wait. Before merge, follow the full PR Merge Policy in
`AGENTS.md`, including:

- exact-current-head Claude approval;
- a current-head Codex verdict, with one `@codex review` trigger only after the
  final later push when required by the documented lifecycle;
- green required CI and `codecov/patch`;
- independent disposition and resolution of every review thread; and
- branch-protection and privileged-change rules without bypass.

Opening a draft runs Claude review and its findings remain substantive even
though its diagnostic status is not itself required. Never treat a trigger,
green CI, or elapsed time as review evidence.
