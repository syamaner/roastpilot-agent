"""Consistency guard for the agent-topology model/effort pins.

The topology (``docs/agent-topology.md``) pins each named subagent to a full
model ID with an explicit effort, and §12 requires a consistency mechanism when
that fact is duplicated across the agent frontmatters, ``AGENTS.md``, and the §4
table. This test is that mechanism: a single authoritative mapping that fails if
any agent frontmatter drifts (an alias creeps back, a model/effort changes
without updating the map, or a role is added/removed), plus a repo-side guard
against committed settings that could silently defeat the pins.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO / ".claude" / "agents"

# The single authoritative (model, effort) mapping for every named subagent.
# Any change to an agent's frontmatter must be reflected here, and vice versa.
_EXPECTED: dict[str, tuple[str, str]] = {
    "engineer-be": ("claude-sonnet-5", "high"),
    "engineer-fe": ("claude-sonnet-5", "high"),
    "mcp-contract-checker": ("claude-sonnet-5", "medium"),
    "sim-roast-runner": ("claude-sonnet-5", "medium"),
    "pr-triage": ("claude-sonnet-5", "high"),
    "product-auditor": ("claude-sonnet-5", "high"),
    "qa": ("claude-sonnet-5", "high"),
    "security-reviewer": ("claude-sonnet-5", "high"),
    "ui-reviewer": ("claude-sonnet-5", "high"),
    "safety-reviewer": ("claude-opus-5", "xhigh"),
    "planning-architect": ("claude-fable-5", "high"),
    "story-planner": ("claude-fable-5", "high"),
}
_ALIASES = {"sonnet", "opus", "fable", "haiku", "best", "default"}


def _frontmatter(path: Path) -> dict[str, str]:
    """Return the simple ``key: value`` frontmatter fields of an agent file."""
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path.name}: no YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _agent_files() -> list[Path]:
    return sorted(_AGENTS_DIR.glob("*.md"))


def test_every_expected_agent_exists() -> None:
    on_disk = {p.stem for p in _agent_files()}
    assert on_disk == set(_EXPECTED), (
        f"agent set drifted from the map: only-on-disk={on_disk - set(_EXPECTED)}, "
        f"only-in-map={set(_EXPECTED) - on_disk}"
    )


@pytest.mark.parametrize("path", _agent_files(), ids=lambda p: p.stem)
def test_agent_model_and_effort_match_the_map(path: Path) -> None:
    fm = _frontmatter(path)
    name = fm.get("name")
    assert name == path.stem, (
        f"{path.name}: frontmatter name {name!r} must equal the filename stem "
        f"{path.stem!r} — workflows resolve agents by name, so a mismatch or a "
        f"renamed role would silently pass the file-set check"
    )
    assert name in _EXPECTED, f"{name} is not in the authoritative pin map"
    expected_model, expected_effort = _EXPECTED[name]
    assert fm.get("model") == expected_model, (
        f"{name}: model is {fm.get('model')!r}, expected {expected_model!r}"
    )
    assert fm.get("effort") == expected_effort, (
        f"{name}: effort is {fm.get('effort')!r}, expected {expected_effort!r}"
    )


@pytest.mark.parametrize("path", _agent_files(), ids=lambda p: p.stem)
def test_no_agent_uses_a_floating_alias(path: Path) -> None:
    model = _frontmatter(path).get("model", "")
    assert model not in _ALIASES, f"{path.stem}: model {model!r} is a floating alias; pin a full ID"
    assert model.startswith("claude-"), f"{path.stem}: model {model!r} is not a full ID"


def test_agents_md_prose_names_the_full_ids() -> None:
    """AGENTS.md prose must name the pinned IDs and the two special role associations."""
    agents_md = (_REPO / "AGENTS.md").read_text()
    for full_id in {m for m, _ in _EXPECTED.values()}:
        assert full_id in agents_md, f"AGENTS.md does not mention the pinned id {full_id}"
    # Bind the two roles whose model/effort differ from the Sonnet-high default, so
    # the prose can't drift from the pins for them.
    assert re.search(r"claude-opus-5.{0,24}xhigh", agents_md, re.S), (
        "AGENTS.md must document safety-reviewer as claude-opus-5 at xhigh effort"
    )
    assert re.search(r"claude-fable-5(?:(?!claude-)[\s\S]){0,400}planning-architect", agents_md), (
        "AGENTS.md must associate planning-architect with the claude-fable-5 pin "
        "with no other model ID intervening — a bare mention check is satisfiable "
        "by the other Fable role's text"
    )
    # Bind story-planner to the Fable pin the same way: the two-Fable-roles
    # sentence must associate the full ID with the role, not merely mention both
    # somewhere — otherwise re-pinning story-planner alone in prose stays green
    # because planning-architect already satisfies the mention checks.
    assert re.search(r"claude-fable-5(?:(?!claude-)[\s\S]){0,400}story-planner", agents_md), (
        "AGENTS.md must associate story-planner with the claude-fable-5 pin (D152) "
        "with no other model ID intervening"
    )
    # Converse guard: the association checks above pass on the shared
    # two-Fable-roles sentence even if a LATER clause re-pins one role (e.g.
    # "story-planner (model: claude-opus-5)"). Forbid any non-Fable model ID
    # in the window straight after either role's name, anywhere in the prose.
    for role in ("planning-architect", "story-planner"):
        drift = re.search(
            rf"{role}(?:(?!claude-)[\s\S]){{0,80}}claude-(?!fable-5)[a-z0-9.-]+",
            agents_md,
        )
        found = drift.group(0) if drift else ""
        assert drift is None, (
            f"{role} appears re-associated with a non-Fable model in AGENTS.md prose: {found!r}"
        )


def test_topology_reference_table_rows_match_the_map() -> None:
    """Light regex guard over the §4 reference table's Claude rows.

    The module docstring has always claimed the §4 table as a guarded surface,
    but until D152 added rows there nothing actually read the file — a drifted
    table row (wrong model or effort text) stayed green. Cell-level regexes are
    deliberately loose about prose and strict about the pinned ID + effort.
    """
    topo = (_REPO / "docs" / "agent-topology.md").read_text()

    def cells(model: str, effort: str) -> str:
        return rf"[^\n|]*\|[^\n|]*`{model}`[^\n|]*\|[^\n|]*`{effort}`"

    rows = [
        (r"\| Planning architect" + cells("claude-fable-5", "high"), "planning-architect"),
        (r"\| Story planner" + cells("claude-fable-5", "high"), "story-planner"),
        (r"\| Safety/critical" + cells("claude-opus-5", "xhigh"), "safety reviewer"),
        (r"Claude fallback" + cells("claude-sonnet-5", "high"), "fallback implementer"),
        (r"\| Mechanical contract" + cells("claude-sonnet-5", "medium"), "mechanical checker"),
        (r"\| QA/product/security" + cells("claude-sonnet-5", "high"), "qa/product/security"),
        (r"\| Independent PR triage" + cells("claude-sonnet-5", "high"), "pr triage"),
    ]
    for row_re, role in rows:
        assert re.search(row_re, topo), (
            f"§4 reference-table row for {role} drifted from the authoritative pin map"
        )


def test_workflow_model_pins_are_full_ids() -> None:
    """The review-branch inline-stage pins must be full IDs, and the safety verifier
    must stay on the Opus tier so it can't silently overrule the always-Opus safety
    review by drifting to a lower tier."""
    mjs = (_REPO / ".claude" / "workflows" / "review-branch.mjs").read_text()

    review = re.search(r"const REVIEW_MODEL = '([^']+)'", mjs)
    assert review, "REVIEW_MODEL constant not found in review-branch.mjs"
    assert review.group(1) not in _ALIASES, f"REVIEW_MODEL {review.group(1)!r} is a floating alias"
    assert review.group(1) == "claude-sonnet-5", f"REVIEW_MODEL is {review.group(1)!r}"

    safety = re.search(r"const SAFETY_VERIFY_MODEL = '([^']+)'", mjs)
    assert safety, "SAFETY_VERIFY_MODEL constant not found in review-branch.mjs"
    assert safety.group(1) not in _ALIASES, (
        f"SAFETY_VERIFY_MODEL {safety.group(1)!r} is a floating alias"
    )
    assert safety.group(1) == "claude-opus-5", (
        f"SAFETY_VERIFY_MODEL is {safety.group(1)!r}, expected claude-opus-5 — a lower "
        f"tier lets the safety verifier discard an Opus safety finding"
    )

    assert "f.lens === 'safety'" in mjs, "verify stage no longer branches on the safety lens"
    assert "model: SAFETY_VERIFY_MODEL, effort: 'xhigh'" in mjs, (
        "the safety verify branch must use SAFETY_VERIFY_MODEL at effort xhigh"
    )


def test_committed_settings_do_not_defeat_the_pins() -> None:
    """Repo-side of the §5 override check, on GIT-TRACKED settings only.

    A committed ``availableModels`` that excludes a pinned model, or a committed
    ``CLAUDE_CODE_SUBAGENT_MODEL`` env override, would defeat the frontmatter pins.
    Only tracked files are inspected: a developer's local (gitignored) override is
    their own environment, not repository config, and must not fail the suite. The
    live-environment and organisation-policy side stays an operator verification.
    """
    import json
    import subprocess

    tracked = set(
        subprocess.run(
            ["git", "ls-files", ".claude"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    pinned_ids = {m for m, _ in _EXPECTED.values()}
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        if rel not in tracked:
            continue
        data = json.loads((_REPO / rel).read_text())
        env = data.get("env", {})
        assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env, (
            f"{rel} commits a CLAUDE_CODE_SUBAGENT_MODEL override that defeats every pin"
        )
        allowed = data.get("availableModels")
        if not allowed:
            continue
        for pinned in pinned_ids:
            # family alias (sonnet/opus/fable), exact id, or a prefix the id extends
            family = pinned.rsplit("-", 1)[0].rsplit("-", 1)[-1]
            permitted = family in allowed or any(
                pinned == e or pinned.startswith(e + "-") for e in allowed
            )
            assert permitted, f"{rel} availableModels {allowed} does not permit pinned id {pinned}"
