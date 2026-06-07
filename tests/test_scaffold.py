"""E1 scaffold smoke tests.

Real suites (test_controller, test_safety, test_advisor, test_mcp_client,
test_store, test_api, test_milestone1) land with their epics per component
plan §8. These tests pin the scaffold's typed invariants.
"""

import pydantic
import pytest
from fastapi.testclient import TestClient

import roastpilot_agent
from roastpilot_agent.advisor import RoastDecision
from roastpilot_agent.api import create_app
from roastpilot_agent.controller import RoastController, RoastPhase
from roastpilot_agent.safety import SafetyVerdict


def test_package_has_version() -> None:
    assert roastpilot_agent.__version__


def test_health_route() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == roastpilot_agent.__version__


def test_roast_phases_match_plan() -> None:
    """The nine agent phases from component plan §3 / orchestration plan."""
    assert {phase.value for phase in RoastPhase} == {
        "idle",
        "starting",
        "preheating",
        "roasting_pre_first_crack",
        "development",
        "cooling",
        "complete",
        "faulted",
        "operator_recovery_required",
    }


def test_controller_starts_idle() -> None:
    assert RoastController().phase is RoastPhase.IDLE


def test_safety_verdicts_are_typed() -> None:
    """Verdicts per AGENTS.md invariant — typed, never string-compared."""
    assert {verdict.name for verdict in SafetyVerdict} == {
        "ALLOW",
        "CLAMP",
        "REJECT",
        "FAULT",
        "EMERGENCY_STOP",
    }


def test_roast_decision_bounds() -> None:
    with pytest.raises(pydantic.ValidationError):
        RoastDecision(
            target_heat=101,
            target_fan=50,
            should_drop=False,
            confidence=0.5,
            rationale="out-of-bounds heat must not validate",
        )
