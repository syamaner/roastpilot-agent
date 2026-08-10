"""Guard the worktree controls in every Bash-capable role prompt.

This proves presence of the control in the prompt, not comprehension or compliance;
the lead-side provisioning duty (§8 item 6) is the unguardable other half.
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


@pytest.mark.parametrize("path", _agent_files(), ids=lambda path: path.stem)
def test_bash_capable_roles_carry_worktree_controls(path: Path) -> None:
    """Every shell-capable role must carry each operational control anchor."""
    if "Bash" not in _tools(path):
        return

    text = path.read_text()
    assert re.search(r"^## .*?[Ww]orktree discipline", text, re.M), (
        f"{path.name}: missing Worktree discipline section"
    )
    anchors = ("docs/agent-team-worktrees.md", "git show", *_FORBIDDEN_COMMANDS)
    missing = [anchor for anchor in anchors if anchor not in text]
    assert not missing, f"{path.name}: missing worktree-control anchors: {missing}"


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
