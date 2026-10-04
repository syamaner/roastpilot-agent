"""Docs tests for the #954 cold characterisation runbook, E11 and the registry (U4).

These are literal checks that the safety-relevant statements are present and that
the public-accuracy boundaries hold; they are not a semantic audit of the prose.
"""

import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from roastpilot_agent import cold_composition

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs/deployment/cold-characterisation-runbook.md"
EPIC = REPO_ROOT / "docs/epics/E11-packaging.md"
REGISTRY = REPO_ROOT / "docs/state/registry.md"
PI_APPLIANCE = REPO_ROOT / "docs/deployment/pi-appliance.md"
ROUTES = REPO_ROOT / "web/src/routes.tsx"
TWO_PHASE = REPO_ROOT / "src/roastpilot_agent/cold_characterisation/two_phase.py"
COLD_STREAM = REPO_ROOT / "src/roastpilot_agent/cold_observation_stream.py"
S3_HEADING = (
    "### E11-S3 — Pi 5 single-primary-mic complete-appliance cold characterisation "
    "(overflow validation)"
)
PROHIBITED = (
    "-".join(("production", "ready")),
    " ".join(("fully", "autonomous")),
    "-".join(("hardware", "validated")),
    "-".join(("pi", "ready")),
    " ".join(("pi", "ready")),
    "-".join(("pi", "readiness")),
    " ".join(("physical", "validation", "complete")),
    " ".join(("physical", "validation", "completed")),
    " ".join(("complete", "physical", "validation")),
    "-".join(("release", "ready")),
    " ".join(("ready", "for", "release")),
    "".join(("%", " deterministic")),
    chr(176) + "F",
    "Fahren" + "heit",
)


def _flat(text: str) -> str:
    """Collapse whitespace so wrapped sentences match as one line."""
    return re.sub(r"\s+", " ", text)


@pytest.mark.docs
def test_runbook_states_the_safety_boundaries() -> None:
    text = _flat(RUNBOOK.read_text(encoding="utf-8"))
    for phrase in (
        "Per-tick software observation checks commanded heat, main fan, roast fan and cooling. "
        "Drum and solenoid/drop appear only in eligible D195 six-dimension finalisation "
        "evidence, never after a retained abort. These are commanded software values, "
        "not physical sensing or proof of physical response.",
        "The independent operator emergency stop is required throughout the run.",
        "Exit codes 80-83",
        "no trusted receipt",
        "Repeated signals neither force an exit nor cancel again.",
        "does not prove that no cold child exists",
        "retained display ticks before engine classification",
        "four unauthenticated display clients",
        "this invocation started no cold child",
        "Every operator-supervised gate in this runbook is unexecuted",
        "#954 stays open",
        "operator assertion",
        "advisory only",
        "Never derive the receipt from the evidence tree itself.",
        "There is no CLI verifier",
        "A successful bind does not prove that the service was stopped",
        "If no `manifest_sha256` value was printed, there is no receipt for that run.",
        "an exit status of 0 without a complete closed summary is not conformance",
        "If the process exit status differs from the summary's `exit_code` line",
        "teardown may be incomplete and is uncertain",
        "keeps the default disposition: the process ends with no summary",
        "Exit 130 with `signal=none` means the process was interrupted or cancelled "
        "without a recorded first signal",
    ):
        assert phrase.replace("**", "") in text.replace("**", ""), phrase


def _marked_fence(marker: str, language: str) -> str:
    """Extract the one fenced block immediately following a story marker."""
    raw = RUNBOOK.read_text(encoding="utf-8")
    pattern = rf"<!-- {re.escape(marker)} -->\s*```{language}\n(.*?)\n```"
    matches = re.findall(pattern, raw, flags=re.DOTALL)
    assert len(matches) == 1
    return matches[0]


_PLACEHOLDERS = {
    "<frozen provider>": "openai_compatible",
    "<frozen provider endpoint>": "https://cold.invalid/v1",
    "<frozen credential variable name>": "RP_COLD_TEST_KEY",
    "<frozen model slug>": "frozen/model",
    "<frozen prompt version>": "cold-v1",
    "<frozen absolute MCP entry point>": "/opt/cold/bin/coffee-roaster-mcp",
    "<frozen absolute device path>": "/dev/cold-test-roaster",
    "<frozen primary microphone identity>": "Cold Test Primary Mic",
    "<absolute path to frozen MCP YAML>": "/nonexistent/cold-test-mcp.yaml",
}
_CANARY = "cold-preflight-secret-canary"


def _replace_placeholders(text: str, *, saved_model: str = "frozen/model") -> str:
    """Replace the documented operator placeholders with synthetic frozen values."""
    replacements = {**_PLACEHOLDERS, "<frozen model slug>": saved_model}
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    assert "<frozen " not in text and "<absolute path" not in text
    return text


def _controlled_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install only synthetic process values needed by the closed environment admission."""
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith("ROASTPILOT_") or upper.startswith("COFFEE_"):
            monkeypatch.delenv(name)
    for name, value in {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent/cold-test-home",
        "USER": "cold-test",
        "LOGNAME": "cold-test",
        "TMPDIR": "/nonexistent/cold-test-tmp",
        "SHELL": "/bin/synthetic-shell",
        "TERM": "synthetic-term",
        "RP_COLD_TEST_KEY": _CANARY,
        "ROASTPILOT_ADVISOR__API_KEY_ENV": "RP_COLD_TEST_KEY",
    }.items():
        monkeypatch.setenv(name, value)


def _run_documented_preflight(
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    yaml_mutator: Callable[[str], str] = lambda text: text,
    environment: dict[str, str | None] | None = None,
    config_path: str | None = "absolute",
) -> tuple[int, str, str]:
    """Execute the actual marked fences under closed synthetic process state."""
    _controlled_environment(monkeypatch)
    yaml_text = _replace_placeholders(
        _marked_fence("story-1002-cold-config-template", "yaml"), saved_model="saved/model"
    )
    config_file = tmp_path / "cold-config.yaml"
    config_file.write_text(yaml_mutator(yaml_text), encoding="utf-8")
    if config_path == "absolute":
        monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(config_file))
    elif config_path == "missing":
        monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    elif config_path == "relative":
        monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", "relative-cold-config.yaml")
    elif config_path is None:
        monkeypatch.delenv("ROASTPILOT_CONFIG_FILE", raising=False)
    else:  # pragma: no cover - closed helper vocabulary
        raise AssertionError(config_path)
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "frozen/model")
    for name, value in (environment or {}).items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    forbidden_calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        forbidden_calls.append("called")
        raise AssertionError("provider, MCP or hardware construction attempted")

    monkeypatch.setattr(cold_composition, "build_advisor", forbidden)
    monkeypatch.setattr(cold_composition, "ColdMCPServerProcess", forbidden)
    script = _replace_placeholders(_marked_fence("story-1002-cold-config-preflight", "python"))
    with pytest.raises(SystemExit) as stopped:
        exec(compile(script, "<documented-cold-preflight>", "exec"), {"__name__": "__main__"})
    captured = capsys.readouterr()
    assert forbidden_calls == []
    exit_code = stopped.value.code
    assert type(exit_code) is int
    return exit_code, captured.out, captured.err


@pytest.mark.docs
def test_story_1002_artifact_selection_and_installed_bytes_are_separate_gates() -> None:
    """Selected artifacts remain distinct from separately authorised installed-byte proof."""
    raw = RUNBOOK.read_text(encoding="utf-8")
    section = raw[raw.index("### 2.1") : raw.index("### 2.2")]
    for phrase in (
        "Agent artifact",
        "reviewed MCP candidate",
        "Published MCP 0.2.2 lacks the D209 temperature projection",
        "installed bytes as separate facts",
        "none attests the bytes imported by the intended interpreter",
        "separately authorised installed-byte gate",
        "That gate remains unexecuted here.",
    ):
        assert phrase in _flat(section)
    for overclaim in ("is installed", "matches installed bytes", "has been verified"):
        assert overclaim not in section


@pytest.mark.docs
def test_story_1002_template_uses_supported_credential_name_configuration() -> None:
    """The read-only saved field stays absent; the effective env override is explicit."""
    template = _marked_fence("story-1002-cold-config-template", "yaml")
    section = RUNBOOK.read_text(encoding="utf-8").split("### 2.2", maxsplit=1)[1]
    assert "api_key_env" not in template
    assert "ROASTPILOT_ADVISOR__API_KEY_ENV='<frozen credential variable name>'" in section
    assert "config.advisor.api_key_env != EXPECTED_CREDENTIAL_NAME" in section
    assert (
        "assumes an operator-controlled configuration file that is kept stable for the "
        "duration of the check" in _flat(section)
    )
    assert "does not lock or atomically snapshot the selected pathname" in _flat(section)
    assert "does not protect against concurrent replacement" in _flat(section)


@pytest.mark.docs
def test_story_1002_template_closes_frozen_model_device_and_command_boundaries() -> None:
    """The documented fences close every L8 effective-configuration gap."""
    template = _marked_fence("story-1002-cold-config-template", "yaml")
    section = _flat(RUNBOOK.read_text(encoding="utf-8").split("### 2.2", maxsplit=1)[1])
    assert "command: <frozen absolute MCP entry point>" in template
    for omitted in (
        "model_slug_by_phase",
        "fc_confidence_threshold",
        "auto_t0_detection_enabled",
        "auto_t0_drop_threshold_c",
        "ambient_mode",
        "ambient_device",
        "ambient_poll_interval_seconds",
    ):
        assert omitted not in template
        assert f"{omitted} is not None" in section or f"{omitted} != {{}}" in section
    assert "an alternate absolute path, a relative path or the bare default is refused" in section
    assert "does not resolve, open or execute it" in section
    assert "attest its existence, installed bytes, code or provenance" in section


def test_story_1002_commanded_state_sibling_prose_matches_engine_and_projection() -> None:
    """The engine checks main fan while the public display field remains intentionally null."""
    two_phase = _flat(TWO_PHASE.read_text(encoding="utf-8"))
    stream = _flat(COLD_STREAM.read_text(encoding="utf-8"))
    corrected = (
        "per-tick software observation checks commanded heat, main fan, roast fan and cooling. "
        "Drum and solenoid/drop appear only in eligible D195 six-dimension finalisation "
        "evidence. These are commanded software values, not physical sensing or proof of "
        "physical response."
    )
    assert corrected in two_phase
    assert (
        "engine's per-tick commanded-state check covers heat, main fan, roast fan and cooling"
        in stream
    )
    assert (
        "public display projection still leaves ``fan_percent`` (the main fan) as ``null``"
        in stream
    )


@pytest.mark.docs
def test_story_1002_documented_preflight_admits_effective_environment_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The exact documented fences load env-over-file config and admit without side effects."""
    code, stdout, stderr = _run_documented_preflight(
        monkeypatch=monkeypatch, capsys=capsys, tmp_path=tmp_path
    )
    assert (code, stdout, stderr) == (0, "cold configuration preflight: ADMITTED\n", "")
    assert _CANARY not in stdout + stderr


@pytest.mark.docs
@pytest.mark.parametrize("config_path", [None, "relative", "missing"])
def test_story_1002_documented_preflight_refuses_missing_or_unusable_config_file(
    config_path: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    code, stdout, stderr = _run_documented_preflight(
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
        config_path=config_path,
    )
    assert (code, stdout, stderr) == (1, "cold configuration preflight: REFUSED\n", "")


_REFUSAL_CASES: list[tuple[Callable[[str], str], dict[str, str | None] | None]] = [
    (lambda text: _CANARY + "\n: malformed", None),
    (lambda text: f"- {_CANARY}\n- genuine-non-mapping-yaml", None),
    (lambda text: text.replace("  env: {}", "  env: {EXTRA: canary}"), None),
    (lambda text: text + "\n  recording_enabled: true\n", None),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: relative.yaml",
        ),
        None,
    ),
    (
        lambda text: text.replace("  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml", ""),
        None,
    ),
    (lambda text: text, {"coffee_hidden": _CANARY}),
    (lambda text: text, {"RP_COLD_TEST_KEY": ""}),
    (lambda text: text.replace("provider: openai_compatible", "provider: openai"), None),
    (
        lambda text: text.replace(
            "provider_base_url: https://cold.invalid/v1",
            "provider_base_url: https://other.invalid/v1",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_ADVISOR__MODEL_SLUG": "other/model"}),
    (
        lambda text: text.replace(
            "  prompt_version: cold-v1",
            "  prompt_version: cold-v1\n  model_slug_by_phase:\n    development: other/model",
        ),
        None,
    ),
    (
        lambda text: text,
        {"ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE": '{"preheating":"other/model"}'},
    ),
    (
        lambda text: text,
        {"ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE": '{"development":"other/model"}'},
    ),
    (lambda text: text.replace("prompt_version: cold-v1", "prompt_version: other"), None),
    (lambda text: text, {"ROASTPILOT_ADVISOR__API_KEY_ENV": "OTHER_KEY"}),
    (
        lambda text: text.replace("serial_port: /dev/cold-test-roaster", "serial_port: /dev/other"),
        None,
    ),
    (
        lambda text: text.replace("roaster_driver: hottop_kn8828b_2k_plus", "roaster_driver: mock"),
        None,
    ),
    (
        lambda text: text.replace(
            "audio_input_device: Cold Test Primary Mic", "audio_input_device: Other Mic"
        ),
        None,
    ),
    (
        lambda text: text.replace("    - Cold Test Primary Mic", "    - Other Recording Mic"),
        None,
    ),
    (lambda text: text.replace("fc_mode: audio", "fc_mode: manual"), None),
    (lambda text: text + "\n  recording_autocapture: false\n", None),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml\n"
            "  fc_confidence_threshold: 0.0",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_MCP_DEVICE__FC_CONFIDENCE_THRESHOLD": "0"}),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml\n"
            "  auto_t0_detection_enabled: false",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_MCP_DEVICE__AUTO_T0_DETECTION_ENABLED": "false"}),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml\n"
            "  auto_t0_drop_threshold_c: 1.0",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_MCP_DEVICE__AUTO_T0_DROP_THRESHOLD_C": "1"}),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml\n  ambient_mode: disabled",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_MCP_DEVICE__AMBIENT_MODE": "disabled"}),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml\n  ambient_device: SYNTHETIC",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_MCP_DEVICE__AMBIENT_DEVICE": "SYNTHETIC"}),
    (
        lambda text: text.replace(
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml",
            "  mcp_yaml_source_path: /nonexistent/cold-test-mcp.yaml\n"
            "  ambient_poll_interval_seconds: 1.0",
        ),
        None,
    ),
    (lambda text: text, {"ROASTPILOT_MCP_DEVICE__AMBIENT_POLL_INTERVAL_SECONDS": "1"}),
    (
        lambda text: text.replace(
            "command: /opt/cold/bin/coffee-roaster-mcp",
            "command: /opt/other/bin/coffee-roaster-mcp",
        ),
        None,
    ),
    (
        lambda text: text.replace(
            "command: /opt/cold/bin/coffee-roaster-mcp", "command: relative/coffee-roaster-mcp"
        ),
        None,
    ),
    (
        lambda text: text.replace(
            "command: /opt/cold/bin/coffee-roaster-mcp", "command: coffee-roaster-mcp"
        ),
        None,
    ),
]


@pytest.mark.docs
@pytest.mark.parametrize(
    ("mutation", "environment"),
    _REFUSAL_CASES,
    ids=(
        "malformed-yaml",
        "non-mapping-yaml-list",
        "nonempty-mcp-env",
        "recording-field-set",
        "relative-mcp-yaml",
        "missing-mcp-yaml",
        "ambient-coffee-name",
        "empty-credential",
        "provider-mismatch",
        "endpoint-mismatch",
        "model-mismatch",
        "saved-development-model-override",
        "environment-preheating-model-override",
        "environment-development-model-override",
        "prompt-mismatch",
        "credential-name-mismatch",
        "serial-mismatch",
        "driver-mismatch",
        "audio-mismatch",
        "recording-device-mismatch",
        "fc-mode-mismatch",
        "recording-autocapture-set",
        "saved-zero-fc-confidence",
        "environment-zero-fc-confidence",
        "saved-disabled-auto-t0",
        "environment-disabled-auto-t0",
        "saved-auto-t0-threshold",
        "environment-auto-t0-threshold",
        "saved-disabled-ambient-mode",
        "environment-disabled-ambient-mode",
        "saved-ambient-device",
        "environment-ambient-device",
        "saved-ambient-poll-interval",
        "environment-ambient-poll-interval",
        "alternate-absolute-mcp-command",
        "relative-mcp-command",
        "bare-default-mcp-command",
    ),
)
def test_story_1002_documented_preflight_refuses_closed_without_leaking(
    mutation: Callable[[str], str],
    environment: dict[str, str | None] | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Every malformed, mismatched or helper-refused case has one content-free result."""
    code, stdout, stderr = _run_documented_preflight(
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
        yaml_mutator=mutation,
        environment=environment,
    )
    assert (code, stdout, stderr) == (1, "cold configuration preflight: REFUSED\n", "")
    assert _CANARY not in stdout + stderr


@pytest.mark.docs
def test_runbook_lists_every_exit_code_and_the_closed_summary_keys() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for row in ("| 0 |", "| 2 |", "| 3 |", "| 4 |", "| 5 |", "| 6 |", "| 7 |", "| 8 |"):
        assert row in text, row
    assert "| 130 / 143 |" in text
    assert "| 80-83 |" in text
    assert "0 | `ADVISORY_CONFORMANT` | Not qualification" in text
    for key in (
        "mode",
        "run_invoked",
        "result",
        "cli_refusal",
        "composition_refusal",
        "outcome",
        "start_refusal",
        "termination_reason",
        "child_ownership",
        "advisory_path",
        "provider_check",
        "conformance_outcome",
        "manifest_sha256",
        "signal",
        "http_server",
        "exit_code",
    ):
        assert f"`{key}`" in text, key


@pytest.mark.docs
def test_cold_docs_exclude_prohibited_public_claims() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8").casefold()
    epic = EPIC.read_text(encoding="utf-8")
    s3 = epic[epic.index(S3_HEADING) : epic.index("## Status")].casefold()
    registry = REGISTRY.read_text(encoding="utf-8")
    start = registry.index("**3 Oct 2026 — #954")
    entry = registry[start : registry.index("\n\n", start)].casefold()
    for content in (runbook, s3, entry):
        assert all(phrase.casefold() not in content for phrase in PROHIBITED)


@pytest.mark.docs
def test_e11_s3_is_single_primary_mic_and_not_started() -> None:
    epic = EPIC.read_text(encoding="utf-8")
    assert S3_HEADING in epic
    assert (
        "| E11-S3 | Pi 5 single-primary-mic complete-appliance cold characterisation "
        "(overflow validation) | not started |" in epic
    )
    s3 = epic[epic.index(S3_HEADING) : epic.index("## Status")]
    acceptance = s3[s3.index("Acceptance criteria:") : s3.index("**History")]
    assert "dual-mic" not in acceptance
    assert "both mics" not in acceptance
    assert "mono 16 kHz / 16-bit" in _flat(s3)
    assert "Multi-microphone capture is deferred." in _flat(s3)
    assert "dual-mic" not in epic[: epic.index("## Status")].split(S3_HEADING)[0]
    assert "`coffee-roaster-mcp==0.2.2`" in epic
    assert "0.1.13" not in epic
    assert "E11-S3 are not started" in _flat(epic)


@pytest.mark.docs
def test_registry_records_954_as_software_only_and_open() -> None:
    registry = REGISTRY.read_text(encoding="utf-8")
    start = registry.index("**3 Oct 2026 — #954")
    entry = _flat(registry[start : registry.index("\n\n", start)])
    assert "software" in entry
    assert "not started" in entry
    assert "#954 is open" in entry
    assert "approv" not in entry.casefold()
    assert registry.index("D-ToS-1 governance reconciliation") < start


@pytest.mark.docs
def test_pi_appliance_guide_links_the_runbook() -> None:
    text = PI_APPLIANCE.read_text(encoding="utf-8")
    assert "(cold-characterisation-runbook.md)" in text


@pytest.mark.docs
def test_e11_s3_active_acceptance_keeps_the_locked_d191_limits_and_authorises_no_tuning() -> None:
    """The active D194 acceptance states the locked limits; tuning is never in scope."""
    epic = EPIC.read_text(encoding="utf-8")
    s3 = epic[epic.index(S3_HEADING) : epic.index("## Status")]
    active = _flat(s3[s3.index("Acceptance criteria") : s3.index("**History")])
    history = _flat(s3[s3.index("**History") :])
    for phrase in (
        "the locked D191 limits apply unchanged",
        "30-minute recording-off phase followed by the 30-minute recording-on phase",
        "at most N = 1 consecutive overflow",
        "at most X = 200 ms of peak trailing-60-second lost audio",
        "The production fatal consecutive-overflow streak of 30 is unchanged.",
        "failing to meet them fails qualification",
        "no limit is loosened in the light of results",
        "This cold run authorises no tuning.",
        "separately authorised change, never part of this run",
        "requires a fresh characterisation",
    ):
        assert phrase in active, phrase
    assert "well under the fatal threshold" not in active
    assert "any overflow fails" not in active.casefold()
    assert "apply the optimisation levers" not in active
    assert "Record which lever" not in active
    assert "historical context only and are not authorised by the cold run" in history
    status = _flat(epic[epic.index("## Status") :])
    assert "logged (historical; superseded by the D194 scope" in status


@pytest.mark.docs
def test_runbook_verifier_signals_and_spa_boundaries() -> None:
    """Verifier interpreter, protected roots, SPA trust and signal timing are stated."""
    raw = RUNBOOK.read_text(encoding="utf-8")
    text = _flat(raw)
    assert "RP_COLD_PYTHON='/absolute/path/to/installed/roastpilot-agent/venv/bin/python'" in raw
    assert '"$RP_COLD_PYTHON" -c' in raw
    assert not any(line.lstrip().startswith("python -c") for line in raw.splitlines())
    assert "protected_roots=(" in raw
    assert "never a bare `python`" in text
    assert "every file under it is served to unauthenticated clients" in text
    assert (
        "A usage error prints only the fixed usage line on stderr (exit 2), never a summary."
        in text
    )
    assert "a 30-minute recording-off phase followed by a 30-minute recording-on phase" in text
    assert "prescribes no stimulus content, no other duration and no run authority" in text
    assert "may only be handled after the run has been invoked" in text
    assert "it can exit 1 with `signal=none`" in text
    assert "Exit 130 is not guaranteed for every early timing." in text
    assert "the software adds no automatic exclusion" in text


@pytest.mark.docs
def test_runbook_states_the_http_server_snapshot_without_health_or_qualification_claims() -> None:
    """H13: the 16th key, its closed tokens and every non-meaning are stated."""
    raw = RUNBOOK.read_text(encoding="utf-8")
    text = _flat(raw)
    assert "summary of exactly 16 `key=value` lines" in text
    assert "summary of exactly 15" not in text
    for token in (
        "task_not_created",
        "start_not_confirmed",
        "task_pending_at_report",
        "task_ended_at_report",
    ):
        assert f"| `{token}` |" in raw, token
    for phrase in (
        "`http_server` is not uptime, health, client delivery or rendering, "
        "and it is not evidence.",
        "It is never written to the store, the evidence or any qualification input.",
        "It never changes the outcome, result or exit code, and it is absent when exit codes "
        "80-83 apply",
        "An HTTP server that ends does not stop, cancel or reclassify the run.",
        "Neither the engine outcome nor exit 0 is proof of view availability or of qualification.",
        "It is a single snapshot, not monitoring and not a watchdog.",
        "a failure after the snapshot (during output or teardown) is not reported",
        "Nothing this software prints shows that the view stayed available for the whole run.",
    ):
        assert phrase in text, phrase
    for overclaim in (
        "operator observation proves",
        "observation is acceptance",
        "view was healthy",
    ):
        assert overclaim not in text.casefold()


def _section(raw: str, heading: str) -> str:
    """Return one ``## `` section of the runbook, up to the next ``## `` heading."""
    start = raw.index(heading)
    end = raw.find("\n## ", start + len(heading))
    return raw[start:] if end == -1 else raw[start:end]


@pytest.mark.docs
def test_runbook_route_empty_host_and_distinct_evidence_roots() -> None:
    """A6: the cold URL matches the frozen route; roots must not overlap; empty host refused."""
    raw = RUNBOOK.read_text(encoding="utf-8")
    command = _flat(_section(raw, "## 3. Command"))
    monitoring = _flat(_section(raw, "## 4. Monitoring"))
    evidence = _flat(_section(raw, "## 6. Evidence"))
    assert "`http://127.0.0.1:8000/cold-characterisation`" in monitoring
    assert 'path: "/cold-characterisation"' in ROUTES.read_text(encoding="utf-8")
    assert "`ROOTS_OVERLAP`" in evidence
    secondary = command[command.index("`--secondary-evidence-dir` is recorded only") :]
    secondary = secondary[: secondary.index(" - `--protected-root`")]
    assert "never opens or writes" in secondary
    assert "distinct and non-overlapping" in secondary
    for word in ("nested", "aliased", "hard-linked"):
        assert word in secondary, word
    assert "An explicitly empty `--host` is a usage error" in command


@pytest.mark.docs
def test_runbook_states_complete_appliance_preconditions_and_interrupted_usage_output() -> None:
    """A8: D183/D194 operator preconditions; an interrupted usage or help write exits 130."""
    raw = RUNBOOK.read_text(encoding="utf-8")
    prerequisites = _flat(_section(raw, "## 2. Prerequisites"))
    for phrase in (
        "Raspberry Pi 5",
        "one mono 16 kHz / 16-bit stream; multi-microphone capture is deferred",
        "ONNX-int8 first-crack inference",
        "advisory-only advisor runs with a frozen production provider, model and prompt",
        "Any material identity change restarts characterisation.",
        "does not independently attest the physical board, the microphone format or the "
        "actual loaded model",
        "not physical proof",
    ):
        assert phrase in prerequisites, phrase
    summary = _flat(_section(raw, "## 5. Exit codes and summary"))
    usage = summary.index("A usage error prints only the fixed usage line on stderr (exit 2)")
    interrupted = summary.index(
        "An interrupt arriving while the usage line or help text is being written follows "
        "the interrupt rule instead: exit 130 with one `cancelled_before_run` summary"
    )
    assert 0 < interrupted - usage < 200


# ----------------------------------------- #997 T2: D209 reconciliation (DC1-DC4)

_RETIRED_PHRASES = ("plausibility is unresolved", "values are checked for finiteness only")


def _normalised(text: str) -> str:
    """Whitespace-normalised text with Markdown bold markers removed."""
    return _flat(text).replace("**", "")


@pytest.mark.docs
def test_997_dc1_the_runbook_states_the_d209_screen_and_its_residuals() -> None:
    """DC1: the finiteness-only residual is retired; the screen and its limits are stated.

    Literal checks only (positive control: both retired phrases are present in the
    base runbook); the product audit is the semantic lens.
    """
    raw = RUNBOOK.read_text(encoding="utf-8")
    text = _normalised(raw)
    for retired in _RETIRED_PHRASES:
        assert retired not in text, retired
    for phrase in (
        "5 to 40 °C",
        "engineering screening, not calibration",
        "not a watchdog",
        "Bad-checksum frames",
        "operator assertion",
        "does not attest the bytes that are installed",
        "fails closed at its first tick",
        "a separately authorised gate",
        "A non-Celsius reported unit fails the screen.",
        "not a transaction",
        "`temperature_screened_conformant`",
        "`mcp_candidate_not_admitted` (exit 4)",
    ):
        assert phrase in text, phrase
    command = _section(raw, "## 3. Command")
    block = command[command.index("```bash") : command.index("```", command.index("```bash") + 3)]
    for flag in (
        "--mcp-candidate-version",
        "--mcp-candidate-wheel-sha256",
        "--mcp-candidate-wheel-bytes",
        "--mcp-candidate-reviewed-revision",
    ):
        assert flag in block, flag
    assert "0 | `ADVISORY_CONFORMANT` | Not qualification" in raw


@pytest.mark.docs
def test_997_dc2_the_runbook_distinguishes_reviewed_candidate_and_published_bytes() -> None:
    """DC2: the reviewed-candidate record values and the published 0.2.2 digest differ."""
    prerequisites = _normalised(_section(RUNBOOK.read_text(encoding="utf-8"), "## 2. Prereq"))
    published = "25f4164027afc9d336e8726465ad3c702c39dc40a905c9f83a2c36f3473d4de3"
    reviewed = "4f52048efa4c368e785c30acface04356dd943583a251e02a9b635905dec476a"
    for value in (
        published,
        reviewed,
        "169948 bytes",
        "0fc5e6d2e2a677c392b0706267e154f5780b96b0",
        "96f8916407ec8d31bbed9637e824c3cd2b0cf24a",
        "02bfa739a4cc3975295540c7df98ac844b5abbb7",
        "Published distribution metadata",
        "Reviewed candidate bytes",
        "Installed bytes",
    ):
        assert value in prerequisites, value
    assert prerequisites.index(published) < prerequisites.index(reviewed)


@pytest.mark.docs
def test_997_dc3_e11_records_the_997_slices_and_keeps_e11_s3_unstarted() -> None:
    """DC3: E11 names the #997 slices, keeps the published pin and an unstarted E11-S3."""
    epic = _flat(EPIC.read_text(encoding="utf-8"))
    for value in ("#998", "#999", "#1000", "T2", "`coffee-roaster-mcp==0.2.2`"):
        assert value in epic, value
    assert "E11-S3 are not started" in epic


@pytest.mark.docs
def test_997_dc4_the_registry_records_997_as_software_only_and_954_open() -> None:
    """DC4: a dated #997 entry, software only, #954 open, E11-S3 unstarted, no overclaim."""
    registry = REGISTRY.read_text(encoding="utf-8")
    start = registry.index("**4 Oct 2026 — #997")
    entry = _flat(registry[start : registry.index("\n\n", start)])
    for phrase in ("software", "#954 is open", "E11-S3 is not started"):
        assert phrase in entry, phrase
    assert all(phrase.casefold() not in entry.casefold() for phrase in PROHIBITED)
    assert registry.index("D-ToS-1 governance reconciliation") < start
    assert start < registry.index("**3 Oct 2026 — #954")
