"""Tests for the shared fail-closed agent-definition frontmatter grammar."""

from __future__ import annotations

import ast
from collections.abc import Callable
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


def test_agent_files_rejects_markdown_named_directory(tmp_path: Path) -> None:
    """A directory matching the roster glob cannot masquerade as a definition file."""
    (tmp_path / "nested.md").mkdir()
    with pytest.raises(AgentFrontmatterError, match="agent roster contains a non-file definition"):
        agent_files(tmp_path)


@pytest.mark.parametrize("leading", ("\ufeff---\n", "\n---\n", "---\r\n"))
def test_split_frontmatter_rejects_invalid_leading_markers(leading: str) -> None:
    """The opening marker is anchored at byte zero with an exact newline."""
    with pytest.raises(AgentFrontmatterError, match="bad.md"):
        split_frontmatter(leading + _document().removeprefix("---\n"), source="bad.md")


def test_split_frontmatter_rejects_missing_terminator_with_source() -> None:
    """An unterminated leading block fails with the supplied source name."""
    with pytest.raises(AgentFrontmatterError, match="unterminated.md"):
        split_frontmatter("---\nname: example\n", source="unterminated.md")


def test_split_frontmatter_rejects_empty_frontmatter_block() -> None:
    """An immediate closing marker cannot produce an empty field mapping."""
    with pytest.raises(AgentFrontmatterError, match="frontmatter has no fields"):
        split_frontmatter("---\n---\n", source="empty.md")


def test_split_frontmatter_treats_nonexact_closing_marker_as_malformed_field() -> None:
    """A nonexact closing marker is consumed as a malformed field, never a terminator."""
    malformed = _document().replace("---\nBody\n", "--- \nBody\n")
    with pytest.raises(AgentFrontmatterError, match="frontmatter field is malformed"):
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
        "name:  example\n",
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
    malformed = _document().replace("name: example\n", line, 1)
    with pytest.raises(AgentFrontmatterError, match="frontmatter field"):
        split_frontmatter(malformed, source="malformed.md")


@pytest.mark.parametrize("prefix", tuple("-?:,[]{}#&*!|>'\"%@`"))
def test_split_frontmatter_rejects_yaml_value_indicators(prefix: str) -> None:
    """Every YAML indicator or reserved prefix is closed at scalar value position."""
    malformed = _document().replace("name: example\n", f"name: {prefix}example\n", 1)
    with pytest.raises(AgentFrontmatterError, match="quoted or structured"):
        split_frontmatter(malformed, source="yaml.md")


def test_split_frontmatter_allows_embedded_colons_in_plain_values() -> None:
    """A colon after the first value character remains ordinary scalar content."""
    fields, _body = split_frontmatter(
        _document({**_fields(), "description": "Example: embedded: colons"}),
        source="embedded-colons.md",
    )
    assert fields["description"] == "Example: embedded: colons"


@pytest.mark.parametrize(
    "value",
    ("example ", "example # comment", "example\tvalue", "example\x7fvalue"),
    ids=("trailing-space", "inline-comment", "tab", "delete-control"),
)
def test_split_frontmatter_rejects_ambiguous_or_control_scalar_values(value: str) -> None:
    """Trailing space, inline YAML comments, and controls cannot enter scalar values."""
    malformed = _document().replace("name: example\n", f"name: {value}\n", 1)
    with pytest.raises(AgentFrontmatterError, match="frontmatter field value is malformed"):
        split_frontmatter(malformed, source="scalar.md")


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


def test_parse_frontmatter_rejects_invalid_utf8_with_source_named_reason(tmp_path: Path) -> None:
    """Path-backed parsing refuses invalid bytes without an undecoded fallback."""
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    with pytest.raises(
        AgentFrontmatterError,
        match="invalid.md: agent definition cannot be read as UTF-8",
    ):
        parse_frontmatter(invalid)


_CONSUMER_IMPORTS = {
    "test_agent_worktree_controls.py": frozenset({"AGENTS_DIR", "agent_files", "agent_tools"}),
    "test_agent_model_pins.py": frozenset(
        {"AGENTS_DIR", "agent_body", "agent_files", "parse_frontmatter"}
    ),
}
_CONSUMER_HELPERS = {
    "test_agent_worktree_controls.py": frozenset(
        {
            "_canonical_block_mismatch",
            "_discipline_section",
            "_expected_variant",
            "_has_runbook_line_citation",
            "_javascript_agent_calls",
            "_normalize_section_separator",
            "_role_backed_agent_calls",
            "_shared_checkout_direction",
        }
    ),
    "test_agent_model_pins.py": frozenset(
        {
            "_assert_live_planning_and_assurance_pins",
            "_model_family",
            "_self_identified_families",
        }
    ),
}


def _consumer_ast(source: str, filename: str) -> ast.Module:
    """Parse one governed test consumer as Python source."""
    return ast.parse(source, filename=filename)


def _string_bindings(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Return every literal string binding for each name without execution."""
    bindings: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, set()).add(node.value.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            bindings.setdefault(node.target.id, set()).add(node.value.value)
    return {name: frozenset(values) for name, values in bindings.items()}


def _static_strings(node: ast.expr, bindings: dict[str, frozenset[str]]) -> frozenset[str]:
    """Resolve direct or bound literal strings without evaluating code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.Name):
        return bindings.get(node.id, frozenset())
    return frozenset()


def _valid_inline_flags(flags: str) -> bool:
    """Return whether one leading inline regex flag group is syntactically closed."""
    enabled, minus, disabled = flags.partition("-")
    valid_flags = frozenset("aiLmsux")
    return (
        bool(enabled)
        and set(enabled) <= valid_flags
        and len(set(enabled)) == len(enabled)
        and (not minus or (bool(disabled) and set(disabled) <= valid_flags))
        and len(set(disabled)) == len(disabled)
        and not (set(enabled) & set(disabled))
    )


def _is_frontmatter_marker_value(value: str) -> bool:
    """Return whether a value is an exact marker or valid regex-marker prefix."""
    bare_markers = frozenset({"---", "---\n", "---\\n"})
    regex_prefixes = ("^---\n", "^---\\n")
    if value in bare_markers or value.startswith(regex_prefixes):
        return True
    inline_flags, separator, suffix = value.partition(")")
    if (
        not inline_flags.startswith("(?")
        or separator != ")"
        or not suffix.startswith(regex_prefixes)
    ):
        return False
    return _valid_inline_flags(inline_flags[2:])


def _is_frontmatter_marker(node: ast.expr, bindings: dict[str, frozenset[str]]) -> bool:
    """Return whether any literal value for an expression denotes a marker."""
    return any(_is_frontmatter_marker_value(value) for value in _static_strings(node, bindings))


def _first_call_value(node: ast.Call) -> tuple[ast.expr, ...]:
    """Return the first positional operand for an API with positional-only input."""
    return tuple(node.args[:1])


def _pattern_call_values(node: ast.Call) -> tuple[ast.expr, ...]:
    """Return a parser pattern from its first positional or explicit ``pattern=`` argument."""
    return (
        *_first_call_value(node),
        *(keyword.value for keyword in node.keywords if keyword.arg == "pattern"),
    )


@pytest.mark.parametrize(
    "value",
    ("----", "^----", "(?m)^----", "plain --- text", "(?m)^## heading", "(?i)role body"),
)
def test_frontmatter_marker_detection_ignores_nonmarkers(value: str) -> None:
    """Bare and inline-flag detection remains specific to the frontmatter marker."""
    assert not _is_frontmatter_marker(ast.Constant(value=value), {})


@pytest.mark.parametrize(
    "value",
    (
        "---",
        "---\n",
        "---\\n",
        "^---\n",
        "^---\\n",
        "(?m)^---\n",
        "(?m)^---\\n",
        "(?im-sx)^---\\n",
        r"^---\n(.*?)\n---\n",
        r"(?m)^---\n(.*?)\n---\n",
    ),
)
def test_frontmatter_marker_detection_accepts_exact_bare_and_regex_forms(value: str) -> None:
    """Only exact actual or escaped-newline frontmatter markers are admitted."""
    assert _is_frontmatter_marker(ast.Constant(value=value), {})


def test_consumer_guard_ignores_marker_like_unrelated_keyword_values() -> None:
    """Only parser patterns, glob patterns, and startswith prefixes are inspected."""
    source = (_REPO / "tests" / "test_agent_model_pins.py").read_text()
    unrelated_startswith = "".join(
        (
            '\nfrontmatter_text = "role body"\n',
            'frontmatter_text.startswith("ordinary", start="---\\n")\n',
        )
    )
    mutated = (
        source
        + (
            '\nre.compile("ordinary", flags="^---\\n")\n'
            'roster_directory = Path(".")\nroster_directory.glob("ordinary", recursive="*.md")\n'
        )
        + unrelated_startswith
    )
    _assert_consumer_uses_shared_agent_defs(mutated, "test_agent_model_pins.py")


_REGEX_FRONTMATTER_CALLS = frozenset({"compile", "match", "search", "fullmatch"})


def _regex_import_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Resolve names bound to the ``re`` module and its parser callables."""
    module_names: set[str] = set()
    function_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re":
                    module_names.add(alias.asname or "re")
        elif isinstance(node, ast.ImportFrom) and node.module == "re" and node.level == 0:
            for alias in node.names:
                if alias.name == "*":
                    function_names.update(_REGEX_FRONTMATTER_CALLS)
                elif alias.name in _REGEX_FRONTMATTER_CALLS:
                    function_names.add(alias.asname or alias.name)
    return module_names, function_names


def _is_regex_frontmatter_call(
    node: ast.Call, module_names: set[str], function_names: set[str]
) -> bool:
    """Return whether a call resolves to an imported ``re`` parser callable."""
    function = node.func
    if isinstance(function, ast.Name):
        return function.id in function_names
    return (
        isinstance(function, ast.Attribute)
        and function.attr in _REGEX_FRONTMATTER_CALLS
        and isinstance(function.value, ast.Name)
        and function.value.id in module_names
    )


def _assert_consumer_uses_shared_agent_defs(source: str, filename: str) -> None:
    """Fail closed when a consumer restores local roster or parser machinery."""
    expected_imports = _CONSUMER_IMPORTS[filename]
    expected_helpers = _CONSUMER_HELPERS[filename]
    tree = _consumer_ast(source, filename)
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "_agent_defs" and node.level == 0
    ]
    assert len(imports) == 1, f"{filename}: expected one shared _agent_defs import"
    imported = imports[0]
    assert all(alias.asname is None for alias in imported.names), (
        f"{filename}: _agent_defs imports must not be aliased"
    )
    assert {alias.name for alias in imported.names} == expected_imports, (
        f"{filename}: _agent_defs imports drifted from the shared-consumer contract"
    )

    helpers = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("test_")
    }
    assert helpers == expected_helpers, (
        f"{filename}: non-test top-level helpers drifted from the closed helper set"
    )

    bindings = _string_bindings(tree)
    regex_modules, regex_functions = _regex_import_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "glob" and any(
                "*.md" in _static_strings(argument, bindings)
                for argument in _pattern_call_values(node)
            ):
                raise AssertionError(f"{filename}: local Markdown roster glob is forbidden")
            if node.func.attr == "startswith" and any(
                _is_frontmatter_marker(argument, bindings) for argument in _first_call_value(node)
            ):
                raise AssertionError(f"{filename}: local leading-frontmatter parser is forbidden")
        if isinstance(node, ast.Compare) and any(
            _is_frontmatter_marker(operand, bindings) for operand in (node.left, *node.comparators)
        ):
            raise AssertionError(f"{filename}: local leading-frontmatter parser is forbidden")
        if (
            isinstance(node, ast.Call)
            and _is_regex_frontmatter_call(node, regex_modules, regex_functions)
            and any(
                _is_frontmatter_marker(argument, bindings)
                for argument in _pattern_call_values(node)
            )
        ):
            raise AssertionError(f"{filename}: local leading-frontmatter parser is forbidden")


def test_consuming_guards_use_only_the_shared_roster_and_parser() -> None:
    """Neither consumer may restore a local roster glob or frontmatter/body parser."""
    for filename in _CONSUMER_IMPORTS:
        _assert_consumer_uses_shared_agent_defs((_REPO / "tests" / filename).read_text(), filename)


def _rename_consumer_helper(source: str) -> str:
    """Mutate a consumer helper name without changing its implementation."""
    return source.replace("def _discipline_section", "def _renamed_local_parser", 1)


def _add_reformatted_roster_glob(source: str) -> str:
    """Add a formatting-insensitive local Markdown roster glob mutation."""
    return (
        source + '\nroster_directory = Path(".claude/agents")\nroster_directory . glob ( "*.md" )\n'
    )


def _add_keyword_roster_glob(source: str) -> str:
    """Add a keyword-argument local Markdown roster glob mutation."""
    return source + (
        '\nroster_directory = Path(".claude/agents")\nroster_directory.glob(pattern="*.md")\n'
    )


def _add_startswith_frontmatter_parser(source: str) -> str:
    """Add a direct leading-frontmatter marker parser mutation."""
    return source + '\nfrontmatter_text = "role body"\nfrontmatter_text.startswith("---\\n")\n'


def _add_bare_frontmatter_marker_check(source: str) -> str:
    """Add a bare closing-marker comparison mutation."""
    return source + '\nfrontmatter_text = "role body"\nfrontmatter_text == "---"\n'


def _add_raw_regex_frontmatter_parser(source: str) -> str:
    """Restore the historical raw-regex frontmatter parser mutation."""
    return source + '\nlocal_frontmatter = re.compile(r"^---\\n")\n'


def _add_historical_full_regex_frontmatter_parser(source: str) -> str:
    """Restore the complete historical frontmatter regex parser mutation."""
    return source + '\nlocal_frontmatter = re.compile(r"^---\\n(.*?)\\n---\\n", re.S)\n'


def _add_inline_historical_full_regex_frontmatter_parser(source: str) -> str:
    """Restore the inline-flag spelling of the complete historical parser."""
    return source + '\nlocal_frontmatter = re.compile(r"(?m)^---\\n(.*?)\\n---\\n", re.S)\n'


def _add_keyword_regex_frontmatter_parser(source: str) -> str:
    """Add a keyword-argument regex frontmatter parser mutation."""
    return source + '\nlocal_frontmatter = re.compile(pattern=r"^---\\n(.*?)\\n---\\n")\n'


def _add_use_then_rebind_frontmatter_pattern(source: str) -> str:
    """Bind a marker pattern, use it, then rebind safely to prove no hiding."""
    return source + (
        '\nfrontmatter_pattern = r"^---\\n(.*?)\\n---\\n"\n'
        "re.compile(frontmatter_pattern)\n"
        'frontmatter_pattern = "ordinary text"\n'
    )


def _add_direct_import_regex_frontmatter_parser(source: str) -> str:
    """Add a direct-import ``re`` parser mutation without name guessing."""
    return source + '\nfrom re import match\nmatch(r"^---\\n", "role body")\n'


def _add_module_alias_regex_frontmatter_parser(source: str) -> str:
    """Add a module-alias ``re`` parser mutation."""
    return source + '\nimport re as local_regex\nlocal_regex.compile(r"^---\\n")\n'


def _add_function_alias_regex_frontmatter_parser(source: str) -> str:
    """Add an aliased direct-import ``re`` parser mutation."""
    return source + (
        '\nfrom re import search as local_search\nlocal_search(r"^---\\n", "role body")\n'
    )


def _add_star_import_regex_frontmatter_parser(source: str) -> str:
    """Add a star-imported ``re`` parser mutation."""
    return source + '\nfrom re import *\nmatch(r"^---\\n", "role body")\n'


def _add_inline_flag_regex_frontmatter_parser(source: str) -> str:
    """Add a leading-inline-flag regex frontmatter parser mutation."""
    return source + '\nlocal_frontmatter = re.compile(r"(?m)^---\\n")\n'


_CONSUMER_MUTATIONS: tuple[tuple[str, Callable[[str], str], str], ...] = (
    (
        "test_agent_worktree_controls.py",
        _rename_consumer_helper,
        "non-test top-level helpers",
    ),
    (
        "test_agent_worktree_controls.py",
        _add_reformatted_roster_glob,
        "local Markdown roster glob",
    ),
    (
        "test_agent_worktree_controls.py",
        _add_keyword_roster_glob,
        "local Markdown roster glob",
    ),
    (
        "test_agent_model_pins.py",
        _add_startswith_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_bare_frontmatter_marker_check,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_raw_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_historical_full_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_inline_historical_full_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_keyword_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_use_then_rebind_frontmatter_pattern,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_direct_import_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_module_alias_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_function_alias_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_star_import_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
    (
        "test_agent_model_pins.py",
        _add_inline_flag_regex_frontmatter_parser,
        "local leading-frontmatter parser",
    ),
)


@pytest.mark.parametrize(
    ("filename", "mutation", "expected"),
    _CONSUMER_MUTATIONS,
    ids=(
        "renamed-helper",
        "reformatted-roster-glob",
        "keyword-roster-glob",
        "startswith-parser",
        "bare-marker-check",
        "raw-regex-parser",
        "historical-full-regex-parser",
        "inline-historical-full-regex-parser",
        "keyword-regex-parser",
        "use-then-rebind-regex-parser",
        "direct-import-regex-parser",
        "module-alias-regex-parser",
        "function-alias-regex-parser",
        "star-import-regex-parser",
        "inline-flag-regex-parser",
    ),
)
def test_consuming_guard_rejects_local_parser_and_roster_mutations(
    filename: str, mutation: Callable[[str], str], expected: str
) -> None:
    """Structural guard rejects renamed helpers and syntax-insensitive bypasses."""
    source = (_REPO / "tests" / filename).read_text()
    mutated = mutation(source)
    with pytest.raises(AssertionError, match=expected):
        _assert_consumer_uses_shared_agent_defs(mutated, filename)


def test_consuming_guard_does_not_guess_unbound_regex_function_provenance() -> None:
    """An unrelated nested function named ``match`` is not treated as ``re.match``."""
    source = (_REPO / "tests" / "test_agent_model_pins.py").read_text()
    mutated = source + (
        "\ndef test_unrelated_match_name() -> None:\n"
        "    def match(pattern: str, text: str) -> None:\n"
        "        del pattern, text\n"
        '    match(r"^---\\n", "role body")\n'
    )
    _assert_consumer_uses_shared_agent_defs(mutated, "test_agent_model_pins.py")


def test_consuming_guard_ignores_rebound_nonmarker_patterns() -> None:
    """All-literal binding resolution does not invent a marker from safe values."""
    source = (_REPO / "tests" / "test_agent_model_pins.py").read_text()
    mutated = source + (
        '\nfrontmatter_pattern = "^----"\n'
        "re.compile(frontmatter_pattern)\n"
        'frontmatter_pattern = "(?m)^----"\n'
    )
    _assert_consumer_uses_shared_agent_defs(mutated, "test_agent_model_pins.py")


@pytest.mark.parametrize(
    "source",
    (
        'local_regex = object()\nlocal_regex.compile(r"^---\\n")\n',
        're = object()\nre.match(r"^---\\n", "role body")\n',
    ),
    ids=("unimported-attribute", "shadowed-re-attribute"),
)
def test_regex_frontmatter_call_requires_an_import_binding_for_attributes(source: str) -> None:
    """Unimported or shadowed attribute calls cannot impersonate ``re`` parser calls."""
    tree = _consumer_ast(source, "negative-control.py")
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    module_names, function_names = _regex_import_bindings(tree)
    assert module_names == function_names == set()
    assert not _is_regex_frontmatter_call(call, module_names, function_names)
