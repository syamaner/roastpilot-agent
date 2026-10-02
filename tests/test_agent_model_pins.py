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

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest
from _agent_defs import AGENTS_DIR, agent_body, agent_files, agent_text, parse_frontmatter

# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

_REPO = Path(__file__).resolve().parents[1]
_CODEX_DIR = _REPO / ".codex"
_PROJECT_DOC_MAX_BYTES = 131072

# The single authoritative (model, effort) mapping for every named subagent.
# Any change to an agent's frontmatter must be reflected here, and vice versa.
_EXPECTED: dict[str, tuple[str, str]] = {
    "engineer-be": ("claude-opus-5-5", "medium"),
    "engineer-fe": ("claude-opus-5-5", "medium"),
    "mcp-contract-checker": ("claude-opus-5-5", "medium"),
    "sim-roast-runner": ("claude-opus-5-5", "medium"),
    "pr-triage": ("claude-opus-5-5", "medium"),
    "product-auditor": ("claude-opus-5-5", "medium"),
    "qa": ("claude-opus-5-5", "medium"),
    "security-reviewer": ("claude-opus-5-5", "medium"),
    "ui-reviewer": ("claude-opus-5-5", "medium"),
    "safety-reviewer": ("claude-opus-5-5", "high"),
    "planning-architect": ("claude-opus-5-5", "medium"),
    "story-planner": ("claude-opus-5-5", "medium"),
}
_ALIASES = {"sonnet", "opus", "fable", "haiku", "best", "default"}
_EXPECTED_CODEX: dict[str, tuple[str, str]] = {
    "engineer-be": ("gpt-6.1-sol", "medium"),
    "engineer-fe": ("gpt-6.1-sol", "medium"),
    "repair": ("gpt-6.1-sol", "medium"),
}
_LEGACY_CODEX_MODEL = "gpt-5.6-terra"
_CANONICAL_CODEX_PIN_SENTENCE = (
    "  and `repair`. Their files pin `gpt-6.1-sol` with `medium` reasoning for all\n"
    "  three roles (D206)."
)


def test_every_expected_agent_exists() -> None:
    on_disk = {p.stem for p in agent_files()}
    assert on_disk == set(_EXPECTED), (
        f"agent set drifted from the map: only-on-disk={on_disk - set(_EXPECTED)}, "
        f"only-in-map={set(_EXPECTED) - on_disk}"
    )


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_agent_model_and_effort_match_the_map(path: Path) -> None:
    fm = parse_frontmatter(path)
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


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_no_agent_uses_a_floating_alias(path: Path) -> None:
    model = parse_frontmatter(path).get("model", "")
    assert model not in _ALIASES, f"{path.stem}: model {model!r} is a floating alias; pin a full ID"
    assert model.startswith("claude-"), f"{path.stem}: model {model!r} is not a full ID"


_MODEL_FAMILIES = frozenset({"opus", "sonnet", "fable", "haiku"})
_SELF_IDENTIFICATION = re.compile(
    r"you are an? (?:claude )?(opus|sonnet|fable|haiku) model", re.IGNORECASE
)


def _model_family(model: str) -> str:
    """Return the family segment of a full ``claude-<family>-<major>[-<minor>]`` pin."""
    parts = model.split("-")
    assert len(parts) in (3, 4) and parts[0] == "claude" and parts[1] in _MODEL_FAMILIES, (
        f"model {model!r} is not a recognized full pinned id"
    )
    return parts[1]


def _self_identified_families(body: str) -> set[str]:
    """Return every model family a role body claims to itself be, lowercased."""
    return {match.group(1).lower() for match in _SELF_IDENTIFICATION.finditer(body)}


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_agent_body_never_self_identifies_as_an_inconsistent_model_family(path: Path) -> None:
    """A role body must never claim a model family other than its own frontmatter pin.

    The retired ``story-planner.md`` construction ("you are a Fable model") is
    the motivating case: its frontmatter pins ``claude-opus-5``, so a body claim
    of any OTHER family — including that exact retired sentence — must fail.
    """
    fm = parse_frontmatter(path)
    pinned_family = _model_family(fm["model"])
    claimed = _self_identified_families(agent_body(path))
    assert claimed <= {pinned_family}, (
        f"{path.stem}: body self-identifies as {sorted(claimed - {pinned_family})}, "
        f"inconsistent with its frontmatter pin family {pinned_family!r}"
    )


def test_self_identification_guard_detects_the_retired_fable_construction() -> None:
    """The guard helper itself must flag the exact retired construction if reintroduced."""
    sentence = "you are a Fable model and this contract is mandatory"
    assert _self_identified_families(sentence) == {"fable"}
    assert not ({"fable"} <= {_model_family("claude-opus-5")})


_CANONICAL_PLANNING_SENTENCE = (
    "- Planning: medium-effort `claude-opus-5-5` roles `planning-architect` for complex,\n"
    "  ambiguous, cross-repository, or safety-boundary design and `story-planner`\n"
    "  for the mandatory implementation contract before every delegated slice. Both\n"
    "  remain read-only."
)
_CANONICAL_ASSURANCE_SENTENCE = (
    "- Assurance: `qa`, `security-reviewer`, `ui-reviewer`,\n"
    "  `mcp-contract-checker`, and `sim-roast-runner` retain their existing pins and\n"
    "  lenses. `safety-reviewer` remains the mandatory `claude-opus-5-5`, `high`\n"
    "  safety floor."
)


def _assert_live_planning_and_assurance_pins(agents_md: str) -> None:
    """Require the complete canonical live-pin prose rather than nearby identifiers."""
    assert agents_md.count(_CANONICAL_PLANNING_SENTENCE) == 1
    assert agents_md.count(_CANONICAL_ASSURANCE_SENTENCE) == 1
    assert "claude-fable-5" not in agents_md


def test_agents_md_prose_uses_canonical_planning_and_assurance_sentences() -> None:
    """Live planning and safety pin prose must be exact and free of Fable drift."""
    agents_md = (_REPO / "AGENTS.md").read_text()
    for full_id in {m for m, _ in _EXPECTED.values()}:
        assert full_id in agents_md, f"AGENTS.md does not mention the pinned id {full_id}"
    _assert_live_planning_and_assurance_pins(agents_md)


@pytest.mark.parametrize(
    "decoy",
    [
        ("claude-opus-5", "claude-fable-5"),
        ("`high`\n  safety", "`medium`\n  safety"),
    ],
)
def test_agents_md_canonical_pin_guard_rejects_planning_and_safety_decoys(
    decoy: tuple[str, str],
) -> None:
    """A nearby or downgraded planning/safety pin cannot satisfy the prose guard."""
    agents_md = (_REPO / "AGENTS.md").read_text()
    source, replacement = decoy
    with pytest.raises(AssertionError):
        _assert_live_planning_and_assurance_pins(agents_md.replace(source, replacement, 1))


@pytest.mark.docs
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
        (r"\| Planning architect" + cells("claude-opus-5", "high"), "planning-architect"),
        (r"\| Story planner" + cells("claude-opus-5", "high"), "story-planner"),
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


def _assert_codex_project_agents(
    config_text: str, role_texts: Mapping[str, str], agents_md: str
) -> None:
    """Require the closed Codex project-agent registration and role shapes."""
    project_config = cast(dict[str, object], tomllib.loads(config_text))
    assert set(project_config) == {"project_doc_max_bytes", "agents"}
    project_doc_max_bytes = project_config["project_doc_max_bytes"]
    assert type(project_doc_max_bytes) is int
    assert project_doc_max_bytes == _PROJECT_DOC_MAX_BYTES
    raw_agents_config = project_config["agents"]
    assert isinstance(raw_agents_config, dict)
    agents_config = cast(dict[str, object], raw_agents_config)
    assert {key: value for key, value in agents_config.items() if not isinstance(value, dict)} == {
        "enabled": True,
        "max_concurrent_threads_per_session": 3,
    }

    registered_roles: dict[str, dict[str, object]] = {
        name: cast(dict[str, object], value)
        for name, value in agents_config.items()
        if isinstance(value, dict)
    }
    assert set(registered_roles) == set(_EXPECTED_CODEX)
    assert set(role_texts) == set(_EXPECTED_CODEX)
    assert "parent dispatches only the three registered named roles" in agents_md
    assert "Unnamed or default worker dispatch\n  is forbidden" in agents_md
    assert "Topology depth one remains mandatory\n  policy" in agents_md
    assert "Codex 0.147.0 V2 does not enforce parent depth through\n  `max_depth`" in agents_md
    assert (
        "Codex 0.147.0 V2 runtime verification established that\n"
        "  each leaf's `agents.enabled = false` removes spawn capability" in agents_md
    )

    for name, role_text in role_texts.items():
        data = tomllib.loads(role_text)
        model, effort = _EXPECTED_CODEX[name]
        registration = registered_roles[name]
        assert set(registration) == {"description", "config_file"}
        assert isinstance(registration["description"], str)
        assert registration["description"].strip()
        assert registration["config_file"] == f"agents/{name}.toml"
        assert set(data) == {"model", "model_reasoning_effort", "developer_instructions", "agents"}
        assert data["model"] == model
        assert data["model_reasoning_effort"] == effort
        assert data["agents"] == {"enabled": False}
        instructions = data["developer_instructions"]
        assert "do not spawn agents" in instructions.lower()
        assert "invoke Claude Code or any other model" in instructions


def _assert_codex_pin_prose(agents_md: str) -> None:
    """Require the canonical D206 pin declaration without legacy drift."""
    assert agents_md.count(_CANONICAL_CODEX_PIN_SENTENCE) == 1
    assert f"Their files pin `{_LEGACY_CODEX_MODEL}`" not in agents_md


def test_codex_project_agents_are_bounded_and_pinned() -> None:
    """Project Codex roles are registered, bounded, leaf-only, and pinned."""
    role_texts = {
        path.stem: path.read_text() for path in sorted((_CODEX_DIR / "agents").glob("*.toml"))
    }
    agents_md = (_REPO / "AGENTS.md").read_text()
    _assert_codex_project_agents((_CODEX_DIR / "config.toml").read_text(), role_texts, agents_md)
    _assert_codex_pin_prose(agents_md)


@pytest.mark.parametrize(
    ("case", "config_mutation", "role_mutation", "agents_mutation"),
    [
        ("config-parent-model", lambda text: 'model = "default"\n' + text, None, None),
        (
            "config-unknown-key",
            lambda text: 'sandbox_mode = "danger-full-access"\n' + text,
            None,
            None,
        ),
        (
            "config-doc-bytes",
            lambda text: text.replace(
                "project_doc_max_bytes = 131072", "project_doc_max_bytes = 65536"
            ),
            None,
            None,
        ),
        (
            "config-threads-4",
            lambda text: text.replace(
                "max_concurrent_threads_per_session = 3", "max_concurrent_threads_per_session = 4"
            ),
            None,
            None,
        ),
        (
            "config-top-disabled",
            lambda text: text.replace("enabled = true", "enabled = false", 1),
            None,
            None,
        ),
        (
            "registration-extra",
            lambda text: (
                text
                + '\n[agents.extra]\ndescription = "Extra"\nconfig_file = "agents/extra.toml"\n'
            ),
            None,
            None,
        ),
        (
            "registration-missing",
            lambda text: text.replace(
                "\n[agents.repair]\n"
                'description = "Lead-directed repair worker."\n'
                'config_file = "agents/repair.toml"\n',
                "\n",
            ),
            None,
            None,
        ),
        (
            "role-file-missing",
            None,
            lambda texts: {k: v for k, v in texts.items() if k != "repair"},
            None,
        ),
        (
            "role-file-extra",
            None,
            lambda texts: {**texts, "extra": texts["repair"]},
            None,
        ),
        (
            "agents-md-dispatch-phrase",
            None,
            None,
            lambda text: text.replace("Unnamed or default", "Unnamed", 1),
        ),
        (
            "registration-extra-key",
            lambda text: text.replace(
                'description = "Backend PR-slice implementer."',
                'description = "Backend PR-slice implementer."\nnickname = "be"',
            ),
            None,
            None,
        ),
        (
            "registration-empty-description",
            lambda text: text.replace(
                'description = "Backend PR-slice implementer."', 'description = ""'
            ),
            None,
            None,
        ),
        (
            "registration-path-drift",
            lambda text: text.replace(
                'config_file = "agents/engineer-be.toml"', 'config_file = "agents/wrong.toml"'
            ),
            None,
            None,
        ),
        (
            "role-unknown-key",
            None,
            lambda texts: {
                **texts,
                "engineer-be": 'sandbox_mode = "danger-full-access"\n' + texts["engineer-be"],
            },
            None,
        ),
        (
            "role-name-key",
            None,
            lambda texts: {**texts, "engineer-be": 'name = "be"\n' + texts["engineer-be"]},
            None,
        ),
        (
            "role-description-key",
            None,
            lambda texts: {**texts, "engineer-be": 'description = "be"\n' + texts["engineer-be"]},
            None,
        ),
        (
            "model-alias-default",
            None,
            lambda texts: {
                **texts,
                "repair": texts["repair"].replace(
                    f'model = "{_EXPECTED_CODEX["repair"][0]}"', 'model = "default"'
                ),
            },
            None,
        ),
        (
            "model-truncated",
            None,
            lambda texts: {
                **texts,
                "repair": texts["repair"].replace(
                    f'model = "{_EXPECTED_CODEX["repair"][0]}"', 'model = "gpt-6.1"'
                ),
            },
            None,
        ),
        (
            "model-legacy",
            None,
            lambda texts: {
                **texts,
                "engineer-fe": texts["engineer-fe"].replace(
                    f'model = "{_EXPECTED_CODEX["engineer-fe"][0]}"',
                    f'model = "{_LEGACY_CODEX_MODEL}"',
                ),
            },
            None,
        ),
        (
            "effort-high-be",
            None,
            lambda texts: {
                **texts,
                "engineer-be": texts["engineer-be"].replace(
                    'model_reasoning_effort = "medium"', 'model_reasoning_effort = "high"'
                ),
            },
            None,
        ),
        (
            "effort-low-repair",
            None,
            lambda texts: {
                **texts,
                "repair": texts["repair"].replace(
                    'model_reasoning_effort = "medium"', 'model_reasoning_effort = "low"'
                ),
            },
            None,
        ),
        (
            "leaf-spawn-enabled",
            None,
            lambda texts: {
                **texts,
                "repair": texts["repair"].replace("enabled = false", "enabled = true"),
            },
            None,
        ),
        (
            "leaf-agents-extra-key",
            None,
            lambda texts: {**texts, "engineer-be": texts["engineer-be"] + "max_depth = 2\n"},
            None,
        ),
        (
            "instructions-no-spawn-phrase",
            None,
            lambda texts: {
                **texts,
                "engineer-be": texts["engineer-be"].replace(
                    "Do not spawn agents", "Do not create leaves"
                ),
            },
            None,
        ),
        (
            "instructions-no-model-boundary",
            None,
            lambda texts: {
                **texts,
                "engineer-be": texts["engineer-be"].replace(
                    "invoke Claude Code or any other model", "invoke provider"
                ),
            },
            None,
        ),
    ],
    ids=[
        "config-parent-model",
        "config-unknown-key",
        "config-doc-bytes",
        "config-threads-4",
        "config-top-disabled",
        "registration-extra",
        "registration-missing",
        "role-file-missing",
        "role-file-extra",
        "agents-md-dispatch-phrase",
        "registration-extra-key",
        "registration-empty-description",
        "registration-path-drift",
        "role-unknown-key",
        "role-name-key",
        "role-description-key",
        "model-alias-default",
        "model-truncated",
        "model-legacy",
        "effort-high-be",
        "effort-low-repair",
        "leaf-spawn-enabled",
        "leaf-agents-extra-key",
        "instructions-no-spawn-phrase",
        "instructions-no-model-boundary",
    ],
)
def test_codex_project_agent_guard_rejects_drift(
    case: str,
    config_mutation: Callable[[str], str] | None,
    role_mutation: Callable[[dict[str, str]], dict[str, str]] | None,
    agents_mutation: Callable[[str], str] | None,
) -> None:
    """Every closed project-agent guard rejects its dedicated configuration drift."""
    config_text = (_CODEX_DIR / "config.toml").read_text()
    role_texts = {
        path.stem: path.read_text() for path in sorted((_CODEX_DIR / "agents").glob("*.toml"))
    }
    agents_md = (_REPO / "AGENTS.md").read_text()
    mutated_config = config_mutation(config_text) if config_mutation else config_text
    mutated_roles = role_mutation(role_texts) if role_mutation else role_texts
    mutated_agents = agents_mutation(agents_md) if agents_mutation else agents_md
    assert (mutated_config, mutated_roles, mutated_agents) != (
        config_text,
        role_texts,
        agents_md,
    ), case
    with pytest.raises(AssertionError):
        _assert_codex_project_agents(mutated_config, mutated_roles, mutated_agents)


@pytest.mark.parametrize(
    "agents_md",
    [
        (_REPO / "AGENTS.md")
        .read_text()
        .replace(_EXPECTED_CODEX["engineer-be"][0], _LEGACY_CODEX_MODEL, 1),
        (_REPO / "AGENTS.md")
        .read_text()
        .replace("`medium` reasoning for all", "`high` reasoning for all", 1),
    ],
    ids=["canonical-legacy-model", "canonical-high-effort"],
)
def test_codex_pin_prose_guard_rejects_decoys(agents_md: str) -> None:
    """Canonical D206 prose cannot be satisfied by a legacy model or high effort."""
    with pytest.raises(AssertionError):
        _assert_codex_pin_prose(agents_md)


def test_agents_md_fits_configured_project_document_budget() -> None:
    """AGENTS.md remains comfortably below the configured model-visible budget."""
    project_config = cast(
        dict[str, object], tomllib.loads((_CODEX_DIR / "config.toml").read_text())
    )
    project_doc_max_bytes = project_config["project_doc_max_bytes"]
    assert type(project_doc_max_bytes) is int
    assert project_doc_max_bytes == _PROJECT_DOC_MAX_BYTES
    assert (_REPO / "AGENTS.md").stat().st_size <= project_doc_max_bytes * 3 // 4


def test_codex_leaf_configs_pass_installed_strict_parse() -> None:
    """Each leaf config must pass the installed Codex strict-config parser."""
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("installed Codex CLI is unavailable")

    for path in sorted((_CODEX_DIR / "agents").glob("*.toml")):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            codex_home = temp_path / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(path.read_text())
            inert_cwd = temp_path / "inert-cwd"
            inert_cwd.mkdir()
            result = subprocess.run(
                [codex, "app-server", "--stdio", "--strict-config"],
                cwd=inert_cwd,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                input="",
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, (
                f"{path.name}: strict config parse failed:\n{result.stderr}"
            )


def test_installed_codex_exposes_required_agents_md_sections() -> None:
    """Installed Codex exposes the required AGENTS.md sections for this trusted repo."""
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("installed Codex CLI is unavailable")

    with tempfile.TemporaryDirectory() as temp_dir:
        codex_home = Path(temp_dir)
        (codex_home / "config.toml").write_text(f'[projects."{_REPO}"]\ntrust_level = "trusted"\n')
        result = subprocess.run(
            [codex, "debug", "prompt-input"],
            cwd=_REPO,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert result.returncode == 0, f"prompt-input failed:\n{result.stderr}"
    prompt_input = cast(list[object], json.loads(result.stdout))
    agents_blocks: list[str] = []
    for message in prompt_input:
        if not isinstance(message, dict):
            continue
        message_data = cast(dict[str, object], message)
        content = message_data.get("content")
        if not isinstance(content, list):
            continue
        for block in cast(list[object], content):
            if not isinstance(block, dict):
                continue
            block_data = cast(dict[str, object], block)
            text = block_data.get("text")
            if isinstance(text, str) and "# AGENTS.md - roastpilot-agent" in text:
                agents_blocks.append(text)

    assert len(agents_blocks) == 1
    assert "Codex-Led Delivery Topology" in agents_blocks[0]
    assert "Hardware Safety Notes" in agents_blocks[0]


def test_claude_implementation_roles_follow_slice_routing() -> None:
    """Claude implementation capacity remains leaf-only and slice-scoped."""
    role_paths = {path.stem: path for path in agent_files()}
    for role in ("engineer-be", "engineer-fe"):
        instructions = agent_text(role_paths[role])
        assert "one approved PR slice" in instructions
        assert "One PR per story" not in instructions
        assert "Do not invoke Codex or spawn agents" in instructions

    planner = agent_text(AGENTS_DIR / "story-planner.md")
    assert "Codex-MCP" not in planner
    assert "budget stop" not in planner
    for status in ("healthy", "constrained", "reserve-only"):
        assert f"`{status}`" in planner

    architect = agent_text(AGENTS_DIR / "planning-architect.md")
    assert "Opus PM" not in architect
    assert "Codex parent orchestrator to adjudicate" in architect
    assert "Codex parent owns delivery orchestration and\nscope decomposition" in architect
    assert "human retains product authority per `AGENTS.md`" in architect

    frontend = agent_text(AGENTS_DIR / "engineer-fe.md")
    assert "E10 status table" not in frontend
    assert "contract-named epic's status table\n  and registry" in frontend


def test_pr_preflight_has_one_live_d158_review_flow() -> None:
    """The pilot admits committed work and excludes legacy coordinator fan-out."""
    preflight = (_REPO / ".claude" / "skills" / "pr-preflight" / "SKILL.md").read_text()
    agents_md = (_REPO / "AGENTS.md").read_text()
    assert "Legacy sections" not in preflight
    assert "codex review --base" not in preflight
    assert "Minimum sufficient independent review" in preflight
    assert "Rerun every triggered reviewer whose evidence the change invalidated" in preflight
    assert "git status --porcelain`" in preflight
    assert (
        "does not replace the stricter fresh-worktree plus ignored-file admission check"
        in preflight
    )
    assert "git status --porcelain --ignored` is empty before delegation" in agents_md
    assert "restart preflight from the new `HEAD`" in preflight
    assert re.search(
        r"review-branch`\s+workflow is dormant and unavailable during the D158 pilot",
        agents_md,
    )
    assert re.search(
        r"Claude-coordinator\s+fan-out violates the Codex-parent-only crossing "
        r"and depth-one topology",
        agents_md,
    )
