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
    assert "booted with systemd\nand an active systemd manager" in deployment
    assert "it uses `systemctl` and `hostnamectl`" in deployment
    assert (
        "requires `apt` and `/usr/bin/python3` itself to report Python 3.11 or newer" in deployment
    )  # noqa: E501
    assert "`curl`, which both documented download procedures use." in deployment
    assert "--set-hostname roastpilot --start --yes" in deployment
    assert "Inspect with `less` or another trusted local viewer before running:" in deployment
    assert (
        "/usr/bin/sudo` must be installed, and that operator must be authorised to use it"
        in deployment
    )
    assert "sudo systemctl stop roastpilot-agent" in deployment
    assert "Never\nstop the service during a roast." in deployment
    assert "before the initial `--start`" in deployment
    assert "first safely end any roast and wait until\nthe appliance is inactive" in deployment
    assert (
        "".join(
            (
                "stop the\nservice, edit the protected file, and explicitly start it only ",
                "while no roast",
            )
        )
        in deployment
    )
    assert "that start reloads the `EnvironmentFile`" in deployment
    assert "Never stop or restart the\nservice during a roast." in deployment
    assert "use `--start` only for\nan already-safe inactive appliance" in deployment
    assert "otherwise it starts at the next boot" in deployment
    assert "operator may explicitly start it only while no roast is active" in deployment
    assert (
        "Saved non-null Config UI `mcp_device.serial_port` and "
        "`mcp_device.audio_input_device`" in deployment
    )
    assert (
        "update or clear\nthose saved overrides as well as rerunning the installer safely"
        in deployment
    )
    assert "MCP default relative `logs` export directory" in deployment
    assert (
        "sets `WorkingDirectory=~`, MCP exports are in the operator account's `~/logs`"
        in deployment
    )
    assert (
        "`~/.roastpilot/config.yaml`. Treat all four locations as appliance data when\n"
        "planning storage, backup, replacement, or removal." in deployment
    )
    assert "For offline model placement, use `--from-dir DIR`." in deployment
    assert (
        "It supplies only model\nbytes; `apt` and `pipx` still require their packages and "
        "dependencies." in deployment
    )
    assert "sudo journalctl -u roastpilot-agent -f" in deployment
    assert (
        "".join(
            (
                "configured HTTP port differs from `8000`, repeat it as ",
                "`--port CURRENT_PORT`",
            )
        )
        in deployment
    )
    assert "on every installer rerun; otherwise the installer default overwrites it." in deployment


@pytest.mark.docs
def test_new_deployment_artefacts_exclude_prohibited_public_claims() -> None:
    """T26: slice artefacts exclude specified exact public-accuracy phrases."""

    # Literal matching protects the listed variants; it is not a semantic acceptance audit.
    prohibited = (
        "-".join(("production", "ready")),
        " ".join(("fully", "autonomous")),
        "-".join(("hardware", "validated")),
        "-".join(("pi", "ready")),
        " ".join(("pi", "ready")),
        "-".join(("pi", "readiness")),
        " ".join(("physical", "validation", "complete")),
        " ".join(("physical", "validation", "completed")),
        " ".join(("physical", "validation", "is", "complete")),
        " ".join(("physical-device", "validation", "complete")),
        " ".join(("physical-device", "validation", "completed")),
        " ".join(("physical-device", "validation", "is", "complete")),
        " ".join(("complete", "physical", "validation")),
        " ".join(("completed", "physical", "validation")),
        "-".join(("release", "ready")),
        " ".join(("ready", "for", "release")),
        "".join(("%", " deterministic")),
        chr(176) + "F",
        "Fahren" + "heit",
    )
    deployment_content = DEPLOYMENT_DOC.read_text(encoding="utf-8").casefold()
    epic = EPIC_DOC.read_text(encoding="utf-8")
    registry = REGISTRY_DOC.read_text(encoding="utf-8")
    test_content = Path(__file__).read_text(encoding="utf-8").casefold()
    e11_s2_heading = "### E11-S2 — Native installer, systemd unit, bundled model, deploy doc"
    e11_s3_heading = "### E11-S3 — Pi 5 dual-mic recording + FC-detection CPU soak"
    d27_callout_heading = "> **D27 E11-S1 dependency/publication gate — ✅ CLEARED:"
    epic_story = epic[epic.index(e11_s2_heading) : epic.index(e11_s3_heading)].casefold()
    epic_d27_callout = epic[epic.index(d27_callout_heading) : epic.index("## Stories")].casefold()
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
        (EPIC_DOC, epic_d27_callout),
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
