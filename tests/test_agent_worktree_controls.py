"""Guard the ratified worktree controls in every Bash-capable role prompt.

This proves each Bash-capable role carries its ratified control text verbatim, not
comprehension or compliance; the lead-side provisioning duty (§8 item 6) remains the
unguardable other half. Faithful rewording deliberately fails: each block is a routed
control with one authoritative copy here, so a change is made once in this test and
routed to every applicable role in the same commit instead of drifting file by file.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO / ".claude" / "agents"
_DISCIPLINE_HEADING = re.compile(
    r"^## [^\n]*worktree discipline[^\n]*$", re.IGNORECASE | re.MULTILINE
)
_NEXT_LEVEL_TWO_HEADING = re.compile(r"^## ", re.MULTILINE)

# These are the two authoritative routed-control variants copied byte-for-byte from
# the ratified role prompts. Intentional edits update the relevant constant and every
# role that consumes it in the same commit.
_READ_ONLY_DISCIPLINE_BLOCK = """## Worktree discipline (topology §7 — binding)

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
"""

_WRITING_DISCIPLINE_BLOCK = """## Worktree discipline (topology §7 — binding)

- Your assigned worktree is the **only** tree you write in; the main checkout
  and sibling worktrees are read-only (`git -C` peeks are fine, never a write).
  Self-locate every command against the assigned worktree because cwd resets
  between Bash calls.
- Never run tree-mutating git commands — **`git checkout --`**, **`git restore`**,
  **`git stash`**, **`git reset`**, **`git clean`**, or anything else that rewrites
  a working tree or index — in a tree you do not own.
- For mutation testing, snapshot the target to the scratchpad by file copy (`cp`)
  before editing and restore by copying the snapshot back — never by git.
- Verify committed-tree claims with **`git show`** `HEAD:path`, never against the
  working tree.
- Run Python gates with the assigned worktree's `.venv/bin/python -m …` and a
  per-run `--basetemp`, following **"Per-worktree gate environment (venv,
  pyright, pytest) — added Aug 2026 (#738, #733)"** in
  **`docs/agent-team-worktrees.md`**. The full recipe and fail-closed assertions
  live there.
"""

_ADVERSARIAL_BLOCKS = (
    (
        "round-1-inverted-permission",
        """## Worktree discipline (topology §7 — binding)

- `git checkout --`, `git restore`, `git stash`, `git reset`, and `git clean`
  are all fine to use whenever convenient.
- Inspect committed state with `git show` and read `docs/agent-team-worktrees.md`.
""",
    ),
    (
        "not-forbidden",
        """## Worktree discipline (topology §7 — binding)

- `git checkout --`, `git restore`, `git stash`, `git reset`, and `git clean` are
  not forbidden and may be used whenever convenient.
- Inspect committed state with `git show` and read `docs/agent-team-worktrees.md`.
""",
    ),
    (
        "cross-bullet-contradiction",
        """## Worktree discipline (topology §7 — binding)

- Never run `git checkout --`, `git restore`, `git stash`, `git reset`, or `git clean`.
- Despite the previous bullet, all of those commands are permitted whenever useful.
- Inspect committed state with `git show` and read `docs/agent-team-worktrees.md`.
""",
    ),
    (
        "single-sentence-scope",
        """## Worktree discipline (topology §7 — binding)

- Never run `git checkout --`; `git restore`, `git stash`, `git reset`, and `git clean`
  are permitted whenever convenient.
- Inspect committed state with `git show` and read `docs/agent-team-worktrees.md`.
""",
    ),
    (
        "numbered-bullet-gap",
        """## Worktree discipline (topology §7 — binding)

- Never mutate an unowned tree without first considering the command.
1) `git checkout --`, `git restore`, `git stash`, `git reset`, and `git clean` are
   permitted whenever convenient.
- Inspect committed state with `git show` and read `docs/agent-team-worktrees.md`.
""",
    ),
)


def _agent_files() -> list[Path]:
    """Return every agent definition from the authoritative directory roster."""
    return sorted(_AGENTS_DIR.glob("*.md"))


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the simple ``key: value`` frontmatter used by agent definitions."""
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path.name}: no YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    assert "tools" in fields, f"{path.name}: frontmatter has no tools field"
    assert fields["tools"], f"{path.name}: frontmatter tools field is empty"
    return fields


def _tools(path: Path) -> set[str]:
    """Return the parsed comma-separated frontmatter tool names."""
    tools = {tool.strip() for tool in _frontmatter(path)["tools"].split(",")}
    assert "" not in tools, f"{path.name}: frontmatter tools list is malformed"
    malformed = [tool for tool in tools if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", tool)]
    assert not malformed, f"{path.name}: malformed frontmatter tool names: {malformed}"
    return tools


def _discipline_section(text: str) -> str | None:
    """Return the worktree-discipline section through the next level-two heading."""
    heading = _DISCIPLINE_HEADING.search(text)
    if heading is None:
        return None
    next_heading = _NEXT_LEVEL_TWO_HEADING.search(text, heading.end())
    end = next_heading.start() if next_heading is not None else len(text)
    return text[heading.start() : end]


def _canonical_block_mismatch(text: str, expected: str, source: str) -> str | None:
    """Return an actionable unified diff when a routed control block has drifted."""
    actual = _discipline_section(text)
    if actual == expected:
        return None

    actual_for_diff = actual if actual is not None else "<missing discipline section>\n"
    difference = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual_for_diff.splitlines(),
            fromfile="expected canonical discipline block",
            tofile=f"actual discipline block in {source}",
            lineterm="",
        )
    )
    return (
        f"{source}: worktree discipline block differs from its canonical variant. "
        "This discipline block is a routed control with one authoritative copy in "
        "tests/test_agent_worktree_controls.py; an intentional change means updating "
        "the constant there and every routed role in the same commit.\n"
        f"{difference}"
    )


def _expected_variant(tools: set[str]) -> str:
    """Select the canonical block from write capability, never from a role list."""
    if tools.intersection({"Edit", "Write"}):
        return _WRITING_DISCIPLINE_BLOCK
    return _READ_ONLY_DISCIPLINE_BLOCK


@pytest.mark.parametrize("path", _agent_files(), ids=lambda path: path.stem)
def test_bash_capable_roles_carry_worktree_controls(path: Path) -> None:
    """Every shell-capable role must carry its canonical routed-control variant."""
    tools = _tools(path)
    if "Bash" not in tools:
        return

    mismatch = _canonical_block_mismatch(path.read_text(), _expected_variant(tools), path.name)
    assert mismatch is None, mismatch


@pytest.mark.parametrize(
    ("case_name", "block"),
    _ADVERSARIAL_BLOCKS,
    ids=[case_name for case_name, _ in _ADVERSARIAL_BLOCKS],
)
def test_inverted_permission_is_rejected(case_name: str, block: str) -> None:
    """Known lexical bypasses cannot equal the canonical routed control."""
    mismatch = _canonical_block_mismatch(block, _READ_ONLY_DISCIPLINE_BLOCK, case_name)
    assert mismatch is not None


def test_story_planner_remains_shell_and_write_closed() -> None:
    """A shell addition must force a conscious discipline-block decision."""
    tools = _tools(_AGENTS_DIR / "story-planner.md")
    forbidden = {"Bash", "Edit", "Write"}
    assert tools.isdisjoint(forbidden), (
        "story-planner.md gained shell or write capability; decide whether to "
        "add the worktree-discipline control before enabling it"
    )


def test_runbook_citations_never_use_line_anchors() -> None:
    """Runbook citations use durable section names, never line numbers."""
    guarded = [
        *_agent_files(),
        _REPO / "docs" / "agent-topology.md",
        _REPO / "docs" / "state" / "registry.md",
        _REPO / "AGENTS.md",
    ]
    pattern = re.compile(r"agent-team-worktrees\.md:[0-9]")
    offenders = [path.relative_to(_REPO) for path in guarded if pattern.search(path.read_text())]
    assert not offenders, f"line-anchored runbook citations found in: {offenders}"
