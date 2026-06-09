---
name: triage-pr
description: Independently triage the current PR's review feedback via the pr-triage subagent — which comments to address, defer, or reject, and whether it's mergeable. Use before merging an agent-team PR so the author never triages its own review (D23).
context: fork
agent: pr-triage
---

Triage the review feedback on the target PR (default: the current branch's PR).
Pass the PR number as the argument if it isn't the current branch.

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
