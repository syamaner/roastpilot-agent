"""Governance checks for the E11-S2 Pi appliance deployment guide."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
DEPLOYMENT_DOC = REPO_ROOT / "docs/deployment/pi-appliance.md"


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
def test_new_deployment_artefacts_exclude_prohibited_public_claims() -> None:
    """T26: new guide material preserves the E11 public-text boundary."""

    prohibited = (
        "-".join(("production", "ready")),
        " ".join(("fully", "autonomous")),
        "-".join(("hardware", "validated")),
        chr(176) + "F",
        "Fahren" + "heit",
    )
    for artefact in (DEPLOYMENT_DOC, Path(__file__)):
        content = artefact.read_text(encoding="utf-8").casefold()
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
