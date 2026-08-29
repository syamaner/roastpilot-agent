"""Tests for the shared fail-closed agent-definition grammar and consumer boundary."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import cast

import _agent_defs
import pytest
from _agent_defs import (
    AGENTS_DIR,
    OPTIONAL_KEYS,
    REQUIRED_KEYS,
    AgentFrontmatterError,
    agent_body,
    agent_files,
    agent_text,
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
_ALLOWED_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "Edit", "Write", "mcp__playwright"})


def _fields() -> dict[str, str]:
    """Return a fresh valid field mapping for parser mutations."""
    return {
        "name": "example",
        "description": "Example:preserves embedded colons",
        "tools": "Read, Grep",
        "model": "claude-sonnet-5",
        "effort": "high",
    }


def _document(fields: dict[str, str] | None = None, body: str = "Body\n") -> str:
    """Build one otherwise-valid closed-grammar agent definition."""
    values = _fields() if fields is None else fields
    return "---\n" + "".join(f"{key}: {value}\n" for key, value in values.items()) + "---\n" + body


def _definition(tmp_path: Path, tools: str) -> Path:
    """Write one valid definition whose tools value can be varied by a test."""
    path = tmp_path / "example.md"
    path.write_text(_document({**_fields(), "tools": tools}))
    return path


def test_committed_roster_is_nonempty_and_complete() -> None:
    """The shared roster has exactly the committed Claude definitions."""
    assert {path.stem for path in agent_files()} == _COMMITTED_ROLES


def test_committed_optional_field_and_tool_vocabulary_are_preserved() -> None:
    """Committed frontmatter retains its closed optional field and tool vocabulary."""
    planner = parse_frontmatter(AGENTS_DIR / "planning-architect.md")
    assert set(planner) == REQUIRED_KEYS | OPTIONAL_KEYS
    assert planner["permissionMode"] == "dontAsk"
    assert set().union(*(agent_tools(path) for path in agent_files())) <= _ALLOWED_TOOLS


def test_split_frontmatter_preserves_embedded_colons_and_exact_body() -> None:
    """Only the leading block is parsed; later markers remain body bytes."""
    body = "First body line\n---\nname: body-marker\n"
    fields, actual_body = split_frontmatter(_document(body=body), source="exact.md")
    assert fields["description"] == "Example:preserves embedded colons"
    assert actual_body == body


@pytest.mark.parametrize("kind", ("empty", "missing", "file"))
def test_agent_files_rejects_invalid_rosters(tmp_path: Path, kind: str) -> None:
    """Missing, non-directory, and empty rosters fail closed."""
    directory = tmp_path / "agents"
    if kind == "empty":
        directory.mkdir()
    elif kind == "file":
        directory.write_text("not a roster\n")
    with pytest.raises(AgentFrontmatterError, match="agents"):
        agent_files(directory)


@pytest.mark.parametrize("target_is_directory", (False, True))
def test_agent_files_rejects_symlinked_roster_entries(
    tmp_path: Path, target_is_directory: bool
) -> None:
    """Neither a role file nor roster directory may be substituted through a symlink."""
    external = tmp_path / "external"
    external.mkdir()
    (external / "role.md").write_text(_document())
    roster = tmp_path / "agents"
    if target_is_directory:
        roster.symlink_to(external, target_is_directory=True)
    else:
        roster.mkdir()
        (roster / "linked.md").symlink_to(external / "role.md")
    with pytest.raises(AgentFrontmatterError, match="symlink|non-file"):
        agent_files(roster)


def test_agent_files_rejects_markdown_named_directory(tmp_path: Path) -> None:
    """A non-symlink directory matching the roster glob cannot be a role definition."""
    (tmp_path / "nested.md").mkdir()
    with pytest.raises(AgentFrontmatterError, match="agent roster contains a non-file definition"):
        agent_files(tmp_path)


@pytest.mark.parametrize("leading", ("\ufeff---\n", "\n---\n", "---\r\n"))
def test_split_frontmatter_rejects_nonexact_opening_marker(leading: str) -> None:
    """The opening marker is exact, byte-zero, and LF-terminated."""
    with pytest.raises(AgentFrontmatterError, match="frontmatter must begin"):
        split_frontmatter(leading + _document().removeprefix("---\n"), source="opening.md")


def test_split_frontmatter_rejects_empty_block_and_nonexact_closing_marker() -> None:
    """An empty block and a nonexact closing marker both fail closed."""
    with pytest.raises(AgentFrontmatterError, match="frontmatter has no fields"):
        split_frontmatter("---\n---\n", source="empty.md")
    malformed = _document().replace("---\nBody\n", "--- \nBody\n")
    with pytest.raises(AgentFrontmatterError, match="frontmatter field is malformed"):
        split_frontmatter(malformed, source="closing.md")


@pytest.mark.parametrize(
    "line",
    (
        "\n",
        "# comment\n",
        "- name: example\n",
        " name: example\n",
        "name:  example\n",
        "name: example\n description: continued\n",
        'name: "example"\n',
        "name: \n",
        "name example\n",
        "name: example\r\n",
        "tools: [Read]\n",
    ),
)
def test_split_frontmatter_rejects_malformed_fields(line: str) -> None:
    """The direct grammar rejects malformed, structured, and quoted field forms."""
    malformed = _document().replace("name: example\n", line, 1)
    with pytest.raises(AgentFrontmatterError, match="frontmatter field"):
        split_frontmatter(malformed, source="malformed.md")


@pytest.mark.parametrize("prefix", tuple("-?:,[]{}#&*!|>'\"%@`"))
def test_split_frontmatter_rejects_yaml_value_indicators(prefix: str) -> None:
    """Every YAML indicator or reserved prefix is closed at scalar value position."""
    malformed = _document().replace("name: example\n", f"name: {prefix}example\n", 1)
    with pytest.raises(AgentFrontmatterError, match="quoted or structured"):
        split_frontmatter(malformed, source="yaml.md")


def test_split_frontmatter_rejects_duplicate_unknown_and_each_missing_key() -> None:
    """Key membership is closed: duplicate, unknown, and missing fields are all invalid."""
    duplicate = _document().replace("tools: Read, Grep\n", "tools: Read, Grep\ntools: Bash\n")
    with pytest.raises(AgentFrontmatterError, match="duplicated"):
        split_frontmatter(duplicate, source="duplicate.md")
    with pytest.raises(AgentFrontmatterError, match="unknown keys"):
        split_frontmatter(_document({**_fields(), "unknown": "value"}), source="unknown.md")
    for key in REQUIRED_KEYS:
        fields = _fields()
        del fields[key]
        with pytest.raises(AgentFrontmatterError, match="missing required keys"):
            split_frontmatter(_document(fields), source="missing.md")


@pytest.mark.parametrize(
    "value",
    (
        "null",
        "TRUE",
        "off",
        "0",
        "-12",
        "1.5",
        "0x10",
        ".5",
        "-.5E-2",
        "+.INF",
        "-NaN",
        "2026-08-22",
        "2026-08-22T12:34:56Z",
        "12:34",
        "+12:34:56",
        "1:20.5",
        "2001-1-1 2:03:04",
    ),
)
def test_split_frontmatter_rejects_yaml_implicit_scalar_spellings(value: str) -> None:
    """Plain YAML values that loaders could coerce cannot enter closed frontmatter."""
    malformed = _document().replace("name: example\n", f"name: {value}\n", 1)
    with pytest.raises(AgentFrontmatterError, match="frontmatter field value"):
        split_frontmatter(malformed, source="implicit.md")


def test_split_frontmatter_rejects_yaml_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """YAML parser failures retain the source-named closed-frontmatter error."""

    def raise_yaml_error(_value: str) -> None:
        raise _agent_defs.yaml.YAMLError("invalid scalar")

    monkeypatch.setattr(_agent_defs.yaml, "safe_load", raise_yaml_error)
    with pytest.raises(AgentFrontmatterError, match="parse-error.md: frontmatter field value"):
        split_frontmatter(_document(), source="parse-error.md")


@pytest.mark.parametrize(
    "value",
    (
        "nullish",
        "true-blue",
        "onward",
        "v1.0",
        "1e3",
        ".5ish",
        "12:34pm",
        "1:20.5ish",
        "2001-1-1 2:03:04ish",
    ),
)
def test_split_frontmatter_allows_ordinary_scalar_lookalikes(value: str) -> None:
    """The conservative scalar grammar preserves ordinary role prose."""
    fields, _body = split_frontmatter(_document({**_fields(), "description": value}))
    assert fields["description"] == value


@pytest.mark.parametrize(
    "value", ("example ", "example\tvalue", "example\x7fvalue", "example # note")
)
def test_split_frontmatter_rejects_ambiguous_or_control_scalars(value: str) -> None:
    """Trailing space, tabs, controls, and YAML comments cannot enter scalar values."""
    malformed = _document().replace("name: example\n", f"name: {value}\n", 1)
    with pytest.raises(AgentFrontmatterError, match="frontmatter field value is malformed"):
        split_frontmatter(malformed, source="scalar.md")


def test_split_frontmatter_rejects_mapping_separator_but_allows_embedded_colon() -> None:
    """A colon-space mapping separator is forbidden while an embedded colon remains plain."""
    malformed = _document().replace("name: example\n", "name: example: nested\n", 1)
    with pytest.raises(AgentFrontmatterError, match="frontmatter field value is malformed"):
        split_frontmatter(malformed, source="separator.md")
    fields, _body = split_frontmatter(_document({**_fields(), "description": "READ-ONLY—reports"}))
    assert fields["description"] == "READ-ONLY—reports"


@pytest.mark.parametrize(
    "tools", ("Read,,Grep", "Read, mcp-playwright", "Read, Read", "Read, Task")
)
def test_agent_tools_rejects_malformed_duplicate_or_unapproved_names(
    tmp_path: Path, tools: str
) -> None:
    """Tool lists cannot contain malformed, duplicate, or uncommitted capabilities."""
    path = _definition(tmp_path, tools)
    with pytest.raises(AgentFrontmatterError, match="example.md"):
        parse_frontmatter(path)
    with pytest.raises(AgentFrontmatterError, match="example.md"):
        agent_tools(path)


def test_agent_tools_accepts_the_exact_committed_vocabulary(tmp_path: Path) -> None:
    """Every admitted tool is accepted without widening the vocabulary."""
    tools = ", ".join(sorted(_ALLOWED_TOOLS))
    assert agent_tools(_definition(tmp_path, tools)) == _ALLOWED_TOOLS


def test_path_backed_helpers_validate_and_preserve_bodies(tmp_path: Path) -> None:
    """Shared path helpers retain source-named failures and exact valid bodies."""
    malformed = tmp_path / "broken.md"
    malformed.write_text("---\nname: example\n")
    with pytest.raises(AgentFrontmatterError, match="broken.md"):
        parse_frontmatter(malformed)
    valid = tmp_path / "body.md"
    body = "Unchanged body\n---\n"
    valid.write_text(_document(body=body))
    assert agent_text(valid) == _document(body=body)
    assert agent_body(valid) == body


def test_agent_body_reads_and_splits_the_definition_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Body extraction does not re-read an already-read role definition."""
    path = tmp_path / "body.md"
    reads: list[Path] = []

    def read_once(actual_path: Path) -> str:
        reads.append(actual_path)
        return _document(body="Body\n")

    monkeypatch.setattr(_agent_defs, "_read_agent", read_once)
    assert agent_body(path) == "Body\n"
    assert reads == [path]


def test_parse_frontmatter_rejects_invalid_utf8_with_source_named_reason(tmp_path: Path) -> None:
    """Path-backed parsing refuses invalid bytes without an undecoded fallback."""
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    with pytest.raises(
        AgentFrontmatterError, match="invalid.md: agent definition cannot be read as UTF-8"
    ):
        parse_frontmatter(invalid)


# Governance deliberately couples these two consumers to exact semantic AST projections.
# The projection uses the Python 3.11 grammar across supported Python 3.11--3.14, sorts
# node fields, and excludes only the version-added non-runtime ``type_params`` field.
# Comments and formatting are absent from ASTs; every retained semantic field requires a
# deliberate digest update alongside review and direct helper tests.
_CONSUMER_SEMANTIC_SHA256 = {
    # #702 slice 2: both digests moved when the broad module-level
    # `pytestmark = pytest.mark.docs` assignment was replaced by an exact
    # `@pytest.mark.docs` decorator on the one function in each module that
    # actually reads committed docs/**/*.md content (scripts/docs_reader_governance.py
    # is the new authoritative detector). A module-level assignment and a
    # function decorator are different AST shapes, so this digest move is
    # the deliberate, reviewed update this comment block requires — not drift.
    "test_agent_model_pins.py": "6ae75063dc945f6c84d471f7b4bc8c32506147dbf080e5872d774ed699ba73a5",
    "test_agent_worktree_controls.py": (
        "f87b4b5ddda26bb9d32c539766115995c9ce23bb1817d3cf8e1f60deba3040bb"
    ),
}
_NON_RUNTIME_AST_FIELDS = frozenset({"type_params"})


def _consumer_semantic_sha256(path: Path) -> str:
    """Return the supported-version semantic digest for one governed consumer."""
    return _canonical_semantic_sha256(path.read_text())


def _semantic_ast_projection(value: object) -> object:
    """Return a fixed JSON-safe projection of a Python 3.11 semantic AST."""
    if isinstance(value, ast.AST):
        field_names = sorted(value._fields)
        field_values = cast(dict[str, object], vars(value))
        serialized_fields: list[tuple[str, object]] = []
        for field in field_names:
            if field in _NON_RUNTIME_AST_FIELDS:
                continue
            field_value: object = field_values.get(field)
            serialized_fields.append((field, _semantic_ast_projection(field_value)))
        return {
            "node": type(value).__name__,
            "fields": serialized_fields,
        }
    if isinstance(value, list):
        return [_semantic_ast_projection(item) for item in cast(list[object], value)]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported semantic AST value: {type(value).__name__}")


def _canonical_semantic_sha256(source: str) -> str:
    """Return a comment- and formatting-insensitive cross-version semantic digest."""
    tree = ast.parse(source, feature_version=(3, 11))
    canonical = json.dumps(
        _semantic_ast_projection(tree), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_governed_consumers_match_closed_semantic_fingerprints() -> None:
    """Both governed consumers remain exactly at their reviewed executable semantics."""
    assert {
        filename: _consumer_semantic_sha256(_REPO / "tests" / filename)
        for filename in _CONSUMER_SEMANTIC_SHA256
    } == _CONSUMER_SEMANTIC_SHA256


def test_canonical_semantic_digest_changes_for_guard_mutations() -> None:
    """Marker-regex and roster-provenance behavior cannot drift under the closed boundary."""
    marker = 'import re\nre.compile(r"^---\\n")\n'
    alternate_marker = 'import re\nre.compile(r"^---")\n'
    roster = "agent_files()\n"
    alternate_roster = "agent_files(directory)\n"
    assert _canonical_semantic_sha256(marker) == (
        "a2029c153284a6ae8282c0f551da4d3ec59be3d79ff86e9c6aac01caf9b3ad58"
    )
    assert _canonical_semantic_sha256(roster) == (
        "30fd1e1b751065a2b2f89c3326619d7584ef7b8fdedacf630a032843a6b05fce"
    )
    assert _canonical_semantic_sha256(marker) != _canonical_semantic_sha256(alternate_marker)
    assert _canonical_semantic_sha256(roster) != _canonical_semantic_sha256(alternate_roster)


def test_canonical_semantic_digest_ignores_comments_and_formatting() -> None:
    """Comments and layout do not perturb the canonical semantic digest."""
    compact = "def parse():\n    return agent_files()\n"
    formatted = "# governance comment\n\ndef parse( ):\n\n    return agent_files( )\n"
    assert _canonical_semantic_sha256(compact) == _canonical_semantic_sha256(formatted)


def test_governed_consumers_import_and_call_parse_frontmatter() -> None:
    """Parser provenance remains explicit and load-bearing in both governed consumers."""
    for filename in _CONSUMER_SEMANTIC_SHA256:
        tree = ast.parse((_REPO / "tests" / filename).read_text(), filename=filename)
        imported = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "_agent_defs" and node.level == 0
            for alias in node.names
            if alias.name == "parse_frontmatter"
        }
        assert imported == {"parse_frontmatter"}
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "parse_frontmatter"
            for node in ast.walk(tree)
        )
