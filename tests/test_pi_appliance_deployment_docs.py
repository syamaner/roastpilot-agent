"""Governance checks for the E11-S2 Pi appliance deployment guide."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
DEPLOYMENT_DOC = REPO_ROOT / "docs/deployment/pi-appliance.md"
EPIC_DOC = REPO_ROOT / "docs/epics/E11-packaging.md"
REGISTRY_DOC = REPO_ROOT / "docs/state/registry.md"


@pytest.mark.docs
def test_pi_appliance_deployment_doc_has_the_required_sections() -> None:
    """T25: the operator guide carries every required deployment section."""

    deployment = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    required_sections = (
        "## Prerequisites",
        "## Install",
        "## Configuration",
        "## Data location",
        "## Upgrade and maintenance",
        "## Logs",
        "## mDNS access",
        "## Trust boundary",
        "## Uninstall and rollback",
    )
    assert all(section in deployment for section in required_sections)


@pytest.mark.docs
def test_pi_appliance_deployment_doc_preserves_install_and_maintenance_contract() -> None:
    """T28: piped installation and maintenance retain their safe operator contract."""

    deployment = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    assert "--set-hostname roastpilot --start --yes" in deployment
    assert (
        "/usr/bin/sudo` must be\ninstalled, and that operator must be authorised to use it"
        in deployment
    )
    assert "sudo systemctl stop roastpilot-agent" in deployment
    assert "Never\nstop the service during a roast." in deployment
    assert "use `--start` only for\nan already-safe inactive appliance" in deployment
    assert "otherwise it starts at the next boot" in deployment
    assert "operator may explicitly start it only while no roast is active" in deployment
    assert "MCP default relative `logs` export directory" in deployment
    assert (
        "sets `WorkingDirectory=~`, MCP exports are in the operator account's `~/logs`"
        in deployment
    )
    assert (
        "Treat all three locations as appliance data when planning storage, backup,\n"
        "replacement, or removal." in deployment
    )


@pytest.mark.docs
def test_new_deployment_artefacts_exclude_prohibited_public_claims() -> None:
    """T26: new guide material preserves the E11 public-text boundary."""

    prohibited = (
        "-".join(("production", "ready")),
        " ".join(("fully", "autonomous")),
        "-".join(("hardware", "validated")),
        chr(176) + "F",
        "Fahren" + "heit",
    )
    deployment_content = DEPLOYMENT_DOC.read_text(encoding="utf-8").casefold()
    epic = EPIC_DOC.read_text(encoding="utf-8")
    registry = REGISTRY_DOC.read_text(encoding="utf-8")
    test_content = Path(__file__).read_text(encoding="utf-8").casefold()
    e11_s2_heading = "### E11-S2 — Native installer, systemd unit, bundled model, deploy doc"
    e11_s3_heading = "### E11-S3 — Pi 5 dual-mic recording + FC-detection CPU soak"
    epic_story = epic[epic.index(e11_s2_heading) : epic.index(e11_s3_heading)].casefold()
    epic_completion = epic[epic.index("**E11-S2 is complete (12 Sep 2026):") :].casefold()
    epic_status = next(
        line.casefold()
        for line in epic.splitlines()
        if line.startswith("| E11-S2 | Native installer, systemd unit, bundled model, deploy doc |")
    )
    registry_entry = registry[
        registry.index("**12 Sep 2026 — #138 E11-S2 native installer") : registry.index(
            "\n\n", registry.index("**12 Sep 2026 — #138 E11-S2 native installer")
        )
    ].casefold()
    for artefact, content in (
        (DEPLOYMENT_DOC, deployment_content),
        (EPIC_DOC, epic_story),
        (EPIC_DOC, epic_completion),
        (EPIC_DOC, epic_status),
        (REGISTRY_DOC, registry_entry),
        (Path(__file__), test_content),
    ):
        assert all(phrase.casefold() not in content for phrase in prohibited), artefact


@pytest.mark.docs
def test_e11_s2_status_updates_without_recasting_d191_thresholds() -> None:
    """T27: E11-S2 is done while the unstarted soak and D191 values remain untouched."""

    epic = (REPO_ROOT / "docs/epics/E11-packaging.md").read_text(encoding="utf-8")
    assert "E11-S2 | Native installer, systemd unit, bundled model, deploy doc | done" in epic
    assert (
        "E11-S3 | Pi 5 dual-mic recording + FC-detection CPU soak "
        "(overflow validation) | not started" in epic
    )
    assert "N/X" not in epic
    assert "streak-30" not in epic
