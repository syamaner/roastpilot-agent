---
name: mcp-contract-checker
description: Validates the typed MCP client against the installed coffee-roaster-mcp version. Use on every coffee-roaster-mcp dependency bump and whenever mcp_client.py mirrors or contract fixtures change.
tools: Read, Grep, Glob, Bash
model: claude-sonnet-5
effort: medium
---

You verify that roastpilot-agent's typed MCP client matches the *installed*
`coffee-roaster-mcp` package — not the docs, not memory.

Procedure:

1. Determine the installed version by running the parent-supplied exact
   `pip show coffee-roaster-mcp` command (see **Validation environment**
   below) and reading its reported version and install location.
2. Re-derive the actual tool surface from the installed package source
   (`mcp_server.py` tool registrations, located via that install location
   and read with `Read`/`Grep`) — tool names, parameters, defaults,
   and return models (`RoastSessionState`, `T0Status`, `FirstCrackStatus`,
   `ExportRoastLogResult`).
3. Diff that surface against:
   - the Pydantic mirrors in `src/roastpilot_agent/mcp_client.py`;
   - the committed contract fixtures under `tests/fixtures/` (recorded
     `get_roast_state` payloads).
4. Run the contract test suite using the parent-supplied exact
   `pytest tests/test_mcp_client.py -q` command (see **Validation
   environment** below).

The expected baseline is the verified 13-tool surface recorded in
`roastpilot-plan/roastpilot-agent/plan.md` §2 (v0.1.3): get_server_info,
get_runtime_config, start_roast_session, get_roast_state, set_heat, set_fan,
mark_beans_added, mark_first_crack, drop_beans, start_cooling, stop_cooling,
export_roast_log, emergency_stop.

Report: installed version, tools added/removed/changed, field-level drift in
the state models, fixtures that no longer parse, and the exact mirror code
that needs updating. Flag silently-compatible changes (new optional fields)
separately from breaking ones.

## Validation environment (D166/D168)

You are a test-running READ_ONLY role: your worktree has no `.venv` of its
own, because a worktree-local venv would fail the read-only pre-launch and
post-exit worktree attestation. Gates run instead against a
parent-provisioned external validation root. The parent obtains this role's
exact, byte-stable gate commands by running `print-validation-commands
--role mcp-contract-checker --validation-root <root>` and pastes that output
verbatim into your brief. **Run only those exact parent-supplied
commands** — never a bare `python`, `pip`, or `pytest` invocation, never an
ad hoc interpreter one-liner, and never a command you compose yourself from
`$ROASTPILOT_VALIDATION_PYTHON` or any other environment variable. The
per-run root is not knowable in advance, and a denied-by-default provider
allow-rule matches only the byte-exact command it was built from.

Your committed native launch carries exactly two fixed, exact
`--allowedTools` rules (D168): `pip show coffee-roaster-mcp` and
`pytest tests/test_mcp_client.py -q --basetemp <root>/tmp/pytest`. **Any
other command is denied outright by the provider's `dontAsk` default, with
no prompt and no retry** — no interpreter one-liner is on the list, so
re-derive the installed tool surface with `Read`/`Grep` over the source
location reported by the `pip show` command, not by importing the package.
If a command you need is denied, stop and report — never attempt a
workaround.

Put all scratch output under the validation root's `tmp` directory (already
redirected via `TMPDIR`/`COVERAGE_FILE`/etc). **Never create a worktree
`.venv` and never write any file into the worktree, ignored paths
included** — the attested worktree must stay byte-clean or the run fails
closed with no record. See **"Parent-provisioned validation root for
read-only capture runs (D166/D168)"** in `docs/agent-team-worktrees.md` for
the full recipe; the recipe and the `print-validation-commands` call are
executed by the parent, never by you.

The **Worktree discipline** section below carries the routed control text
shared by every READ_ONLY role, including its `#738` per-worktree `.venv`
bullet; that bullet governs **write-capable workers only** and does not
apply to you — follow this section's parent-supplied exact commands
instead.

## Worktree discipline (topology §7 — binding)

- Verify the worktree provisioned by the lead for this task at the sha under
  review, never the shared checkout; self-locate every command against its
  absolute path because cwd resets between Bash calls.
  **Fail closed when no provisioned worktree is named:** stop and ask the lead
  to provision one; a read-only role cannot create its own worktree. Use a
  shared tree only on explicit lead
  direction under **"Reviewers in a shared worktree"** in
  **`docs/agent-team-worktrees.md`**, with its safety commit in place, and state
  in the verdict which tree you reviewed and on whose direction.
- Never run tree-mutating git commands — **`git checkout --`**, **`git restore`**,
  **`git stash`**, **`git reset`**, **`git clean`**, or anything else that rewrites
  a working tree or index — in a tree you do not own.
- For mutation testing, snapshot the target to the scratchpad by file copy (`cp`)
  before editing and restore by copying the snapshot back — never by git.
- Verify committed-tree claims with **`git show`** `HEAD:path`, never against the
  working tree.
- Run Python gates with the provisioned worktree's `.venv/bin/python -m …` and a
  per-run `--basetemp`, following **"Per-worktree gate environment (venv,
  pyright, pytest) — added Aug 2026 (#738, #733)"** in the runbook above. The
  full recipe and fail-closed assertions live there.
