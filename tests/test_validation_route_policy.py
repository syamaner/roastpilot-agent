"""Guard D218's declared policy, without classifying changes or proving compliance."""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HEADING = "### Validation routes (D218)"
_HEADER = (
    "| Route | Trigger | Local validation | Witness proof | Mandatory added reviewer | Hosted |"
)
_FULL_HOSTED = frozenset({"ordinary", "serial", "stress", "packaging", "web"})
_CONTROLS = frozenset(
    {
        "exact-commit-evidence",
        "independent-risk-routed-review",
        "feedback-triage",
        "existing-coverage-gates",
        "protected-checks",
        "automatic-ready-head-Codex-boundary-and-wait",
        "conversation-resolution",
        "normal-protected-merge",
    }
)


class RouteId(Enum):
    """Closed identifiers for declared validation routes."""

    DOCS_ONLY = "DOCS_ONLY"
    ROUTINE = "ROUTINE"
    SAFETY_CRITICAL = "SAFETY_CRITICAL"
    GATE_MACHINERY = "GATE_MACHINERY"


@dataclass(frozen=True)
class Row:
    """One declared route's finite triggers and obligations."""

    trigger: frozenset[str]
    local: frozenset[str]
    witness: frozenset[str]
    reviewers: frozenset[str]
    hosted: frozenset[str]


def _tokens(cell: str) -> frozenset[str]:
    if not cell.startswith("`") or not cell.endswith("`"):
        raise ValueError("cells must contain a closed token list")
    tokens = [part.strip() for part in cell[1:-1].split(";")]
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", token) for token in tokens):
        raise ValueError("malformed token")
    if len(tokens) != len(set(tokens)):
        raise ValueError("duplicate token")
    return frozenset(tokens)


def _parse_rows(text: str) -> dict[RouteId, Row]:
    sections = text.split(_HEADING)
    if len(sections) != 2:
        raise ValueError("missing or duplicate policy section")
    section = sections[1].split("\n### ", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    if lines[:2] != [_HEADER, "|---|---|---|---|---|---|"]:
        raise ValueError("missing or malformed table")
    rows: dict[RouteId, Row] = {}
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if not line.endswith("|") or len(cells) != 6:
            raise ValueError("malformed row")
        route = RouteId(cells[0])
        if route in rows:
            raise ValueError("duplicate route")
        rows[route] = Row(*(_tokens(cell) for cell in cells[1:]))
    if set(rows) != set(RouteId):
        raise ValueError("missing route")
    return rows


def _agents() -> str:
    return (_ROOT / "AGENTS.md").read_text(encoding="utf-8")


def _record(text: str, label: str) -> str:
    prefix = f"**{label}:** `"
    lines = [line for line in text.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, f"missing or duplicate {label}"
    assert lines[0].endswith("`"), f"malformed {label}"
    return lines[0][len(prefix) : -1]


def _mapping(record: str) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for item in record.split("; "):
        assert item.count("=") == 1, "malformed mapping entry"
        key, value = item.split("=")
        assert key not in result, "duplicate mapping key"
        values = value.split("+")
        assert key and all(values), "empty mapping key or value"
        assert len(values) == len(set(values)), "repeated mapping value"
        result[key] = frozenset(values)
    return result


def _parse_composition(record: str) -> dict[frozenset[RouteId], frozenset[RouteId]]:
    result: dict[frozenset[RouteId], frozenset[RouteId]] = {}
    for key, values in _mapping(record).items():
        parts = key.split("+")
        assert len(parts) == 2 and parts[0] != parts[1], "invalid composition pair"
        try:
            inputs = frozenset(RouteId(part) for part in parts)
            selected = frozenset(RouteId(part) for part in values)
        except ValueError as error:
            raise ValueError("unknown composition route") from error
        assert inputs not in result, "duplicate composition pair"
        result[inputs] = selected
    return result


def _full_obligations(row: Row) -> frozenset[str]:
    return frozenset(
        {
            *(token for token in row.local if token == "full"),
            *("passed-witness" for token in row.witness if token == "passed"),
            *(token for token in row.reviewers if token == "safety-reviewer"),
        }
    )


def _assert_activation(text: str) -> None:
    assert _mapping(_record(text, "Activation obligations")) == {
        "until-agent-merge": frozenset({"universal-full"}),
        "policy-implementation": frozenset({"GATE_MACHINERY", "full", "passed-witness"}),
    }, "activation must retain universal full and forbid policy self-exemption"


def _assert_adjudication(text: str) -> None:
    assert _mapping(_record(text, "Adjudication obligations")) == {
        "assessment": frozenset({"current-head", "independent-review"}),
        "semantic-risk": frozenset({"no-filename-inference", "no-automatic-classifier"}),
    }, "adjudication must remain current, independent and semantic"


def test_docs_route_requires_the_trusted_classifier() -> None:
    """Docs validation is bound to the existing trusted classifier result."""
    row = _parse_rows(_agents())[RouteId.DOCS_ONLY]
    assert row.trigger == {"trusted-classifier-docs-only"}
    assert row.local == {"docs-consumers", "governance", "applicable-statics", "focused-not-full"}


def test_routine_route_retains_focused_behaviour_and_contracts() -> None:
    """Ordinary behavioural tests alone do not become gate machinery."""
    rows = _parse_rows(_agents())
    assert rows[RouteId.ROUTINE].trigger == {
        "application-behaviour",
        "cross-boundary-contract",
        "ordinary-behavioural-tests",
    }
    assert rows[RouteId.ROUTINE].local == {
        "focused-behaviour",
        "cross-boundary-contracts",
        "statics",
        "focused-not-full",
    }
    assert rows[RouteId.GATE_MACHINERY].trigger == {
        "test-selection",
        "QA-wrapper",
        "execution-environment",
        "gate-machinery",
    }


def test_safety_route_requires_full_and_safety_review() -> None:
    """Safety-relevant capabilities retain full validation and safety review."""
    row = _parse_rows(_agents())[RouteId.SAFETY_CRITICAL]
    assert row.trigger == {
        "safety",
        "controller",
        "recovery",
        "hardware-command-path",
        "safety-relevant-runtime-capability",
    }
    assert row.local == {"full"}
    assert row.reviewers == {"safety-reviewer", "risk-routed"}
    assert row.witness == {"not-required"}


def test_gate_route_requires_full_and_passed_witness() -> None:
    """Gate machinery cannot qualify using focused evidence or a skipped witness."""
    row = _parse_rows(_agents())[RouteId.GATE_MACHINERY]
    assert row.local == {"full"}
    assert row.witness == {"passed"}
    assert row.reviewers == {"risk-routed"}


def test_fallback_and_known_composition_preserve_union_obligations() -> None:
    """Known combinations and uncertain assessments have distinct obligations."""
    text = _agents()
    rows = _parse_rows(text)
    fallback = _mapping(_record(text, "Fallback obligations"))
    assert _full_obligations(rows[RouteId.SAFETY_CRITICAL]) <= fallback["uncertain-safety"], (
        "uncertain safety must retain every explicit safety-route obligation"
    )
    assert (
        _full_obligations(rows[RouteId.GATE_MACHINERY]) <= fallback["uncertain-gate-machinery"]
    ), "uncertain gate machinery must retain every explicit gate-route obligation"
    assert fallback == {
        **{
            key: frozenset({"full"})
            for key in ("missing", "stale", "unknown", "unreviewed", "conflicting", "ambiguous")
        },
        "uncertain-gate-machinery": frozenset({"full", "passed-witness"}),
        "uncertain-safety": frozenset({"full", "safety-reviewer"}),
    }
    # Independent expectations prevent a mutually consistent but narrowed declaration.
    expected_breadth = {
        RouteId.DOCS_ONLY: 0,
        RouteId.ROUTINE: 1,
        RouteId.SAFETY_CRITICAL: 2,
        RouteId.GATE_MACHINERY: 2,
    }
    declared_breadth = _mapping(_record(text, "Breadth"))
    assert declared_breadth == {
        route.value: frozenset({str(rank)}) for route, rank in expected_breadth.items()
    }, "breadth must retain the approved independent ordering"
    breadth = {
        RouteId(route): int(next(iter(values))) for route, values in declared_breadth.items()
    }
    assert _mapping(_record(text, "Composition rule")) == {
        "known": frozenset({"broadest", "union"})
    }
    composition = _parse_composition(_record(text, "Composition"))
    expected_pairs = {
        frozenset({RouteId.DOCS_ONLY, RouteId.ROUTINE}),
        frozenset({RouteId.ROUTINE, RouteId.SAFETY_CRITICAL}),
        frozenset({RouteId.ROUTINE, RouteId.GATE_MACHINERY}),
        frozenset({RouteId.SAFETY_CRITICAL, RouteId.GATE_MACHINERY}),
    }
    assert set(composition) == expected_pairs
    for inputs, selected in composition.items():
        broadest = max(breadth[route] for route in inputs)
        assert selected == {route for route in inputs if breadth[route] == broadest}
    assert composition[frozenset({RouteId.DOCS_ONLY, RouteId.ROUTINE})] == {RouteId.ROUTINE}
    safety_gate = composition[frozenset({RouteId.SAFETY_CRITICAL, RouteId.GATE_MACHINERY})]
    assert frozenset[str]().union(*(_full_obligations(rows[r]) for r in safety_gate)) == {
        "full",
        "passed-witness",
        "safety-reviewer",
    }
    assert set(_record(text, "Assessment fields").split("; ")) == {
        "head-sha",
        "merge-base",
        "changed-paths",
        "touched-capabilities",
        "selected-route",
        "focused-tests-run",
        "cross-boundary-tests-run",
        "independent-reviewer",
        "reviewer-disposition",
    }


def test_only_full_routes_declare_full_validation() -> None:
    """A focused route cannot acquire the full-gate label."""
    rows = _parse_rows(_agents())
    for route, row in rows.items():
        if route in {RouteId.DOCS_ONLY, RouteId.ROUTINE}:
            assert "full" not in row.local
            assert "focused-not-full" in row.local
            assert row.witness == {"not-required"}
        else:
            assert row.local == {"full"}


def test_hosted_lanes_and_preserved_controls_are_unchanged() -> None:
    """Every route retains its existing hosted path and the common controls."""
    text = _agents()
    for route, row in _parse_rows(text).items():
        assert row.hosted == ({"docs-only"} if route is RouteId.DOCS_ONLY else _FULL_HOSTED)
        assert "risk-routed" in row.reviewers
    assert set(_record(text, "Preserved controls").split("; ")) == _CONTROLS


def test_full_definition_matches_configured_roots_and_has_no_selection() -> None:
    """The full gate still covers all configured roots in one native process."""
    text = _agents()
    commands = re.findall(r"^\.venv/bin/python -m pytest --cov=.*$", text, re.MULTILINE)
    assert len(commands) == 1
    argv = shlex.split(commands[0])
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage = config["tool"]["coverage"]["run"]
    assert coverage["branch"] is True
    assert argv == [
        ".venv/bin/python",
        "-m",
        "pytest",
        *(f"--cov={source}" for source in coverage["source"]),
        "--cov-branch",
        "--cov-report=term-missing",
    ]
    assert "all markers in one process" in text


@pytest.mark.docs
def test_instruction_consumers_are_consistent() -> None:
    """Consumers refer to the authoritative route policy rather than universal gates."""
    agents = _agents()
    preflight = (_ROOT / ".claude/skills/pr-preflight/SKILL.md").read_text(encoding="utf-8")
    planner = (_ROOT / ".claude/agents/story-planner.md").read_text(encoding="utf-8")
    runbook = (_ROOT / "docs/agent-team-worktrees.md").read_text(encoding="utf-8")
    assert not re.search(
        r"single process\) remains mandatory before handback and before opening a PR",
        " ".join(agents.split()),
    )
    section = preflight.split("## 1. Deterministic gates\n", 1)[1].split("\n## 2.", 1)[0]
    assert "AGENTS.md\n  Validation routes table" in section
    assert "current-head route assessment" in section
    assert "and `.venv/bin/python -m pytest`." not in section
    assert "Python coverage for full routes:" in section
    for source in ("roastpilot_agent", "scripts", ".agents/skills/capture-agent-usage/scripts"):
        assert f"--cov={source}" in section
    assert "route-applicable gates before\n   handback (AGENTS.md Validation routes)" in planner
    assert "route-applicable gates\n(AGENTS.md Validation routes)" in runbook


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("missing", "missing or duplicate policy section"),
        ("duplicate-section", "missing or duplicate policy section"),
        ("duplicate-row", "duplicate route"),
        ("unknown", "not a valid RouteId"),
        ("missing-row", "missing route"),
        ("malformed", "malformed row"),
        ("duplicate-token", "duplicate token"),
    ],
    ids=[
        "missing",
        "duplicate-section",
        "duplicate-row",
        "unknown",
        "missing-row",
        "malformed",
        "duplicate-token",
    ],
)
def test_route_parser_rejects_incomplete_or_ambiguous_tables(damage: str, reason: str) -> None:
    """Malformed policy documents fail before any obligation can be trusted."""
    text = _agents()
    line = next(line for line in text.splitlines() if line.startswith("| DOCS_ONLY |"))
    if damage == "missing":
        text = text.replace(_HEADING, "### No policy")
    elif damage == "duplicate-section":
        text += "\n" + _HEADING
    elif damage == "duplicate-row":
        text = text.replace(line, line + "\n" + line)
    elif damage == "unknown":
        text = text.replace("| DOCS_ONLY |", "| UNKNOWN |")
    elif damage == "missing-row":
        text = text.replace(line + "\n", "")
    elif damage == "malformed":
        text = text.replace(line, line.rstrip("|"))
    else:
        text = text.replace("`trusted-classifier-docs-only`", "`repeated; repeated`")
    with pytest.raises(ValueError, match=reason):
        _parse_rows(text)


def test_activation_and_adjudication_obligations_are_declared() -> None:
    """Policy metadata retains interim validation and independent scope assessment."""
    text = _agents()
    _assert_activation(text)
    _assert_adjudication(text)


@pytest.mark.parametrize(
    ("cell", "reason"),
    [("full", "cells must contain a closed token list"), ("`not a token`", "malformed token")],
)
def test_token_parser_rejects_invalid_cells(cell: str, reason: str) -> None:
    """Each finite cell rejection reports the intended grammar failure."""
    with pytest.raises(ValueError, match=reason):
        _tokens(cell)


@pytest.mark.parametrize(
    "header",
    [_HEADER.replace("Trigger", "Triggers"), _HEADER.replace("Route | Trigger", "Trigger | Route")],
)
def test_route_parser_rejects_changed_or_reordered_header(header: str) -> None:
    """Column names and order cannot silently change row interpretation."""
    with pytest.raises(ValueError, match="missing or malformed table"):
        _parse_rows(_agents().replace(_HEADER, header))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("**Rule:** `one`\n**Rule:** `two`", "missing or duplicate Rule"),
        ("**Rule:** `one", "malformed Rule"),
    ],
)
def test_record_parser_rejects_duplicate_or_unterminated_records(text: str, reason: str) -> None:
    """A record must have one complete authoritative value."""
    with pytest.raises(AssertionError, match=reason):
        _record(text, "Rule")


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("key=one; key=two", "duplicate mapping key"),
        ("key=", "empty mapping key or value"),
        ("=one", "empty mapping key or value"),
        ("key=one+one", "repeated mapping value"),
        ("key", "malformed mapping entry"),
    ],
)
def test_mapping_parser_rejects_ambiguous_values(record: str, reason: str) -> None:
    """Duplicate keys and incomplete or repeated values fail for their own reasons."""
    with pytest.raises(AssertionError, match=reason):
        _mapping(record)


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("UNKNOWN+ROUTINE=ROUTINE", "unknown composition route"),
        ("DOCS_ONLY+ROUTINE=UNKNOWN", "unknown composition route"),
        ("ROUTINE+ROUTINE=ROUTINE", "invalid composition pair"),
        ("DOCS_ONLY+ROUTINE=ROUTINE; ROUTINE+DOCS_ONLY=ROUTINE", "duplicate composition pair"),
    ],
)
def test_composition_parser_rejects_unknown_or_duplicate_routes(record: str, reason: str) -> None:
    """Composition keys and values remain closed route identifiers."""
    with pytest.raises((ValueError, AssertionError), match=reason):
        _parse_composition(record)


@pytest.mark.parametrize(
    ("label", "record", "reason"),
    [
        (
            "Activation obligations",
            "until-agent-merge=focused; policy-implementation=GATE_MACHINERY+full+passed-witness",
            "activation must retain",
        ),
        (
            "Activation obligations",
            "until-agent-merge=universal-full; policy-implementation=ROUTINE+full+passed-witness",
            "activation must retain",
        ),
        (
            "Activation obligations",
            "until-agent-merge=universal-full; policy-implementation=GATE_MACHINERY+passed-witness",
            "activation must retain",
        ),
        (
            "Activation obligations",
            "until-agent-merge=universal-full; policy-implementation=GATE_MACHINERY+full",
            "activation must retain",
        ),
        (
            "Adjudication obligations",
            "assessment=independent-review; "
            "semantic-risk=no-filename-inference+no-automatic-classifier",
            "adjudication must remain",
        ),
        (
            "Adjudication obligations",
            "assessment=current-head; semantic-risk=no-filename-inference+no-automatic-classifier",
            "adjudication must remain",
        ),
        (
            "Adjudication obligations",
            "assessment=current-head+independent-review; "
            "semantic-risk=filename-inference+no-automatic-classifier",
            "adjudication must remain",
        ),
        (
            "Adjudication obligations",
            "assessment=current-head+independent-review; "
            "semantic-risk=no-filename-inference+automatic-classifier",
            "adjudication must remain",
        ),
    ],
)
def test_obligation_guards_reject_weakened_declarations(
    label: str, record: str, reason: str
) -> None:
    """Finite negative declarations cannot silently exempt policy or scope review."""
    text = f"**{label}:** `{record}`"
    guard = _assert_activation if label == "Activation obligations" else _assert_adjudication
    with pytest.raises(AssertionError, match=reason):
        guard(text)


@pytest.mark.parametrize("damage", ["extra-cell", "separator"])
def test_route_parser_rejects_extra_cell_or_changed_separator(damage: str) -> None:
    """An intact trailing pipe cannot hide an extra cell or altered separator."""
    text = _agents()
    if damage == "extra-cell":
        line = next(line for line in text.splitlines() if line.startswith("| DOCS_ONLY |"))
        text = text.replace(line, line + " `extra` |")
        reason = "malformed row"
    else:
        text = text.replace("|---|---|---|---|---|---|", "|---|---|---|---|---|--|")
        reason = "missing or malformed table"
    with pytest.raises(ValueError, match=reason):
        _parse_rows(text)
