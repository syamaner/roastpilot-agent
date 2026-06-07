"""E1 scaffold smoke tests.

Real suites (test_controller, test_safety, test_advisor, test_mcp_client,
test_store, test_api, test_milestone1) land with their epics per component
plan §8. These tests pin the scaffold's typed invariants.
"""

import pydantic
import pytest
from fastapi.testclient import TestClient

import roastpilot_agent
from roastpilot_agent import controller
from roastpilot_agent.advisor import RoastDecision
from roastpilot_agent.api import create_app
from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import RoastPhase, RoastProfile
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict


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


def test_controller_reexports_roast_phase() -> None:
    """controller.RoastPhase stays an alias of the models.py home (D15)."""
    assert controller.RoastPhase is RoastPhase


def test_safety_verdicts_match_plan() -> None:
    """Six verdicts per D15, matching the plan §5 schema column."""
    assert {verdict.value for verdict in SafetyVerdict} == {
        "allow",
        "clamp",
        "reject",
        "recovery",
        "fault",
        "emergency_stop",
    }


def test_shared_enums_are_not_str() -> None:
    """AGENTS.md invariant: enums are plain Enum so members never compare
    equal to raw strings (and pyright strict flags the attempt)."""
    assert not any(isinstance(verdict, str) for verdict in SafetyVerdict)
    assert not any(isinstance(phase, str) for phase in RoastPhase)


def test_safety_evaluation_allows_no_adjusted_command() -> None:
    """REJECT/RECOVERY/FAULT/E-STOP carry no adjusted heat/fan (plan §5:
    nullable columns) — no fabricated 0/0 in the decision trace."""
    evaluation = SafetyEvaluation(
        rule="drop_eligibility", verdict=SafetyVerdict.REJECT, reason="unsafe drop request"
    )
    assert evaluation.adjusted_heat is None
    assert evaluation.adjusted_fan is None


def test_charge_guidance_stays_within_pre_t0_safety_bound() -> None:
    """models.RoastProfile guidance ceiling must not exceed the pre-T0 hard
    bound in config.SafetyLimits — the two defaults are linked (see comments
    at both definitions)."""
    profile = RoastProfile(
        name="scaffold-default",
        bean_origin="Ethiopia",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )
    assert profile.charge_guidance_max_c <= SafetyLimits().pre_t0_max_bean_temp_c


def test_roast_decision_bounds() -> None:
    with pytest.raises(pydantic.ValidationError):
        RoastDecision(
            target_heat=101,
            target_fan=50,
            should_drop=False,
            confidence=0.5,
            rationale="out-of-bounds heat must not validate",
        )
