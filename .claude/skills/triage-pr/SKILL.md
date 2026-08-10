---
name: triage-pr
description: Independently triage the current PR's review feedback via the pr-triage subagent — which comments to address, defer, or reject, and whether it's mergeable. Use before merging an agent-team PR so the author never triages its own review (D23).
context: fork
agent: pr-triage
---

Triage the review feedback on the current branch's PR. **Run this from the
target PR's branch** — the `gh` commands below operate on the current branch,
so check out the PR's branch first (`gh pr checkout <n>`) if you're elsewhere.

**SHARED-CHECKOUT REVIEW — EXPLICIT LEAD DIRECTION:** this run is a
shared-checkout review under explicit lead direction. If your role definition
carries the fail-closed no-provisioned-worktree rule, it does not apply to this
run. Do not edit repository files for the duration of this run: no hypothesis
edits, no mutation testing, and nothing written into the tree under review.
Read-only test execution and the incidental caches or coverage artifacts it
produces are explicitly permitted. This invocation cannot perform the
shared-tree protocol's lead safety-commit, and the no-repository-edit rule is
what makes its absence tolerable — this direction is not the full shared-tree
protocol.

## PR

!`gh pr view --json title,body,number,reviewDecision,mergeStateStatus 2>/dev/null || echo "pass a PR number"`

## Review comments

!`gh pr view --comments 2>/dev/null`

## Checks

!`gh pr checks 2>/dev/null`

## Diff

!`gh pr diff 2>/dev/null`

## Task

Produce the triage plan per your `pr-triage` role: classify every comment, then
give a single CLEAR TO MERGE / BLOCK recommendation. You adjudicate only — the
author fixes; the lead merges.
