"""Guard the worktree controls in every Bash-capable role prompt.

This proves forbidden-command presence inside a prohibition context within the
discipline section, plus required-anchor presence in that section—not comprehension or
compliance; the lead-side provisioning duty (§8 item 6) remains the unguardable other
half.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO / ".claude" / "agents"
_FORBIDDEN_COMMANDS = (
    "git checkout --",
    "git restore",
    "git stash",
    "git reset",
    "git clean",
)
_DISCIPLINE_HEADING = re.compile(
    r"^## [^\n]*worktree discipline[^\n]*$", re.IGNORECASE | re.MULTILINE
)
_NEXT_LEVEL_TWO_HEADING = re.compile(r"^## ", re.MULTILINE)
_NEGATION_CUE = re.compile(
    r"\b(?:never|not|forbidden|prohibited|must\s+not|do\s+not|cannot|can't)\b",
    re.IGNORECASE,
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


def _control_chunks(section: str) -> list[str]:
    """Split a discipline section into Markdown bullets or prose paragraphs."""
    chunks: list[str] = []
    current: list[str] = []
    for line in section.splitlines()[1:]:
        starts_bullet = re.match(r"^(?:[-*+] |\d+\. )", line) is not None
        if starts_bullet or (not line.strip() and current):
            chunks.append("\n".join(current))
            current = []
        if line.strip():
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _has_negation_cue(text: str) -> bool:
    """Return whether text contains a categorical negation as a complete word."""
    return _NEGATION_CUE.search(text) is not None


def _worktree_control_errors(text: str) -> list[str]:
    """Return discipline-section control violations found in an agent prompt."""
    section = _discipline_section(text)
    if section is None:
        return ["missing Worktree discipline section"]

    errors = [
        f"missing discipline-section anchor {anchor!r}"
        for anchor in ("docs/agent-team-worktrees.md", "git show")
        if anchor not in section
    ]
    chunks = _control_chunks(section)
    for command in _FORBIDDEN_COMMANDS:
        command_chunks = [chunk for chunk in chunks if command in chunk]
        if not command_chunks:
            errors.append(f"missing discipline-section command {command!r}")
        elif not any(_has_negation_cue(chunk) for chunk in command_chunks):
            errors.append(f"command {command!r} is not in a prohibition context")
    return errors


@pytest.mark.parametrize("path", _agent_files(), ids=lambda path: path.stem)
def test_bash_capable_roles_carry_worktree_controls(path: Path) -> None:
    """Every shell-capable role must carry the scoped operational controls."""
    if "Bash" not in _tools(path):
        return

    errors = _worktree_control_errors(path.read_text())
    assert not errors, f"{path.name}: invalid worktree controls: {errors}"


def test_inverted_permission_is_rejected() -> None:
    """Literal anchors cannot satisfy the guard when the prompt permits them."""
    inverted = """## Worktree discipline (topology §7 — binding)

- `git checkout --`, `git restore`, `git stash`, `git reset`, and `git clean`
  are all fine to use whenever convenient.
- Inspect committed state with `git show` and read `docs/agent-team-worktrees.md`.
"""
    errors = _worktree_control_errors(inverted)
    prohibition_errors = [error for error in errors if "prohibition context" in error]
    assert len(prohibition_errors) == len(_FORBIDDEN_COMMANDS), errors


def test_negation_cues_use_word_boundaries() -> None:
    """The substring ``never`` inside ``whenever`` is not a negation cue."""
    assert not _has_negation_cue("These commands are fine whenever convenient.")
    assert _has_negation_cue("Never run these commands.")
    assert _has_negation_cue("These commands are forbidden.")


def test_discipline_heading_is_case_insensitive() -> None:
    """Stylistic heading capitalization does not disable the control guard."""
    prompt = """## Worktree Discipline

- Never run `git checkout --`, `git restore`, `git stash`, `git reset`, or `git clean`.
- Verify commits with `git show`; read `docs/agent-team-worktrees.md`.
"""
    assert not _worktree_control_errors(prompt)


def test_anchors_outside_discipline_section_do_not_satisfy_guard() -> None:
    """Required anchors before or after the section cannot satisfy the guard."""
    prompt = """`git show` appears before the section.

## Worktree discipline

- Never run `git checkout --`, `git restore`, `git stash`, `git reset`, or `git clean`.

## Other instructions

Read `docs/agent-team-worktrees.md` after the section.
"""
    errors = _worktree_control_errors(prompt)
    assert errors == [
        "missing discipline-section anchor 'docs/agent-team-worktrees.md'",
        "missing discipline-section anchor 'git show'",
    ]


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
