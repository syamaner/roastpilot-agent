"""Tests for the shared fail-closed agent-definition frontmatter grammar."""

from __future__ import annotations

from pathlib import Path

import pytest
from _agent_defs import (
    AGENTS_DIR,
    OPTIONAL_KEYS,
    REQUIRED_KEYS,
    AgentFrontmatterError,
    agent_body,
    agent_files,
    agent_tools,
    parse_frontmatter,
    split_frontmatter,
)

_REPO = Path(__file__).resolve().parents[1]
_COMMITTED_ROLES = {
    "engineer-be",
    "engineer-fe",
    "mcp-contract-checker",
    "planning-architect",
    "pr-triage",
    "product-auditor",
    "qa",
    "safety-reviewer",
    "security-reviewer",
    "sim-roast-runner",
    "story-planner",
    "ui-reviewer",
}


def _document(fields: dict[str, str] | None = None, body: str = "Body\n") -> str:
    """Build one otherwise-valid closed-grammar agent definition."""
    values = {
        "name": "example",
        "description": "Example: preserves embedded colons",
        "tools": "Read, Grep",
        "model": "claude-sonnet-5",
        "effort": "high",
    }
    if fields is not None:
        values = fields
    return "---\n" + "".join(f"{key}: {value}\n" for key, value in values.items()) + "---\n" + body


def _definition(tmp_path: Path, tools: str) -> Path:
    """Write one valid definition whose tools value can be varied by a test."""
    path = tmp_path / "example.md"
    path.write_text(_document({**_fields(), "tools": tools}))
    return path


def _fields() -> dict[str, str]:
    """Return a fresh valid field mapping for mutation-oriented parser tests."""
    return {
        "name": "example",
        "description": "Example: preserves embedded colons",
        "tools": "Read, Grep",
        "model": "claude-sonnet-5",
        "effort": "high",
    }


def test_committed_roster_is_nonempty_and_complete() -> None:
    """The shared roster has exactly the twelve committed Claude definitions."""
    paths = agent_files()
    assert paths
    assert {path.stem for path in paths} == _COMMITTED_ROLES


def test_optional_permission_mode_and_committed_playwright_tool_are_preserved() -> None:
    """The closed grammar retains the one optional field and committed MCP tool name."""
    planner = parse_frontmatter(AGENTS_DIR / "planning-architect.md")
    assert planner["permissionMode"] == "dontAsk"
    assert set(planner) == REQUIRED_KEYS | OPTIONAL_KEYS
    assert "mcp__playwright" in agent_tools(AGENTS_DIR / "ui-reviewer.md")


def test_split_frontmatter_preserves_embedded_colons_and_exact_body() -> None:
    """Only the leading block is parsed; later markers remain untouched body bytes."""
    body = "First body line\n---\nname: body-marker\n"
    fields, actual_body = split_frontmatter(_document(body=body), source="exact.md")
    assert fields["description"] == "Example: preserves embedded colons"
    assert actual_body == body


@pytest.mark.parametrize("kind", ("empty", "missing", "file"))
def test_agent_files_rejects_invalid_rosters(tmp_path: Path, kind: str) -> None:
    """Missing, non-directory, and empty rosters cannot silently yield no roles."""
    directory = tmp_path / "agents"
    if kind == "empty":
        directory.mkdir()
    elif kind == "file":
        directory.write_text("not a roster\n")
    with pytest.raises(AgentFrontmatterError, match="agents"):
        agent_files(directory)


@pytest.mark.parametrize("leading", ("\ufeff---\n", "\n---\n", "---\r\n"))
def test_split_frontmatter_rejects_invalid_leading_markers(leading: str) -> None:
    """The opening marker is anchored at byte zero with an exact newline."""
    with pytest.raises(AgentFrontmatterError, match="bad.md"):
        split_frontmatter(leading + _document().removeprefix("---\n"), source="bad.md")


def test_split_frontmatter_rejects_missing_terminator_with_source() -> None:
    """An unterminated leading block fails with the supplied source name."""
    with pytest.raises(AgentFrontmatterError, match="unterminated.md"):
        split_frontmatter("---\nname: example\n", source="unterminated.md")


def test_split_frontmatter_rejects_bad_terminator() -> None:
    """Only the exact closing marker ends the leading frontmatter block."""
    malformed = _document().replace("---\nBody\n", "--- \nBody\n")
    with pytest.raises(AgentFrontmatterError, match="terminator.md"):
        split_frontmatter(malformed, source="terminator.md")


def test_split_frontmatter_never_silently_skips_malformed_lines() -> None:
    """A valid later block cannot make an earlier malformed field inert."""
    malformed = "---\n# comment\n" + _document().removeprefix("---\n")
    with pytest.raises(AgentFrontmatterError, match="skipped.md"):
        split_frontmatter(malformed, source="skipped.md")


@pytest.mark.parametrize(
    "line",
    (
        "\n",
        "# comment\n",
        "- name: example\n",
        " name: example\n",
        "name: example\n description: continued\n",
        'name: "example"\n',
        "name: \n",
        "name example\n",
        "name: example\r\n",
        "tools: [Read]\n",
    ),
)
def test_split_frontmatter_rejects_every_malformed_field_class(line: str) -> None:
    """Blank, comment, list, indentation, multiline, quoted, and CR fields fail closed."""
    with pytest.raises(AgentFrontmatterError, match="malformed.md"):
        split_frontmatter("---\n" + line + "---\n", source="malformed.md")


def test_split_frontmatter_rejects_duplicate_and_unknown_keys() -> None:
    """Duplicate last-wins and future unreviewed fields are both invalid."""
    duplicate = _document() + ""
    duplicate = duplicate.replace("tools: Read, Grep\n", "tools: Read, Grep\ntools: Bash\n")
    with pytest.raises(AgentFrontmatterError, match="duplicate.md"):
        split_frontmatter(duplicate, source="duplicate.md")
    unknown = _document({**_fields(), "unknown": "value"})
    with pytest.raises(AgentFrontmatterError, match="unknown.md"):
        split_frontmatter(unknown, source="unknown.md")


@pytest.mark.parametrize("key", sorted(REQUIRED_KEYS))
def test_split_frontmatter_rejects_each_missing_required_key(key: str) -> None:
    """Every required governance field is closed rather than optional by accident."""
    fields = _fields()
    del fields[key]
    with pytest.raises(AgentFrontmatterError, match="missing.md"):
        split_frontmatter(_document(fields), source="missing.md")


@pytest.mark.parametrize("tools", ("Read,,Grep", "Read, mcp-playwright", "Read, Read"))
def test_agent_tools_rejects_malformed_or_duplicate_names(tmp_path: Path, tools: str) -> None:
    """Tool lists cannot contain empty, malformed, or duplicate capability names."""
    with pytest.raises(AgentFrontmatterError, match="example.md"):
        agent_tools(_definition(tmp_path, tools))


def test_parse_frontmatter_and_agent_body_name_the_source(tmp_path: Path) -> None:
    """Path-backed helper errors retain the definition name while preserving valid bodies."""
    malformed = tmp_path / "broken.md"
    malformed.write_text("---\nname: example\n")
    with pytest.raises(AgentFrontmatterError, match="broken.md"):
        parse_frontmatter(malformed)
    valid = tmp_path / "body.md"
    body = "Unchanged body\n---\n"
    valid.write_text(_document(body=body))
    assert agent_body(valid) == body


def test_consuming_guards_use_only_the_shared_roster_and_parser() -> None:
    """Neither consumer may restore a local roster glob or frontmatter/body parser."""
    required_imports = {
        "test_agent_worktree_controls.py": {"agent_files", "parse_frontmatter", "agent_tools"},
        "test_agent_model_pins.py": {"agent_files", "parse_frontmatter", "agent_body"},
    }
    local_roster = '_REPO / ".claude" / ' + '"agents"'
    frontmatter_anchor = 'r"' + "^" + "---"
    for filename, names in required_imports.items():
        text = (_REPO / "tests" / filename).read_text()
        assert "from _agent_defs import" in text
        assert all(name in text for name in names)
        assert "AGENTS_DIR.glob(" not in text
        assert local_roster not in text
        assert "def _agent_files" not in text
        assert "def _frontmatter" not in text
        assert "def _tools" not in text
        assert "def _body" not in text
        assert frontmatter_anchor not in text
