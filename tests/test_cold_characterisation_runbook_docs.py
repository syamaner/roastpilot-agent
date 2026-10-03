"""Docs tests for the #954 cold characterisation runbook, E11 and the registry (U4).

These are literal checks that the safety-relevant statements are present and that
the public-accuracy boundaries hold; they are not a semantic audit of the prose.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs/deployment/cold-characterisation-runbook.md"
EPIC = REPO_ROOT / "docs/epics/E11-packaging.md"
REGISTRY = REPO_ROOT / "docs/state/registry.md"
PI_APPLIANCE = REPO_ROOT / "docs/deployment/pi-appliance.md"
ROUTES = REPO_ROOT / "web/src/routes.tsx"
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
        "The D195 six-dimension envelope (heat, roast fan, main fan, drum, cooling, "
        "solenoid/drop) is checked only at eligible finalisation, never after a retained abort.",
        "only heat, roast fan (D197) and cooling are observed, and they are commanded state, "
        "not sensing",
        "The independent operator emergency stop is required throughout the run.",
        "Numeric roaster-temperature plausibility is unresolved",
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
