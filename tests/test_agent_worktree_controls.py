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
# This intentionally catches common mechanical citations plus one bounded prose form;
# it is not an exhaustive detector for every way a human could describe a line range.
_RUNBOOK_LINE_CITATIONS = (
    re.compile(r"agent-team-worktrees\.md:[0-9]+"),
    re.compile(r"agent-team-worktrees\.md#L[0-9]+(?:-L[0-9]+)?", re.IGNORECASE),
    re.compile(r"agent-team-worktrees\.md[^\n]{0,48}\blines?\s+[0-9]+", re.IGNORECASE),
)

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

- In each repository, your assigned worktree is the **only** tree you write in;
  that repository's main checkout and sibling worktrees are read-only (`git -C`
  peeks are fine, never a write).
  For a lead-directed serialized or standalone run, the main checkout is the
  assigned writable tree; sibling worktrees remain read-only.
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


def _normalize_section_separator(text: str) -> str:
    """Normalize only trailing whitespace at a discipline-section boundary."""
    return text.rstrip(" \t\r\n") + "\n"


def _canonical_block_mismatch(text: str, expected: str, source: str) -> str | None:
    """Return an actionable unified diff when a routed control block has drifted."""
    heading_count = len(_DISCIPLINE_HEADING.findall(text))
    if heading_count != 1:
        return f"{source}: expected exactly one worktree discipline heading, found {heading_count}"

    actual = _discipline_section(text)
    normalized_expected = _normalize_section_separator(expected)
    normalized_actual = _normalize_section_separator(actual) if actual is not None else None
    if normalized_actual == normalized_expected:
        return None

    actual_for_diff = normalized_actual or "<missing discipline section>\n"
    difference = "\n".join(
        difflib.unified_diff(
            normalized_expected.splitlines(),
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


def test_following_section_separator_preserves_canonical_match() -> None:
    """A later level-two section changes only the separator, not control bytes."""
    prompt = f"{_READ_ONLY_DISCIPLINE_BLOCK}\n## Later instructions\n\nUnrelated text.\n"
    mismatch = _canonical_block_mismatch(prompt, _READ_ONLY_DISCIPLINE_BLOCK, "later-section")
    assert mismatch is None, mismatch


def test_duplicate_discipline_heading_is_rejected() -> None:
    """A later duplicate cannot sit outside the exact-matched control section."""
    prompt = f"{_READ_ONLY_DISCIPLINE_BLOCK}\n## Worktree Discipline\n\nStray copy.\n"
    mismatch = _canonical_block_mismatch(prompt, _READ_ONLY_DISCIPLINE_BLOCK, "duplicate")
    assert mismatch is not None
    assert "expected exactly one worktree discipline heading, found 2" in mismatch


def _shared_checkout_direction(workflow: str) -> str:
    """Extract the workflow's single authoritative shared-checkout direction."""
    match = re.search(r"const SHARED_CHECKOUT_DIRECTION = `(.*?)`\n", workflow, re.DOTALL)
    assert match, "SHARED_CHECKOUT_DIRECTION not found in review-branch.mjs"
    return match.group(1)


def _javascript_agent_calls(source: str) -> list[str]:
    """Extract real ``agent(...)`` calls while ignoring comments and strings."""
    calls: list[str] = []
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", index):
            end_comment = source.find("*/", index + 2)
            index = len(source) if end_comment == -1 else end_comment + 2
            continue
        if source[index] in "'\"`":
            quote = source[index]
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                elif source[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            continue
        if source.startswith("agent", index):
            before = source[index - 1] if index else ""
            after_name = index + len("agent")
            after = source[after_name] if after_name < len(source) else ""
            if not (before.isalnum() or before in "_$") and not (after.isalnum() or after in "_$"):
                open_paren = after_name
                while open_paren < len(source) and source[open_paren].isspace():
                    open_paren += 1
                if open_paren < len(source) and source[open_paren] == "(":
                    depth = 1
                    cursor = open_paren + 1
                    quote: str | None = None
                    while cursor < len(source) and depth:
                        char = source[cursor]
                        if quote is not None:
                            if char == "\\":
                                cursor += 2
                                continue
                            if char == quote:
                                quote = None
                        elif source.startswith("//", cursor):
                            newline = source.find("\n", cursor + 2)
                            cursor = len(source) if newline == -1 else newline
                            continue
                        elif source.startswith("/*", cursor):
                            end_comment = source.find("*/", cursor + 2)
                            cursor = len(source) if end_comment == -1 else end_comment + 2
                            continue
                        elif char in "'\"`":
                            quote = char
                        elif char == "(":
                            depth += 1
                        elif char == ")":
                            depth -= 1
                        cursor += 1
                    assert depth == 0, "unterminated agent() call in review-branch.mjs"
                    calls.append(source[index:cursor])
                    index = cursor
                    continue
        index += 1
    return calls


def _role_backed_agent_calls(workflow: str) -> list[str]:
    """Select calls whose inline or variable options can carry ``agentType``."""
    typed_option_variables = set(
        re.findall(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\.agentType\s*=", workflow)
    )
    role_calls: list[str] = []
    for call in _javascript_agent_calls(workflow):
        inline_agent_type = re.search(r"\bagentType\s*:", call) is not None
        variable_options = any(
            re.search(rf",\s*{re.escape(name)}\s*,?\s*\)$", call, re.DOTALL)
            for name in typed_option_variables
        )
        if inline_agent_type or variable_options:
            role_calls.append(call)
    return role_calls


def test_shared_checkout_direction_is_narrow_and_routed() -> None:
    """Every role-backed workflow call and the triage skill receive the direction."""
    workflow = (_REPO / ".claude" / "workflows" / "review-branch.mjs").read_text()
    direction = _shared_checkout_direction(workflow)
    required = (
        "SHARED-CHECKOUT REVIEW — EXPLICIT LEAD DIRECTION",
        "If your role definition carries the fail-closed no-provisioned-worktree rule",
        "Do not edit repository files",
        "no hypothesis edits",
        "no mutation testing",
        "nothing written into the tree under review",
        "Read-only test execution",
        "incidental caches or coverage artifacts",
        "cannot perform the shared-tree protocol's lead safety-commit",
        "not the full shared-tree protocol",
    )
    missing = [phrase for phrase in required if phrase not in direction]
    assert not missing, f"shared-checkout direction is missing controls: {missing}"
    assert "Do not mutate any file" not in direction

    role_calls = _role_backed_agent_calls(workflow)
    assert role_calls, "review-branch.mjs has no role-backed agent() calls"
    uncovered: list[str] = []
    for call in role_calls:
        if "${SHARED_CHECKOUT_DIRECTION}" not in call:
            uncovered.append(call.splitlines()[0])
    assert not uncovered, (
        "every agentType-bearing agent() call must receive "
        f"SHARED_CHECKOUT_DIRECTION; uncovered calls: {uncovered}"
    )

    skill = (_REPO / ".claude" / "skills" / "triage-pr" / "SKILL.md").read_text()
    normalized_skill = " ".join(skill.split())
    skill_missing = [phrase for phrase in required if phrase not in normalized_skill]
    assert not skill_missing, (
        f"triage-pr skill is missing shared-checkout controls: {skill_missing}"
    )


def test_story_planner_remains_shell_and_write_closed() -> None:
    """A shell addition must force a conscious discipline-block decision."""
    tools = _tools(_AGENTS_DIR / "story-planner.md")
    forbidden = {"Bash", "Edit", "Write"}
    assert tools.isdisjoint(forbidden), (
        "story-planner.md gained shell or write capability; decide whether to "
        "add the worktree-discipline control before enabling it"
    )


def _has_runbook_line_citation(text: str) -> bool:
    """Return whether text contains a supported runbook line-citation form."""
    return any(pattern.search(text) is not None for pattern in _RUNBOOK_LINE_CITATIONS)


@pytest.mark.parametrize(
    "citation",
    (
        "docs/agent-team-worktrees.md:99",
        "docs/agent-team-worktrees.md#L99",
        "docs/agent-team-worktrees.md#L99-L140",
        "docs/agent-team-worktrees.md, line 99",
        "docs/agent-team-worktrees.md — see lines 99-140",
    ),
    ids=("colon", "fragment", "fragment-range", "prose-line", "prose-lines"),
)
def test_runbook_line_citation_forms_are_detected(citation: str) -> None:
    """Each supported mechanical or bounded-prose form is rejected."""
    assert _has_runbook_line_citation(citation)


def test_runbook_citations_never_use_line_anchors() -> None:
    """Runbook citations use durable section names, never line numbers."""
    guarded = [
        *_agent_files(),
        *sorted((_REPO / "docs").rglob("*.md")),
        _REPO / "AGENTS.md",
    ]
    offenders = [
        path.relative_to(_REPO) for path in guarded if _has_runbook_line_citation(path.read_text())
    ]
    assert not offenders, f"line-anchored runbook citations found in: {offenders}"
