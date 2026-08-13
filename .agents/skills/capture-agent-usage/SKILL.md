---
name: capture-agent-usage
description: Opt-in, parent-only local capture of metadata-only Codex and Claude task usage.
---

Use only from the top-level Codex parent; leaves must not invoke it or cross the
Codex/Claude family boundary. The skill records local metadata only, never prompts,
responses, tool output, environment values, or CLI raw output.

Use `scripts/capture_usage_cli.py snapshot-capacity` only for a qualitative capacity
observation. Use `parse-codex` or `parse-claude` with sanitized streams to verify the
frozen parser grammar, and `annotate-outcome` after final gates to record closed finding
counts and rework metadata. The default `.agent-usage/usage.jsonl` is local-only and
gitignored.

For an explicitly authorized implementation task, the parent may use `run` with a
maintainer-selected `--prompt-file` and explicit task, slice, harness, role, model,
repository, branch, and SHA metadata; effort, parent, and whole-tree fields are optional.
It sends that prompt only to the selected provider on stdin and records normalized
metadata and usage totals only.
