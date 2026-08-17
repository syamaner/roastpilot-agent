"""E7 API tests (component plan §6, §8 ``test_api.py``).

E7-S1 covers the REST routes and their typed response models: health, roast
lifecycle start (incl. the 409 active-run guard), history/detail reads, the
downsampled telemetry series, the decision-trace timeline, the export-log
manifest + downloads, and operator rating. E7-S2 covers the operator action
queue (action → operator_actions row → controller queue → safety policy).
E7-S3 covers the typed SSE event stream (event vocabulary, framing, telemetry,
heartbeat, disconnect handling). All hardware-free: an in-memory controller is
never started — routes read the temp SQLite store and events are published
directly into the broadcaster.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast
from unittest import mock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import roastpilot_agent.api as api_module
from roastpilot_agent import __version__
from roastpilot_agent.advisor import AdvisorContext, FakeAdvisor, RoastDecision
from roastpilot_agent.api import (
    _DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    EventBroadcaster,
    FiniteJSONResponse,
    QueuedOperatorAction,
    RoastConfigError,
    RoastRunConflictError,
    RoastRunGoneError,
    RoastRunner,
    RoastService,
    _before_the_minute,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    _parse_last_event_id,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    create_app,
    stream_events,
)
from roastpilot_agent.bean_sourcing import (
    BeanExtractionError,
    BeanExtractionUnavailableError,
    BeanFetchError,
    BeanSourcingDiagnostics,
)
from roastpilot_agent.catalogue_recommendations import CATALOGUE_EXTRACTION_PROMPT_VERSION
from roastpilot_agent.config import (
    AmbientFanDoctrine,
    AppConfig,
    ControllerConfig,
    MCPDeviceConfig,
    PostFirstCrackControl,
    ReferenceCurve,
    SafetyLimits,
)
from roastpilot_agent.mcp_client import (
    AmbientStatus,
    FirstCrackStatus,
    MCPConnectionError,
    MCPServerProcess,
    RoastSessionState,
)
from roastpilot_agent.models import (
    BeanProfileDraft,
    BeanProfileInput,
    ChargeWeightRequest,
    ClearStaleSessionRequest,
    HardwareClearAcknowledgementRequest,
    MicHealth,
    OperatorAction,
    OperatorActionRequest,
    PostFcHeatAuthorityState,
    ReferenceRoast,
    RoastCommand,
    RoastDetail,
    RoastedWeightRequest,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
    SseEvent,
    SseEventType,
    TelemetryEventData,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict, enabled_operator_actions
from roastpilot_agent.store import FrozenRunConfig, PersistedRun, RoastStore
from tests.conftest import FakeClock, FakeMCPClient


def _profile(
    *, name: str = "House Espresso", origin: str = "Ethiopia", varietal: str | None = "Heirloom"
) -> RoastProfile:
    return RoastProfile(
        name=name,
        bean_origin=origin,
        bean_varietal=varietal,
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    instance = RoastStore(tmp_path / "api.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def service(store: RoastStore) -> RoastService:
    return RoastService(store)


@pytest_asyncio.fixture
async def client(service: RoastService) -> AsyncIterator[AsyncClient]:
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        yield instance
    await service.shutdown()


# --- health ---


@pytest.mark.asyncio
async def test_health_reports_version_and_no_active_run(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["mcp_child"] == "not_configured"
    assert body["active_run_id"] is None


@pytest.mark.asyncio
async def test_health_advisor_is_null_until_probed(client: AsyncClient) -> None:
    """``advisor`` is null on /api/health until a startup probe records one
    (issue #168) — the E7 contract path has no advisor reachability yet."""
    body = (await client.get("/api/health")).json()
    assert body["advisor"] is None


@pytest.mark.asyncio
async def test_health_surfaces_recorded_advisor_probe(store: RoastStore) -> None:
    """A recorded reachability probe is surfaced on /api/health so the dashboard
    can render an ADVISOR-OFFLINE state (issue #168)."""
    from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus

    service = RoastService(store)
    service.set_advisor_health(
        AdvisorHealth(
            status=AdvisorHealthStatus.UNREACHABLE,
            provider="openai_compatible",
            model_slug="anthropic/claude-opus-4.8",
            error="401 Unauthorized",
        )
    )
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        body = (await instance.get("/api/health")).json()
    assert body["advisor"]["status"] == "unreachable"
    assert body["advisor"]["error"] == "401 Unauthorized"
    assert body["advisor"]["model_slug"] == "anthropic/claude-opus-4.8"


@pytest.mark.asyncio
async def test_health_reports_active_run(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-active",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    response = await client.get("/api/health")
    assert response.json()["active_run_id"] == "run-active"


class _FakeSession:
    """Minimal ToolSession so MCPServerProcess.running reports attached."""

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        return {}


@pytest.mark.asyncio
async def test_health_reports_mcp_child_running(store: RoastStore) -> None:
    service = RoastService(store, mcp=MCPServerProcess(session=_FakeSession()))
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        response = await instance.get("/api/health")
    assert response.json()["mcp_child"] == "running"


@pytest.mark.asyncio
async def test_health_reports_mcp_child_stopped(store: RoastStore) -> None:
    service = RoastService(store, mcp=MCPServerProcess())
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        response = await instance.get("/api/health")
    assert response.json()["mcp_child"] == "stopped"


@pytest.mark.asyncio
async def test_health_works_without_a_service() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        response = await bare.get("/api/health")
    assert response.status_code == 200
    assert response.json()["mcp_child"] == "not_configured"


@pytest.mark.asyncio
async def test_health_instance_id_is_present_and_stable_across_requests(
    client: AsyncClient,
) -> None:
    """#516: instance_id is a non-empty string, and the SAME process answers
    identically across repeated polls (minted once at construction, not
    re-minted per request — a normal poll must never look like a process
    change)."""
    first = (await client.get("/api/health")).json()
    second = (await client.get("/api/health")).json()
    assert isinstance(first["instance_id"], str)
    assert first["instance_id"] != ""
    assert first["instance_id"] == second["instance_id"]


@pytest.mark.asyncio
async def test_health_instance_id_differs_across_two_service_instances(
    store: RoastStore,
) -> None:
    """#516: two RoastService instances (the shape of two racing processes —
    or an impostor bound to the same port, #513) mint DIFFERENT instance
    ids — the whole basis of the confirm-loop's mismatch detection."""
    service_a = RoastService(store)
    service_b = RoastService(store)
    assert service_a.instance_id != service_b.instance_id


@pytest.mark.asyncio
async def test_health_instance_id_present_without_a_service() -> None:
    """#516: the scaffold-fallback health handler (no RoastService configured)
    still reports a non-empty instance_id — the field is never absent,
    matching the issue's explicit "keep the scaffold-fallback handler
    consistent" requirement."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        response = await bare.get("/api/health")
    body = response.json()
    assert isinstance(body["instance_id"], str)
    assert body["instance_id"] != ""


@pytest.mark.asyncio
async def test_health_instance_id_stable_across_requests_without_a_service() -> None:
    """#516: the scaffold-fallback instance_id is minted ONCE at module
    import, not per request — two bare-app requests (even across separate
    ASGITransport instances, since it's a module-level constant) see the
    identical id."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        first = (await bare.get("/api/health")).json()
        second = (await bare.get("/api/health")).json()
    assert first["instance_id"] == second["instance_id"]


@pytest.mark.asyncio
async def test_store_backed_route_503_without_service() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        response = await bare.get("/api/roasts")
    assert response.status_code == 503


# --- start roast / 409 ---


@pytest.mark.asyncio
async def test_start_roast_creates_an_active_run(client: AsyncClient) -> None:
    response = await client.post("/api/roasts", json=_profile().model_dump())
    assert response.status_code == 201
    body = response.json()
    assert body["agent_phase"] == "starting"
    assert body["profile"]["bean_origin"] == "Ethiopia"
    run_id = body["id"]
    health = await client.get("/api/health")
    assert health.json()["active_run_id"] == run_id


@pytest.mark.asyncio
async def test_start_roast_response_carries_this_process_instance_id(
    client: AsyncClient,
) -> None:
    """#516: the 201 response's instance_id is the confirm-loop's capture
    point — it must equal THIS process's own health instance_id, the value a
    subsequent fresh health read on arrival at /live is compared against."""
    started = await client.post("/api/roasts", json=_profile().model_dump())
    assert started.status_code == 201
    health = await client.get("/api/health")
    assert started.json()["instance_id"] == health.json()["instance_id"]
    assert started.json()["instance_id"] != ""


@pytest.mark.asyncio
async def test_get_roast_detail_does_not_carry_instance_id(
    client: AsyncClient,
) -> None:
    """#516: instance_id is scoped to the start-roast confirm point (the 201
    response), not a general RoastDetail field — GET /api/roasts/{id} (a
    historical/general read) leaves it null, so the field's presence
    specifically signals "this is the response to a start you just issued",
    not "this server is currently live"."""
    started = await client.post("/api/roasts", json=_profile().model_dump())
    run_id = started.json()["id"]
    detail = await client.get(f"/api/roasts/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["instance_id"] is None


@pytest.mark.asyncio
async def test_start_roast_carries_bean_identity_to_detail(client: AsyncClient) -> None:
    """#164: the richer bean-identity fields flow through ``POST /api/roasts``
    and back out on the detail projection (the SPA renders from server data)."""
    profile = _profile().model_dump()
    profile.update(
        {
            "country": "Ethiopia",
            "farm": "Gedeb — Worka Sakaro",
            "description": "Washed; 70% this + 30% Brazil natural.",
            "bean_species": "arabica",
            "is_blend": True,
            "processing": "natural",
            "altitude_m": 2100,
        }
    )
    created = await client.post("/api/roasts", json=profile)
    assert created.status_code == 201
    run_id = created.json()["id"]

    detail = await client.get(f"/api/roasts/{run_id}")
    assert detail.status_code == 200
    body_profile = detail.json()["profile"]
    assert body_profile["country"] == "Ethiopia"
    assert body_profile["farm"] == "Gedeb — Worka Sakaro"
    assert body_profile["description"] == "Washed; 70% this + 30% Brazil natural."
    assert body_profile["bean_species"] == "arabica"
    assert body_profile["is_blend"] is True
    # #291: processing + altitude flow through to the detail projection too.
    assert body_profile["processing"] == "natural"
    assert body_profile["altitude_m"] == 2100


@pytest.mark.asyncio
async def test_start_roast_carries_source_url_to_detail(client: AsyncClient) -> None:
    """#315: the bean's product/source URL flows through ``POST /api/roasts`` and
    back out on the detail projection so the SPA can render it as a link."""
    profile = _profile().model_dump()
    profile["source_url"] = "https://redber.co.uk/products/ethiopia-yirgacheffe-koke"
    created = await client.post("/api/roasts", json=profile)
    assert created.status_code == 201
    run_id = created.json()["id"]

    detail = await client.get(f"/api/roasts/{run_id}")
    assert detail.status_code == 200
    assert (
        detail.json()["profile"]["source_url"]
        == "https://redber.co.uk/products/ethiopia-yirgacheffe-koke"
    )


@pytest.mark.asyncio
async def test_start_roast_rejects_malformed_source_url(client: AsyncClient) -> None:
    """#315: a non-http(s) source_url is rejected at the API boundary (422), so a
    broken link can never reach the corpus or the UI."""
    profile = _profile().model_dump()
    profile["source_url"] = "javascript:alert(1)"
    created = await client.post("/api/roasts", json=profile)
    assert created.status_code == 422


@pytest.mark.asyncio
async def test_history_summary_projects_bean_identity(
    client: AsyncClient, store: RoastStore
) -> None:
    """#164: the history list projects country / species / blend marker from the
    frozen profile so the table can show them without opening each run."""
    profile = _profile().model_dump()
    profile.update(
        {
            "country": "Colombia",
            "bean_species": "arabica",
            "is_blend": True,
            "processing": "honey",
            "altitude_m": 1600,
        }
    )
    created = await client.post("/api/roasts", json=profile)
    run_id = created.json()["id"]
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)

    history = await client.get("/api/roasts")
    assert history.status_code == 200
    row = next(r for r in history.json()["runs"] if r["id"] == run_id)
    assert row["country"] == "Colombia"
    assert row["bean_species"] == "arabica"
    assert row["is_blend"] is True
    assert row["processing"] == "honey"
    assert row["altitude_m"] == 1600


@pytest.mark.asyncio
async def test_start_roast_conflicts_when_a_run_is_active(client: AsyncClient) -> None:
    first = await client.post("/api/roasts", json=_profile().model_dump())
    assert first.status_code == 201
    second = await client.post("/api/roasts", json=_profile(name="Second").model_dump())
    assert second.status_code == 409
    assert "already active" in second.json()["detail"]


@pytest.mark.asyncio
async def test_start_roast_conflicts_after_service_restart(
    client: AsyncClient, store: RoastStore
) -> None:
    """The 409 guard reads persisted state, so it survives a restart: a fresh
    RoastService (empty in-memory pointer) over the same store still 409s."""
    first = await client.post("/api/roasts", json=_profile().model_dump())
    assert first.status_code == 201

    restarted = RoastService(store)  # no in-memory active_run_id
    assert restarted.active_run_id is None
    app = create_app(restarted)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as fresh:
        response = await fresh.post("/api/roasts", json=_profile(name="After").model_dump())
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_start_roast_allowed_after_prior_run_completes(
    client: AsyncClient, store: RoastStore
) -> None:
    first = await client.post("/api/roasts", json=_profile().model_dump())
    run_id = first.json()["id"]
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    second = await client.post("/api/roasts", json=_profile(name="Next").model_dump())
    assert second.status_code == 201


@pytest.mark.asyncio
async def test_start_roast_rejects_invalid_profile(client: AsyncClient) -> None:
    response = await client.post("/api/roasts", json={"name": "bad"})
    assert response.status_code == 422


# --- #668 unconfirmed-teardown hardware-clear acknowledgement ---


@pytest.mark.asyncio
async def test_start_roast_requires_pending_hardware_clear_acknowledgement(
    store: RoastStore,
) -> None:
    """A matching saved device config cannot bypass teardown uncertainty."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    roaster = mock.Mock()
    service = RoastService(store, mcp=process, roaster=roaster)
    service.set_spawned_mcp_device(MCPDeviceConfig())

    with pytest.raises(RoastRunConflictError, match="hardware-clear acknowledgement"):
        await service.start_roast(_profile())

    assert await store.active_run() is None
    assert service.active_run_id is None
    assert process.stop_unconfirmed is True
    assert process.teardown_incident_id == "a" * 32
    assert roaster.mock_calls == []


@pytest.mark.asyncio
async def test_acknowledge_hardware_clear_audits_before_clearing_process_state(
    store: RoastStore,
) -> None:
    """The explicit decision is durable before stale generation state clears."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    roaster = mock.Mock()
    service = RoastService(store, mcp=process, roaster=roaster)
    app = create_app(service)
    transport = ASGITransport(app=app)

    before = await service.health()
    assert before.mcp_hardware_clear_required is True
    assert before.mcp_teardown_incident_id == "a" * 32

    original = process.acknowledge_hardware_clear
    audit = mock.AsyncMock(wraps=store.record_operator_action)

    def _assert_audit_then_clear(teardown_incident_id: str) -> None:
        assert audit.await_count == 1
        awaited = audit.await_args
        assert awaited is not None
        assert awaited.kwargs["action"] == "acknowledge_mcp_hardware_clear"
        assert awaited.kwargs["result"] == "accepted"
        original(teardown_incident_id)

    with (
        mock.patch.object(store, "record_operator_action", audit),
        mock.patch.object(process, "acknowledge_hardware_clear", _assert_audit_then_clear),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as instance:
            response = await instance.post(
                "/api/mcp/acknowledge-hardware-clear",
                json={
                    "hardware_clear": True,
                    "teardown_incident_id": "a" * 32,
                    "reason": "  roaster cold; ports released  ",
                },
            )

    assert response.status_code == 200
    assert response.json() == {
        "result": "accepted",
        "hardware_clear": True,
        "teardown_incident_id": "a" * 32,
        "fresh_spawn_permitted": True,
    }
    assert process.stop_unconfirmed is False
    after = await service.health()
    assert after.mcp_hardware_clear_required is False
    assert after.mcp_teardown_incident_id is None
    assert roaster.mock_calls == []
    async with store.connection.execute(
        "SELECT payload_json FROM operator_actions ORDER BY id DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert json.loads(str(row[0])) == {
        "hardware_clear": True,
        "reason": "roaster cold; ports released",
        "teardown_incident_id": "a" * 32,
    }


@pytest.mark.asyncio
async def test_acknowledge_hardware_clear_rejects_active_recovery_and_replay(
    store: RoastStore,
) -> None:
    """A persisted recovery run and a duplicate acknowledgement both 409."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    clock = FakeClock()
    service = RoastService(store, mcp=process, clock=clock)
    request = HardwareClearAcknowledgementRequest(
        hardware_clear=True,
        teardown_incident_id="a" * 32,
        reason="physical controls verified off",
    )
    await store.create_run(
        run_id="run-recovery-block",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    )

    with pytest.raises(RoastRunConflictError, match="cannot bypass"):
        await service.acknowledge_hardware_clear(request)
    assert process.stop_unconfirmed is True
    clock.advance(1.0)

    await store.complete_run(
        run_id="run-recovery-block",
        outcome="aborted",
        agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    )
    # The process pointer deliberately survives finalization; persisted state
    # is authoritative for whether the run is still active/recovering.
    service.active_run_id = "run-recovery-block"
    accepted = await service.acknowledge_hardware_clear(request)
    assert accepted.fresh_spawn_permitted is True
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "b" * 32  # pyright: ignore[reportPrivateUsage]
    clock.advance(1.0)
    with pytest.raises(RoastRunConflictError, match="does not match"):
        await service.acknowledge_hardware_clear(request)
    assert process.stop_unconfirmed is True
    assert process.teardown_incident_id == "b" * 32
    clock.advance(1.0)
    second = await service.acknowledge_hardware_clear(
        request.model_copy(update={"teardown_incident_id": "b" * 32})
    )
    assert second.teardown_incident_id == "b" * 32

    async with store.connection.execute(
        "SELECT result FROM operator_actions"
        " WHERE action = 'acknowledge_mcp_hardware_clear' ORDER BY id"
    ) as cursor:
        rows = await cursor.fetchall()
    assert [str(row[0]) for row in rows] == ["accepted", "accepted"]


@pytest.mark.asyncio
async def test_acknowledge_hardware_clear_requires_configured_mcp(store: RoastStore) -> None:
    """API-only mode cannot acknowledge a lifecycle it does not own."""
    service = RoastService(store)
    request = HardwareClearAcknowledgementRequest(
        hardware_clear=True,
        teardown_incident_id="a" * 32,
        reason="physical controls verified off",
    )

    with pytest.raises(RoastRunConflictError, match="no MCP child lifecycle"):
        await service.acknowledge_hardware_clear(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "hardware_clear": False,
            "teardown_incident_id": "a" * 32,
            "reason": "not actually checked",
        },
        {
            "hardware_clear": 1,
            "teardown_incident_id": "a" * 32,
            "reason": "numeric coercion is forbidden",
        },
        {
            "hardware_clear": 0,
            "teardown_incident_id": "a" * 32,
            "reason": "numeric coercion is forbidden",
        },
        {
            "hardware_clear": "true",
            "teardown_incident_id": "a" * 32,
            "reason": "string coercion is forbidden",
        },
        {"hardware_clear": True, "teardown_incident_id": "a" * 32, "reason": "   "},
        {"hardware_clear": True, "teardown_incident_id": "a" * 32, "reason": "x" * 501},
        {"hardware_clear": True, "teardown_incident_id": "wrong", "reason": "checked"},
    ],
)
async def test_acknowledge_hardware_clear_requires_bounded_explicit_confirmation(
    store: RoastStore, payload: dict[str, object]
) -> None:
    """A generic retry, empty reason, or oversized audit text cannot confirm."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    app = create_app(RoastService(store, mcp=process))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        response = await instance.post(
            "/api/mcp/acknowledge-hardware-clear",
            json=payload,
        )
    assert response.status_code == 422
    assert process.stop_unconfirmed is True
    assert (await RoastService(store, mcp=process).health()).mcp_hardware_clear_required is True


@pytest.mark.asyncio
async def test_acknowledge_hardware_clear_rate_limits_without_audit_growth(
    store: RoastStore,
) -> None:
    """Rejected probes neither queue freely nor append durable audit rows."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    clock = FakeClock()
    service = RoastService(store, mcp=process, clock=clock)
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        stale = await instance.post(
            "/api/mcp/acknowledge-hardware-clear",
            json={
                "hardware_clear": True,
                "teardown_incident_id": "b" * 32,
                "reason": "stale request",
            },
        )
        limited = await instance.post(
            "/api/mcp/acknowledge-hardware-clear",
            json={
                "hardware_clear": True,
                "teardown_incident_id": "a" * 32,
                "reason": "physical controls verified off",
            },
        )
        clock.advance(1.0)
        oversized = await instance.post(
            "/api/mcp/acknowledge-hardware-clear",
            content=b"x" * 2049,
            headers={"content-type": "application/json"},
        )

    assert stale.status_code == 409
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "1"
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "request body exceeds 2048-byte limit"}
    async with store.connection.execute(
        "SELECT COUNT(*) FROM operator_actions WHERE action = 'acknowledge_mcp_hardware_clear'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert int(row[0]) == 0


@pytest.mark.asyncio
async def test_acknowledge_hardware_clear_audits_post_check_race_failure(
    store: RoastStore,
) -> None:
    """A lifecycle change after preflight is durably recorded and retryable."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    clock = FakeClock()
    service = RoastService(store, mcp=process, clock=clock)
    service._spawned_mcp_device = MCPDeviceConfig()  # pyright: ignore[reportPrivateUsage]
    request = HardwareClearAcknowledgementRequest(
        hardware_clear=True,
        teardown_incident_id="a" * 32,
        reason="physical controls verified off",
    )

    with (
        mock.patch.object(
            process,
            "acknowledge_hardware_clear",
            side_effect=MCPConnectionError("generation changed after audit"),
        ),
        pytest.raises(RoastRunConflictError, match="generation changed"),
    ):
        await service.acknowledge_hardware_clear(request)

    assert process.stop_unconfirmed is True
    assert process.teardown_incident_id == "a" * 32
    assert service._spawned_mcp_device == MCPDeviceConfig()  # pyright: ignore[reportPrivateUsage]
    async with store.connection.execute(
        "SELECT result, payload_json FROM operator_actions"
        " WHERE action = 'acknowledge_mcp_hardware_clear' ORDER BY id"
    ) as cursor:
        rows = await cursor.fetchall()
    assert [str(row[0]) for row in rows] == ["accepted", "failed"]
    assert all(json.loads(str(row[1]))["teardown_incident_id"] == "a" * 32 for row in rows)

    clock.advance(1.0)
    accepted = await service.acknowledge_hardware_clear(request)
    assert accepted.fresh_spawn_permitted is True


@pytest.mark.asyncio
async def test_acknowledge_hardware_clear_rejects_concurrent_request_without_queueing(
    store: RoastStore,
) -> None:
    """Only one acknowledgement may wait on the roast-start critical section."""
    process = MCPServerProcess()
    process._stop_unconfirmed = True  # pyright: ignore[reportPrivateUsage]
    process._teardown_incident_id = "a" * 32  # pyright: ignore[reportPrivateUsage]
    clock = FakeClock()
    service = RoastService(store, mcp=process, clock=clock)
    app = create_app(service)
    transport = ASGITransport(app=app)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_active_run = store.active_run

    async def _paused_active_run() -> PersistedRun | None:
        entered.set()
        await release.wait()
        return await original_active_run()

    payload = {
        "hardware_clear": True,
        "teardown_incident_id": "a" * 32,
        "reason": "physical controls verified off",
    }
    with mock.patch.object(store, "active_run", _paused_active_run):
        async with AsyncClient(transport=transport, base_url="http://test") as instance:
            first_task = asyncio.create_task(
                instance.post("/api/mcp/acknowledge-hardware-clear", json=payload)
            )
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            competing = await asyncio.wait_for(
                instance.post("/api/mcp/acknowledge-hardware-clear", json=payload),
                timeout=0.5,
            )
            release.set()
            first = await asyncio.wait_for(first_task, timeout=0.5)
            clock.advance(1.0)
            later = await instance.post("/api/mcp/acknowledge-hardware-clear", json=payload)

    assert competing.status_code == 429
    assert first.status_code == 200
    assert later.status_code == 409  # admitted after release; incident already consumed
    async with store.connection.execute(
        "SELECT COUNT(*) FROM operator_actions WHERE action = 'acknowledge_mcp_hardware_clear'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert int(row[0]) == 1


# --- history / detail ---


@pytest.mark.asyncio
async def test_history_lists_runs_newest_first_with_summary_fields(
    client: AsyncClient, store: RoastStore
) -> None:
    config = AppConfig()
    await store.create_run(
        run_id="run-old",
        profile=_profile(name="Old", origin="Kenya"),
        config=config,
        agent_phase=RoastPhase.COMPLETE,
        started_at_utc="2026-06-01T00:00:00+00:00",
    )
    await store.complete_run(run_id="run-old", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.set_operator_rating("run-old", rating=4, notes="good")
    await store.create_run(
        run_id="run-new",
        profile=_profile(name="New", origin="Brazil"),
        config=config,
        agent_phase=RoastPhase.PREHEATING,
        started_at_utc="2026-06-05T00:00:00+00:00",
    )
    await store.record_telemetry(
        run_id="run-new",
        tick=0,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=0.0,
        interval_seconds=1.0,
        telemetry=RoastTelemetry(bean_temp_c=200.0, env_temp_c=210.0),
        development_percent=18.5,
    )

    response = await client.get("/api/roasts")
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert [r["id"] for r in runs] == ["run-new", "run-old"]
    new, old = runs
    assert new["bean_origin"] == "Brazil"
    assert new["development_percent"] == 18.5
    assert new["outcome"] is None
    assert old["rating"] == 4
    assert old["outcome"] == "completed"
    assert old["development_percent"] is None


@pytest.mark.asyncio
async def test_get_roast_detail_and_404(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-1",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    found = await client.get("/api/roasts/run-1")
    assert found.status_code == 200
    assert found.json()["profile"]["name"] == "House Espresso"
    # The snapshot carries enabled_actions for the run's phase (E10 option (a),
    # D25) — the permission mirror the SPA's action bar reads.
    assert found.json()["enabled_actions"] == [
        a.value for a in enabled_operator_actions(RoastPhase.PREHEATING)
    ]

    missing = await client.get("/api/roasts/nope")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_typed_roast_detail_projects_persisted_non_finite_float_as_null(
    service: RoastService, store: RoastStore
) -> None:
    """The store model and typed response both project a legacy REAL as absent."""
    await store.create_run(
        run_id="run-non-finite",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.connection.execute(
        "UPDATE roast_runs SET ambient_pressure_hpa = ? WHERE id = ?",
        (float("inf"), "run-non-finite"),
    )
    await store.connection.commit()

    transport = ASGITransport(app=create_app(service), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        response = await instance.get("/api/roasts/run-non-finite")
        history = await instance.get("/api/roasts")

    assert response.status_code == 200
    assert response.json()["ambient_pressure_hpa"] is None

    assert history.status_code == 200
    [summary] = history.json()["runs"]
    assert summary["ambient_pressure_hpa"] is None


def test_finite_json_response_matches_starlette_for_finite_content() -> None:
    """The custom REST sink is byte-identical for ordinary JSON content."""
    content = {"unicode": "café", "nested": [1.25, None, {"ok": True}]}
    assert FiniteJSONResponse(content).body == JSONResponse(content).body


def test_finite_json_response_replaces_nested_non_finite_floats() -> None:
    """Direct response construction replaces every non-finite float with null."""

    def reject_constant(token: str) -> object:
        raise ValueError(f"non-finite JSON token: {token}")

    response = FiniteJSONResponse(
        {
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "nested": {"negative_infinity": float("-inf")},
            "items": [{"value": float("nan")}, float("inf")],
        }
    )
    payload = json.loads(bytes(response.body), parse_constant=reject_constant)
    assert payload == {
        "nan": None,
        "positive_infinity": None,
        "nested": {"negative_infinity": None},
        "items": [{"value": None}, None],
    }


@pytest.mark.asyncio
async def test_finite_json_response_protects_an_untyped_route() -> None:
    """A route without FastAPI's typed fast path can opt in to strict JSON."""

    def reject_constant(token: str) -> object:
        raise ValueError(f"non-finite JSON token: {token}")

    app = FastAPI(default_response_class=FiniteJSONResponse)

    @app.get("/untyped")
    async def untyped():  # pyright: ignore[reportUnusedFunction]
        return {"value": float("nan"), "nested": [float("inf"), float("-inf")]}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        response = await instance.get("/untyped")

    assert response.status_code == 200
    assert json.loads(response.text, parse_constant=reject_constant) == {
        "value": None,
        "nested": [None, None],
    }


# --- telemetry (downsample) ---


async def _seed_telemetry(store: RoastStore, run_id: str, count: int) -> None:
    config = AppConfig()
    await store.create_run(
        run_id=run_id, profile=_profile(), config=config, agent_phase=RoastPhase.DEVELOPMENT
    )
    for tick in range(count):
        await store.record_telemetry(
            run_id=run_id,
            tick=tick,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=float(tick),
            interval_seconds=1.0,
            telemetry=RoastTelemetry(bean_temp_c=190.0 + tick, env_temp_c=205.0),
            heat_level_percent=60,
            fan_level_percent=40,
        )


@pytest.mark.asyncio
async def test_telemetry_default_returns_every_snapshot(
    client: AsyncClient, store: RoastStore
) -> None:
    await _seed_telemetry(store, "run-t", 5)
    response = await client.get("/api/roasts/run-t/telemetry")
    assert response.status_code == 200
    body = response.json()
    assert body["downsample"] == 1
    assert body["point_count"] == 5
    assert [p["tick"] for p in body["points"]] == [0, 1, 2, 3, 4]
    assert body["points"][0]["heat_level_percent"] == 60


@pytest.mark.asyncio
async def test_telemetry_and_timeline_sanitize_persisted_non_finite_floats(
    client: AsyncClient, store: RoastStore
) -> None:
    """Legacy telemetry and event payloads stay available through JSON endpoints."""
    await _seed_telemetry(store, "run-non-finite-series", 1)
    await store.connection.execute(
        "UPDATE telemetry_snapshots SET bean_ror_c_per_min = ? WHERE run_id = ?",
        (float("-inf"), "run-non-finite-series"),
    )
    await store.record_event(
        run_id="run-non-finite-series",
        kind=RoastEventKind.SAFETY_ALERT,
        source=RoastEventSource.SAFETY,
        payload={"nested": {"value": float("nan")}},
    )
    await store.connection.commit()

    telemetry = await client.get("/api/roasts/run-non-finite-series/telemetry")
    assert telemetry.status_code == 200
    assert telemetry.json()["points"][0]["bean_ror_c_per_min"] is None

    timeline = await client.get("/api/roasts/run-non-finite-series/timeline")
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["payload"] == {"nested": {"value": None}}

    missing = await client.get("/api/roasts/nope/telemetry")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_telemetry_downsample_strides_and_keeps_first(
    client: AsyncClient, store: RoastStore
) -> None:
    await _seed_telemetry(store, "run-t", 5)
    response = await client.get("/api/roasts/run-t/telemetry", params={"downsample": 2})
    body = response.json()
    assert body["downsample"] == 2
    assert [p["tick"] for p in body["points"]] == [0, 2, 4]


@pytest.mark.asyncio
async def test_telemetry_rejects_downsample_below_one(
    client: AsyncClient, store: RoastStore
) -> None:
    await _seed_telemetry(store, "run-t", 1)
    response = await client.get("/api/roasts/run-t/telemetry", params={"downsample": 0})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_telemetry_404_for_unknown_run(client: AsyncClient) -> None:
    response = await client.get("/api/roasts/nope/telemetry")
    assert response.status_code == 404


# --- timeline (decision trace) ---


@pytest.mark.asyncio
async def test_timeline_returns_the_full_decision_trace(
    client: AsyncClient, store: RoastStore
) -> None:
    config = AppConfig()
    await store.create_run(
        run_id="run-x", profile=_profile(), config=config, agent_phase=RoastPhase.DEVELOPMENT
    )
    await store.record_event(
        run_id="run-x",
        kind=RoastEventKind.PHASE_CHANGED,
        source=RoastEventSource.CONTROLLER,
        payload={"phase": "development"},
    )
    eval_id = await store.record_safety_evaluation(
        run_id="run-x",
        tick=3,
        evaluation=SafetyEvaluation(
            rule="command_bounds",
            verdict=SafetyVerdict.CLAMP,
            input_heat=150,
            input_fan=40,
            adjusted_heat=100,
            adjusted_fan=40,
            reason="clamped",
        ),
    )
    await store.record_advisor_decision(
        run_id="run-x",
        tick=3,
        provider="openrouter",
        model="test-model",
        prompt_version="v0",
        context=AdvisorContext(
            phase=RoastPhase.DEVELOPMENT,
            roast_elapsed_seconds=120.0,
            development_elapsed_seconds=10.0,
            current_bean_temp_c=200.0,
            current_env_temp_c=210.0,
            bean_ror_c_per_min=5.0,
            env_ror_c_per_min=4.0,
            target_drop_temp_c=205.0,
            profile_name="House Espresso",
        ),
        latency_ms=42,
        decision=RoastDecision(
            target_heat=60, target_fan=40, should_drop=False, confidence=0.8, rationale="hold"
        ),
        status="ok",
        safety_evaluation_id=eval_id,
    )
    await store.record_command(
        run_id="run-x",
        tick=3,
        tool=RoastCommand.SET_HEAT,
        source="advisor",
        status="ok",
        args={"heat_level_percent": 60},
        safety_evaluation_id=eval_id,
    )

    response = await client.get("/api/roasts/run-x/timeline")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-x"
    assert body["events"][0]["kind"] == "phase_changed"
    assert body["events"][0]["payload"] == {"phase": "development"}
    assert body["safety_evaluations"][0]["verdict"] == "clamp"
    assert body["safety_evaluations"][0]["adjusted_heat"] == 100
    assert body["advisor_decisions"][0]["status"] == "ok"
    assert body["advisor_decisions"][0]["decision"]["target_heat"] == 60
    assert body["commands"][0]["tool"] == "set_heat"
    assert body["commands"][0]["args"] == {"heat_level_percent": 60}


@pytest.mark.asyncio
async def test_timeline_404_for_unknown_run(client: AsyncClient) -> None:
    response = await client.get("/api/roasts/nope/timeline")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_timeline_wraps_non_dict_event_payload(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-w",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    await store.record_event(
        run_id="run-w",
        kind=RoastEventKind.RUN_COMPLETED,
        source=RoastEventSource.CONTROLLER,
        payload=["finished", 1],
    )
    body = (await client.get("/api/roasts/run-w/timeline")).json()
    assert body["events"][0]["payload"] == {"value": ["finished", 1]}


@pytest.mark.asyncio
async def test_read_telemetry_points_rejects_downsample_below_one(store: RoastStore) -> None:
    await _seed_telemetry(store, "run-z", 1)
    with pytest.raises(ValueError, match="downsample"):
        await store.read_telemetry_points("run-z", downsample=0)


# --- log manifest + downloads ---


async def _complete_with_manifest(
    store: RoastStore, run_id: str, log_dir: Path, *, ready: bool = True
) -> dict[str, str | bool]:
    config = AppConfig()
    await store.create_run(
        run_id=run_id, profile=_profile(), config=config, agent_phase=RoastPhase.COOLING
    )
    jsonl = log_dir / "roast.jsonl"
    csv = log_dir / "roast.csv"
    summary = log_dir / "summary.json"
    for path in (jsonl, csv, summary):
        path.write_text(f"contents of {path.name}", encoding="utf-8")
    manifest: dict[str, str | bool] = {
        "session_id": "sess-1",
        "log_dir": str(log_dir),
        "jsonl_path": str(jsonl),
        "csv_path": str(csv),
        "summary_path": str(summary),
        "ready": ready,
        "note": "exported",
    }
    await store.complete_run(
        run_id=run_id,
        outcome="completed",
        agent_phase=RoastPhase.COMPLETE,
        log_dir=str(log_dir),
        export_manifest=manifest,
    )
    return manifest


@pytest.mark.asyncio
async def test_log_manifest_returned(
    client: AsyncClient, store: RoastStore, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs-run-l"
    log_dir.mkdir()
    await _complete_with_manifest(store, "run-l", log_dir)

    manifest = await client.get("/api/roasts/run-l/log")
    assert manifest.status_code == 200
    assert manifest.json()["ready"] is True
    assert manifest.json()["jsonl_path"].endswith("roast.jsonl")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact", "filename"),
    [("jsonl", "roast.jsonl"), ("csv", "roast.csv"), ("summary", "summary.json")],
)
async def test_log_artifact_downloads(
    client: AsyncClient, store: RoastStore, tmp_path: Path, artifact: str, filename: str
) -> None:
    log_dir = tmp_path / f"logs-run-dl-{artifact}"
    log_dir.mkdir()
    await _complete_with_manifest(store, f"run-dl-{artifact}", log_dir)

    download = await client.get(f"/api/roasts/run-dl-{artifact}/log/{artifact}")
    assert download.status_code == 200
    assert download.text == f"contents of {filename}"


@pytest.mark.asyncio
async def test_log_manifest_404_for_unknown_run(client: AsyncClient) -> None:
    response = await client.get("/api/roasts/nope/log")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_log_manifest_404_without_export(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-noexp",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    response = await client.get("/api/roasts/run-noexp/log")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_unknown_artifact_404(
    client: AsyncClient, store: RoastStore, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs-run-a"
    log_dir.mkdir()
    await _complete_with_manifest(store, "run-a", log_dir)
    response = await client.get("/api/roasts/run-a/log/png")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_404_when_export_not_ready(
    client: AsyncClient, store: RoastStore, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs-run-nr"
    log_dir.mkdir()
    await _complete_with_manifest(store, "run-nr", log_dir, ready=False)
    response = await client.get("/api/roasts/run-nr/log/jsonl")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_download_404_when_file_missing(
    client: AsyncClient, store: RoastStore, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs-run-gone"
    log_dir.mkdir()
    await _complete_with_manifest(store, "run-gone", log_dir)
    (log_dir / "roast.csv").unlink()
    response = await client.get("/api/roasts/run-gone/log/csv")
    assert response.status_code == 404


# --- rating ---


@pytest.mark.asyncio
async def test_rate_completed_run(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-r",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-r/rating", json={"stars": 5, "notes": "excellent"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rating"] == 5
    assert body["notes"] == "excellent"


@pytest.mark.asyncio
async def test_rate_in_progress_run_conflicts(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-ip",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    response = await client.post("/api/roasts/run-ip/rating", json={"stars": 3})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_rate_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post("/api/roasts/nope/rating", json={"stars": 3})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rate_rejects_out_of_range_stars(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-b",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-b", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post("/api/roasts/run-b/rating", json={"stars": 6})
    assert response.status_code == 422


# --- roasted weight (#388) ---


@pytest.mark.asyncio
async def test_set_roasted_weight_completed_run(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-w",
        profile=_profile(),  # bean_weight_grams 250
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-w", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-w/roasted-weight", json={"roasted_weight_grams": 221.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["roasted_weight_grams"] == 221.0
    assert body["weight_loss_percent"] == 11.6  # (250 - 221) / 250 * 100


@pytest.mark.asyncio
async def test_set_roasted_weight_in_progress_conflicts(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-wip",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    response = await client.post(
        "/api/roasts/run-wip/roasted-weight", json={"roasted_weight_grams": 221.0}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_set_roasted_weight_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/roasts/nope/roasted-weight", json={"roasted_weight_grams": 221.0}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_set_roasted_weight_rejects_non_positive(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-wb",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-wb", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-wb/roasted-weight", json={"roasted_weight_grams": 0}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_set_roasted_weight_rejects_over_charge(
    client: AsyncClient, store: RoastStore
) -> None:
    """A roasted weight above the charge weight is physically impossible → 409."""
    await store.create_run(
        run_id="run-oc",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-oc", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-oc/roasted-weight", json={"roasted_weight_grams": 9999.0}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_set_roasted_weight_bounds_against_a_prior_charge_correction(
    client: AsyncClient, store: RoastStore
) -> None:
    """#520 safety review: set_roasted_weight must bound against the EFFECTIVE
    charge (corrected when present), not just the frozen profile default —
    otherwise correcting the charge DOWN first, then weighing above the new
    (but below the old frozen) value, would silently pass the frozen-only
    check and leave weight_loss_percent null on a run with both values
    entered. Reproduces the exact ordering from the review: correct charge to
    200g (accepted, un-weighed) on a 250g-frozen run, then weigh 210g — must
    409 (210 > the effective 200g charge), not silently pass against 250g."""
    await store.create_run(
        run_id="run-order",
        profile=_profile(),  # bean_weight_grams 250
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-order", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    correction = await client.post(
        "/api/roasts/run-order/charge-weight", json={"corrected_charge_grams": 200.0}
    )
    assert correction.status_code == 200

    response = await client.post(
        "/api/roasts/run-order/roasted-weight", json={"roasted_weight_grams": 210.0}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_set_roasted_weight_races_a_concurrent_charge_correction_still_409s(
    store: RoastStore,
) -> None:
    """#520 round-2 P3: the API-layer pre-check reads `detail` then writes
    moments later — a concurrent set_corrected_charge landing in BETWEEN
    those two steps would invalidate the value the pre-check validated
    against. Monkeypatches `store.read_run` to inject exactly that race (the
    correction lands AFTER the service's pre-check has already read the
    still-frozen 250 g detail, but BEFORE its own write reaches the store),
    proving it's the store's atomic WHERE-clause bound — not the pre-check,
    which by then has already been fooled — that turns this into a 409
    instead of a silently-wrong write."""
    await store.create_run(
        run_id="run-race",
        profile=_profile(),  # bean_weight_grams 250
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-race", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )

    real_read_run = store.read_run
    raced = False

    async def racing_read_run(run_id: str) -> RoastDetail | None:
        nonlocal raced
        detail = await real_read_run(run_id)
        if run_id == "run-race" and not raced:
            raced = True
            # The race: a concurrent correction lands here, AFTER the
            # pre-check above has already read (and validated 210 g against)
            # the frozen 250 g default it just returned.
            await store.set_corrected_charge("run-race", corrected_charge_grams=200.0)
        return detail

    with mock.patch.object(store, "read_run", side_effect=racing_read_run):
        service = RoastService(store)
        # 210 g passes the pre-check (still sees the frozen 250 g), but by
        # the time set_roasted_weight's own UPDATE runs, the effective charge
        # is 200 g — only the atomic WHERE-clause bound catches this.
        with pytest.raises(RoastRunConflictError):
            await service.set_roasted_weight(
                "run-race", RoastedWeightRequest(roasted_weight_grams=210.0)
            )

    assert raced
    # The rejected write must not have landed.
    final = await store.read_run("run-race")
    assert final is not None
    assert final.roasted_weight_grams is None


# --- charge-weight correction (#520) ---


@pytest.mark.asyncio
async def test_set_charge_weight_completed_run(client: AsyncClient, store: RoastStore) -> None:
    """#520: roast 13's exact worked example — charged 255 g against a 250 g
    form default, roasted 223 g. Truth is 12.55%, not the 10.8% the frozen
    charge weight alone would compute."""
    await store.create_run(
        run_id="run-cc",
        profile=_profile(),  # bean_weight_grams 250
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-cc", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.set_roasted_weight("run-cc", roasted_weight_grams=223.0)

    response = await client.post(
        "/api/roasts/run-cc/charge-weight", json={"corrected_charge_grams": 255.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["corrected_charge_grams"] == 255.0
    # The frozen profile is UNTOUCHED — still the 250 g the controller ran with.
    assert body["profile"]["bean_weight_grams"] == 250.0
    assert body["weight_loss_percent"] == 12.55  # (255 - 223) / 255 * 100


@pytest.mark.asyncio
async def test_set_charge_weight_without_a_roasted_weight_yet(
    client: AsyncClient, store: RoastStore
) -> None:
    """#520: correcting the charge weight before the roasted-out weight is
    entered is valid — nothing to bound against yet, so any positive value
    is accepted."""
    await store.create_run(
        run_id="run-cc-early",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-cc-early", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    response = await client.post(
        "/api/roasts/run-cc-early/charge-weight", json={"corrected_charge_grams": 255.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["corrected_charge_grams"] == 255.0
    assert body["weight_loss_percent"] is None  # still un-weighed


@pytest.mark.asyncio
async def test_set_charge_weight_races_a_concurrent_roasted_weight_still_409s(
    store: RoastStore,
) -> None:
    """#520 round-2 P3: mirror-direction race to
    test_set_roasted_weight_races_a_concurrent_charge_correction_still_409s
    — set_charge_weight's own pre-check reads `detail` (no roasted weight
    yet) then writes moments later; a concurrent set_roasted_weight landing
    in between invalidates the "nothing to bound against" the pre-check saw.
    Proves the store's atomic WHERE-clause bound (not the pre-check, already
    fooled by then) is what turns this into a 409."""
    await store.create_run(
        run_id="run-cc-race",
        profile=_profile(),  # bean_weight_grams 250
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-cc-race", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )

    real_read_run = store.read_run
    raced = False

    async def racing_read_run(run_id: str) -> RoastDetail | None:
        nonlocal raced
        detail = await real_read_run(run_id)
        if run_id == "run-cc-race" and not raced:
            raced = True
            # The race: a concurrent weighing lands here, AFTER the
            # pre-check above has already read (and seen no roasted weight
            # to bound against) the still-unweighed run.
            await store.set_roasted_weight("run-cc-race", roasted_weight_grams=240.0)
        return detail

    with mock.patch.object(store, "read_run", side_effect=racing_read_run):
        service = RoastService(store)
        # 200 g is fine against the "nothing to bound against yet" the
        # pre-check saw, but is impossible against the 240 g roasted weight
        # the race actually left behind — only the atomic bound catches this.
        with pytest.raises(RoastRunConflictError):
            await service.set_charge_weight(
                "run-cc-race", ChargeWeightRequest(corrected_charge_grams=200.0)
            )

    assert raced
    final = await store.read_run("run-cc-race")
    assert final is not None
    assert final.corrected_charge_grams is None


@pytest.mark.asyncio
async def test_set_charge_weight_in_progress_conflicts(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-cc-ip",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    response = await client.post(
        "/api/roasts/run-cc-ip/charge-weight", json={"corrected_charge_grams": 255.0}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_set_charge_weight_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/roasts/nope/charge-weight", json={"corrected_charge_grams": 255.0}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_set_charge_weight_rejects_non_positive(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-cc-nonpositive",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-cc-nonpositive", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    response = await client.post(
        "/api/roasts/run-cc-nonpositive/charge-weight", json={"corrected_charge_grams": 0}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_set_charge_weight_rejects_below_roasted_weight(
    client: AsyncClient, store: RoastStore
) -> None:
    """#520: a corrected charge below the roasted-out weight is physically
    impossible (the beans cannot weigh more roasted than green) — 409,
    mirroring set_roasted_weight's own bound in the other direction."""
    await store.create_run(
        run_id="run-cc-under",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-cc-under", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    await store.set_roasted_weight("run-cc-under", roasted_weight_grams=221.0)
    response = await client.post(
        "/api/roasts/run-cc-under/charge-weight", json={"corrected_charge_grams": 200.0}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_set_charge_weight_records_an_audit_event(
    client: AsyncClient, store: RoastStore
) -> None:
    """#520: the correction records an operator_actions audit row with the
    before/after charge values — reuses the existing free-form action column,
    never a new RoastEventKind."""
    await store.create_run(
        run_id="run-cc-audit",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-cc-audit", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    await client.post(
        "/api/roasts/run-cc-audit/charge-weight", json={"corrected_charge_grams": 255.0}
    )
    [action_row] = await _operator_action_rows(store, "run-cc-audit")
    action, result, payload_json = action_row
    assert action == "charge_weight_correction"
    assert result == "accepted"
    assert payload_json is not None
    payload = json.loads(payload_json)
    assert payload["previous_charge_grams"] == 250.0  # the frozen profile default
    assert payload["corrected_charge_grams"] == 255.0


@pytest.mark.asyncio
async def test_set_charge_weight_twice_audits_the_prior_correction(
    client: AsyncClient, store: RoastStore
) -> None:
    """#520: a second correction's audit payload records the PREVIOUS
    correction as its "before" value, not the original frozen default —
    each correction's audit trail is against what was actually true
    immediately before it."""
    await store.create_run(
        run_id="run-cc-twice",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-cc-twice", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    await client.post(
        "/api/roasts/run-cc-twice/charge-weight", json={"corrected_charge_grams": 255.0}
    )
    await client.post(
        "/api/roasts/run-cc-twice/charge-weight", json={"corrected_charge_grams": 252.0}
    )
    action_rows = await _operator_action_rows(store, "run-cc-twice")
    assert len(action_rows) == 2
    _, _, second_payload_json = action_rows[1]
    assert second_payload_json is not None
    second_payload = json.loads(second_payload_json)
    assert second_payload["previous_charge_grams"] == 255.0
    assert second_payload["corrected_charge_grams"] == 252.0


# --- discard / restore a roast (#582) ---


@pytest.mark.asyncio
async def test_discard_completed_run(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-d",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-d", outcome="completed", agent_phase=RoastPhase.COMPLETE)

    response = await client.post("/api/roasts/run-d/discard")
    assert response.status_code == 200
    body = response.json()
    assert body["excluded"] is True

    # A discarded run drops out of the history list...
    history = await client.get("/api/roasts")
    assert "run-d" not in {run["id"] for run in history.json()["runs"]}
    # ...but a direct link to its detail still works, flagged.
    detail = await client.get("/api/roasts/run-d")
    assert detail.status_code == 200
    assert detail.json()["excluded"] is True


@pytest.mark.asyncio
async def test_restore_reverses_a_discard(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-r",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(run_id="run-r", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    discard = await client.post("/api/roasts/run-r/discard")
    assert discard.json()["excluded"] is True

    response = await client.post("/api/roasts/run-r/restore")
    assert response.status_code == 200
    body = response.json()
    assert body["excluded"] is False

    history = await client.get("/api/roasts")
    assert "run-r" in {run["id"] for run in history.json()["runs"]}


@pytest.mark.asyncio
async def test_discard_in_progress_run_conflicts(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-dip",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    response = await client.post("/api/roasts/run-dip/discard")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_restore_in_progress_run_conflicts(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-rip",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    response = await client.post("/api/roasts/run-rip/restore")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_discard_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post("/api/roasts/nope/discard")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_restore_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post("/api/roasts/nope/restore")
    assert response.status_code == 404


# --- clear stale session (#525) ---


async def _insert_recent_telemetry(store: RoastStore, run_id: str, *, seconds_ago: float) -> None:
    """Insert one raw telemetry row at an explicit age (#525 guard (c) tests) —
    mirrors ``tests/test_store.py``'s ``_insert_telemetry_at`` helper."""
    recorded_at = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    await store.connection.execute(
        "INSERT INTO telemetry_snapshots (run_id, tick, recorded_at_utc, agent_phase)"
        " VALUES (?, 1, ?, 'roasting_pre_first_crack')",
        (run_id, recorded_at),
    )
    await store.connection.commit()


async def _make_stale_run(store: RoastStore, run_id: str, phase: RoastPhase) -> None:
    """A run row that is a genuine STALE ORPHAN (#525 P1 fold, clause 2b):
    ``_make_run``'s ``started_at_utc`` default is "now" — exactly what a real
    orphan is NOT (one started minutes ago and abandoned). Tests whose intent
    is "a genuinely stale run the clear SHOULD succeed against" use this
    helper instead of ``_make_run`` so clause 2b's ``started_at_utc <=
    threshold`` bound does not spuriously refuse them."""
    long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=phase,
        started_at_utc=long_ago,
    )


@pytest.mark.asyncio
async def test_clear_stale_session_finalizes_an_orphaned_run(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525: a genuinely stranded run (not this process's active run, no
    recent telemetry, started well outside the recency window) is finalised
    ``outcome="aborted"``."""
    await _make_stale_run(store, "run-stale", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    response = await client.post(
        "/api/roasts/run-stale/clear-stale-session", json={"reason": "orphaned after a crash"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-stale"
    assert body["outcome"] == "aborted"
    assert body["completed_at_utc"] is not None

    row = await _fetch_run_row(store, "run-stale")
    assert row["outcome"] == "aborted"
    assert row["agent_phase"] == "operator_recovery_required"  # untouched, per #525 design
    assert await store.active_run() is None


@pytest.mark.asyncio
async def test_clear_stale_session_blocks_this_processs_own_active_run(
    client: AsyncClient, service: RoastService, store: RoastStore
) -> None:
    """#525 guard (a) — condition 3: this process's own tracked active run
    (covers BOTH a live run and ``operator_recovery_required``, since both
    keep ``active_run_id`` set) can NEVER be cleared through this action —
    the e-stop/recovery path must stay reachable. 409, not a silent no-op."""
    await _make_run(store, "run-own-active", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    service.active_run_id = "run-own-active"

    response = await client.post(
        "/api/roasts/run-own-active/clear-stale-session",
        json={"reason": "trying to clear my own run"},
    )
    assert response.status_code == 409
    assert "active" in response.json()["detail"].lower()

    # Untouched: still active, no outcome stamped.
    row = await _fetch_run_row(store, "run-own-active")
    assert row["outcome"] is None
    assert row["completed_at_utc"] is None
    # The REJECTED attempt is still audited (#525 requirement 4).
    [action_row] = await _operator_action_rows(store, "run-own-active")
    action, result, payload_json = action_row
    assert action == "clear_stale_session"
    assert result == "rejected"
    assert payload_json is not None
    assert json.loads(payload_json)["reason"] == "trying to clear my own run"


@pytest.mark.asyncio
async def test_clear_stale_session_blocks_a_run_with_recent_telemetry(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 guard (c) — condition 3's cross-process simulation: a run with
    FRESH telemetry is refused even though it is NOT this process's tracked
    active run (``service.active_run_id`` stays ``None`` — simulating an
    impostor/second-process view where guard (a) alone would pass). This is
    the safety-reviewer's PASS-WITH-CONDITIONS kill chain, closed by shared
    DB state rather than any one process's self-report."""
    await _make_run(store, "run-driven-elsewhere", RoastPhase.DEVELOPMENT)
    await _insert_recent_telemetry(store, "run-driven-elsewhere", seconds_ago=3.0)

    response = await client.post(
        "/api/roasts/run-driven-elsewhere/clear-stale-session",
        json={"reason": "thought this was orphaned"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "actively driven" in detail or "actively driving" in detail
    assert "already finalized" not in detail  # distinct message, per condition 3

    row = await _fetch_run_row(store, "run-driven-elsewhere")
    assert row["outcome"] is None  # untouched — still live
    assert row["completed_at_utc"] is None


@pytest.mark.asyncio
async def test_clear_stale_session_blocks_a_just_started_run_with_no_telemetry_yet(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 guard (c) clause 2b — the P1 fold (PR #548 round-1 Codex), at the
    route boundary. A run created moments ago has heat/fan actively commanded
    (``RoastRunner.start()`` drives ``controller.start_run()`` before
    ``run()``'s scheduler ever ticks) but ZERO telemetry rows — clause 2a's
    ``NOT EXISTS`` check alone would pass and let this "clear" finalise a row
    whose hardware is being driven right now. ``_make_run`` (unlike
    ``_make_stale_run``) stamps ``started_at_utc`` at "now", exactly
    reproducing the hazard: this is the P1 repro at the API layer, mirroring
    ``test_finalize_orphaned_run_blocked_by_a_just_started_run_with_no_telemetry_yet``
    in ``test_store.py``."""
    await _make_run(store, "run-just-started", RoastPhase.PREHEATING)
    cursor = await store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_snapshots WHERE run_id = 'run-just-started'"
    )
    count_row = await cursor.fetchone()
    assert count_row is not None
    assert count_row[0] == 0  # sanity: genuinely no telemetry yet

    response = await client.post(
        "/api/roasts/run-just-started/clear-stale-session",
        json={"reason": "thought this was an old orphan"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "actively driven" in detail or "actively driving" in detail

    row = await _fetch_run_row(store, "run-just-started")
    assert row["outcome"] is None  # untouched — still active
    assert row["completed_at_utc"] is None


@pytest.mark.asyncio
async def test_clear_stale_session_shadowed_older_run_is_still_clearable(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 (safety-reviewer non-blocking addition): an OLDER unfinalised run
    shadowed by a newer one is still clearable via the recency path — its OWN
    telemetry AND start time are stale even though a different, newer run
    exists."""
    await _make_stale_run(store, "run-older", RoastPhase.FAULTED)
    await _insert_recent_telemetry(store, "run-older", seconds_ago=999.0)  # long stale
    await _make_run(store, "run-newer", RoastPhase.DEVELOPMENT)
    await _insert_recent_telemetry(store, "run-newer", seconds_ago=3.0)  # actively driven

    response = await client.post(
        "/api/roasts/run-older/clear-stale-session", json={"reason": "shadowed orphan"}
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "aborted"

    # The newer, actively-driven run is untouched by clearing the older one.
    newer_row = await _fetch_run_row(store, "run-newer")
    assert newer_row["outcome"] is None
    assert newer_row["completed_at_utc"] is None


@pytest.mark.asyncio
async def test_clear_stale_session_unknown_run_404(client: AsyncClient, store: RoastStore) -> None:
    """#525 PR #548 round-2 P3: an unknown-id clear attempt is still AUDITED
    (requirement 4: every rejection is recorded) — recorded with
    ``run_id=None`` (the FK ``operator_actions.run_id REFERENCES
    roast_runs(id)`` under ``foreign_keys=ON`` would raise if the bogus id
    were passed directly) and the attempted id captured in the payload."""
    response = await client.post(
        "/api/roasts/nope/clear-stale-session", json={"reason": "does not exist"}
    )
    assert response.status_code == 404

    async with store.connection.execute(
        "SELECT action, result, payload_json FROM operator_actions WHERE run_id IS NULL"
        " ORDER BY id DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    action, result, payload_json = row
    assert action == "clear_stale_session"
    assert result == "rejected"
    assert payload_json is not None
    assert json.loads(payload_json)["requested_run_id"] == "nope"


@pytest.mark.asyncio
async def test_clear_stale_session_rejects_empty_reason(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525: no silent no-reason clears — a required, non-empty ``reason``."""
    await _make_run(store, "run-empty-reason", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    response = await client.post(
        "/api/roasts/run-empty-reason/clear-stale-session", json={"reason": ""}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_clear_stale_session_rejects_whitespace_only_reason(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 P3 (PR #548 round-1 Codex): ``min_length=1`` alone lets a
    whitespace-only reason (``"   "``) through, since it isn't the EMPTY
    string. A direct API caller bypassing the FE's own ``.trim()`` must face
    the same requirement server-side — the endpoint IS the audit contract."""
    await _make_run(store, "run-whitespace-reason", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    response = await client.post(
        "/api/roasts/run-whitespace-reason/clear-stale-session", json={"reason": "   "}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_clear_stale_session_stores_the_reason_trimmed(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 P3: a padded reason (leading/trailing whitespace, non-blank
    content) is accepted but STORED TRIMMED — the audit row never carries a
    padded value even if a caller sends one directly (not via the FE, which
    already trims client-side)."""
    await _make_stale_run(store, "run-padded-reason", RoastPhase.FAULTED)
    response = await client.post(
        "/api/roasts/run-padded-reason/clear-stale-session",
        json={"reason": "  confirmed via direct API call  "},
    )
    assert response.status_code == 200
    [action_row] = await _operator_action_rows(store, "run-padded-reason")
    _, _, payload_json = action_row
    assert payload_json is not None
    assert json.loads(payload_json)["reason"] == "confirmed via direct API call"


@pytest.mark.asyncio
async def test_clear_stale_session_already_finalized_conflicts(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 guard (b): a race with a concurrent finalize (or simply an
    already-completed run) is a clean 409, never a silent no-op, and its
    message is distinct from the guard (a)/(c) messages."""
    await store.create_run(
        run_id="run-already-done",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-already-done", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    response = await client.post(
        "/api/roasts/run-already-done/clear-stale-session",
        json={"reason": "already finished"},
    )
    assert response.status_code == 409
    assert "already finalized" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_clear_stale_session_records_accepted_audit_event(
    client: AsyncClient, store: RoastStore
) -> None:
    """#525 requirement 4: a successful clear records the reason and the
    agent_phase the run was in at the moment of clearing."""
    await _make_stale_run(store, "run-audit-accept", RoastPhase.FAULTED)
    await client.post(
        "/api/roasts/run-audit-accept/clear-stale-session",
        json={"reason": "confirmed abandoned via SQL inspection"},
    )
    [action_row] = await _operator_action_rows(store, "run-audit-accept")
    action, result, payload_json = action_row
    assert action == "clear_stale_session"
    assert result == "accepted"
    assert payload_json is not None
    payload = json.loads(payload_json)
    assert payload["reason"] == "confirmed abandoned via SQL inspection"
    assert payload["agent_phase_at_clear"] == "faulted"


@pytest.mark.asyncio
async def test_clear_stale_session_issues_zero_mcp_calls(store: RoastStore) -> None:
    """#525 invariant: this action is a PURE store write. Regardless of
    outcome (accepted or every rejection path), it must never call ANY
    method on the MCP child / roaster control surface — there is no
    controller loop or safety box for a stale row to command through."""
    await _make_stale_run(store, "run-no-mcp", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    fake_mcp = mock.Mock()
    fake_roaster = mock.Mock()
    service = RoastService(store, mcp=fake_mcp, roaster=fake_roaster)

    result = await service.clear_stale_session(
        "run-no-mcp", ClearStaleSessionRequest(reason="zero-mcp invariant")
    )

    assert result.outcome == "aborted"
    assert fake_mcp.mock_calls == []
    assert fake_roaster.mock_calls == []

    # Also true on a REJECTED path (guard (a)).
    await _make_run(store, "run-no-mcp-2", RoastPhase.DEVELOPMENT)
    service.active_run_id = "run-no-mcp-2"
    with pytest.raises(RoastRunConflictError):
        await service.clear_stale_session(
            "run-no-mcp-2", ClearStaleSessionRequest(reason="zero-mcp invariant rejected")
        )
    assert fake_mcp.mock_calls == []
    assert fake_roaster.mock_calls == []


async def _fetch_run_row(store: RoastStore, run_id: str) -> dict[str, object]:
    async with store.connection.execute(
        "SELECT outcome, agent_phase, completed_at_utc FROM roast_runs WHERE id = ?", (run_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return {"outcome": row[0], "agent_phase": row[1], "completed_at_utc": row[2]}


# --- tastings (#522, D91) ---


@pytest.mark.asyncio
async def test_add_tasting_stars_and_notes_only(client: AsyncClient, store: RoastStore) -> None:
    """#522: entry friction stays near zero — stars alone (no notes, no other
    field) is a valid POST body."""
    await store.create_run(
        run_id="run-t", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-t", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post("/api/roasts/run-t/tastings", json={"stars": 4})
    assert response.status_code == 201
    body = response.json()
    assert body["run_id"] == "run-t"
    assert len(body["tastings"]) == 1
    tasting = body["tastings"][0]
    assert tasting["stars"] == 4
    assert tasting["notes"] is None
    assert tasting["tasted_at_utc"] is None
    assert tasting["brew_method"] is None
    assert tasting["attributes"] == []
    assert tasting["defects"] == []


@pytest.mark.asyncio
async def test_add_tasting_full_payload(client: AsyncClient, store: RoastStore) -> None:
    await store.create_run(
        run_id="run-full", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(
        run_id="run-full", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    detail = (await client.get("/api/roasts/run-full")).json()
    completed_at = datetime.fromisoformat(detail["completed_at_utc"])
    # After completion (the lower bound) but well within the future-clock-skew
    # tolerance (the upper bound, #522 Codex round 3) — a value that satisfies
    # BOTH bounds regardless of when the suite runs.
    tasted_at = (completed_at + timedelta(seconds=5)).isoformat()
    response = await client.post(
        "/api/roasts/run-full/tastings",
        json={
            "stars": 5,
            "notes": "sweet, clean",
            "tasted_at_utc": tasted_at,
            "brew_method": "aeropress",
            "grind_note": "fine",
            "attributes": ["sweetness", "body"],
            "defects": [],
        },
    )
    assert response.status_code == 201
    tasting = response.json()["tastings"][0]
    assert tasting["tasted_at_utc"] == tasted_at
    assert tasting["brew_method"] == "aeropress"
    assert tasting["grind_note"] == "fine"
    assert tasting["attributes"] == ["sweetness", "body"]


@pytest.mark.asyncio
async def test_add_tasting_revisit_appends(client: AsyncClient, store: RoastStore) -> None:
    """#522, D91: a second POST is an ADDITIONAL tasting, not an overwrite —
    the roast-13 "flat -> grassy" refinement shape."""
    await store.create_run(
        run_id="run-revisit",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-revisit", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    await client.post("/api/roasts/run-revisit/tastings", json={"stars": 2, "defects": ["flat"]})
    response = await client.post(
        "/api/roasts/run-revisit/tastings", json={"stars": 4, "defects": ["grassy"]}
    )
    assert response.status_code == 201
    tastings = response.json()["tastings"]
    assert len(tastings) == 2
    assert tastings[0]["defects"] == ["flat"]
    assert tastings[1]["defects"] == ["grassy"]


@pytest.mark.asyncio
async def test_add_tasting_in_progress_run_conflicts(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-tip", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.DEVELOPMENT
    )
    response = await client.post("/api/roasts/run-tip/tastings", json={"stars": 3})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_add_tasting_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post("/api/roasts/nope/tastings", json={"stars": 3})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_add_tasting_rejects_out_of_range_stars(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-tb", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tb", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post("/api/roasts/run-tb/tastings", json={"stars": 0})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_tasting_rejects_unknown_brew_method(
    client: AsyncClient, store: RoastStore
) -> None:
    """#522: brew method is a closed controlled vocabulary — an arbitrary
    string is rejected, not silently accepted as free text."""
    await store.create_run(
        run_id="run-tbm", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tbm", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-tbm/tastings", json={"stars": 3, "brew_method": "instant"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_tasting_rejects_malformed_tasted_at(
    client: AsyncClient, store: RoastStore
) -> None:
    """#522 Codex P2: tasted_at_utc is the exact degassing-offset corpus
    signal #522 exists to capture — an unparseable value must 422, not
    persist verbatim and poison the label silently."""
    await store.create_run(
        run_id="run-tba", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tba", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-tba/tastings",
        # A "T" separator IS present (distinct from the bare-date-rejection
        # test below), so this exercises fromisoformat's own parse failure.
        json={"stars": 3, "tasted_at_utc": "2026-07-12Tnot-a-time"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_tasting_normalizes_naive_and_offset_tasted_at(
    client: AsyncClient, store: RoastStore
) -> None:
    """#522 Codex P2: a naive (no-offset) timestamp is assumed UTC; a
    non-UTC-offset timestamp is converted to UTC — both round-trip through
    the store as a UTC-offset ISO-8601 string, never the raw operator input."""
    await store.create_run(
        run_id="run-tbn", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tbn", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    detail = (await client.get("/api/roasts/run-tbn")).json()
    completed_at = datetime.fromisoformat(detail["completed_at_utc"])
    # A UTC instant after completion (the lower bound) but well within the
    # future-clock-skew tolerance (the upper bound, #522 Codex round 3) —
    # satisfies both bounds regardless of when the suite runs, unlike a fixed
    # literal (which would eventually collide with one bound or the other).
    base = completed_at + timedelta(seconds=5)
    expected = base.isoformat()
    naive_str = base.replace(tzinfo=None).isoformat()

    naive = await client.post(
        "/api/roasts/run-tbn/tastings",
        json={"stars": 3, "tasted_at_utc": naive_str},
    )
    assert naive.status_code == 201
    assert naive.json()["tastings"][0]["tasted_at_utc"] == expected

    # The same UTC instant, expressed with a +02:00 offset (wall-clock time
    # shifted +2h so the UTC instant is unchanged) — proper tz arithmetic via
    # astimezone, not string surgery on the ISO text.
    offset_str = base.astimezone(timezone(timedelta(hours=2))).isoformat()
    offset = await client.post(
        "/api/roasts/run-tbn/tastings",
        json={"stars": 4, "tasted_at_utc": offset_str},
    )
    assert offset.status_code == 201
    assert offset.json()["tastings"][1]["tasted_at_utc"] == expected


@pytest.mark.asyncio
async def test_add_tasting_rejects_bare_date(client: AsyncClient, store: RoastStore) -> None:
    """#522 Codex P2: a bare date ("2026-07-13") is parseable by
    datetime.fromisoformat as midnight — but silently inventing a midnight
    instant would shift the degassing offset by up to 24h. Must 422, not
    accept it."""
    await store.create_run(
        run_id="run-tbd", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tbd", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-tbd/tastings",
        json={"stars": 3, "tasted_at_utc": "2026-07-13"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_tasting_dedupes_duplicate_tags(client: AsyncClient, store: RoastStore) -> None:
    """#522 Codex P2: a duplicated tag (e.g. an accidental double-tap) is
    deduplicated on save, preserving first-occurrence order — never rejected,
    never double-counted in the corpus."""
    await store.create_run(
        run_id="run-tdd", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tdd", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    response = await client.post(
        "/api/roasts/run-tdd/tastings",
        json={
            "stars": 4,
            "attributes": ["sweetness", "acidity", "sweetness"],
            "defects": ["bitter", "bitter"],
        },
    )
    assert response.status_code == 201
    tasting = response.json()["tastings"][0]
    assert tasting["attributes"] == ["sweetness", "acidity"]
    assert tasting["defects"] == ["bitter"]


@pytest.mark.asyncio
async def test_add_tasting_rejects_tasted_at_before_completion(
    client: AsyncClient, store: RoastStore
) -> None:
    """#522 Codex P2: a tasted_at_utc earlier than the run's completed_at_utc
    is physically impossible (the beans cannot be tasted before the roast
    that produced them finished) — a negative degassing offset is a nonsense
    corpus label, so reject it as a 409, mirroring the roasted-exceeds-charge
    physical-impossibility class."""
    await store.create_run(
        run_id="run-tpc", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tpc", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    detail = (await client.get("/api/roasts/run-tpc")).json()
    completed_at = datetime.fromisoformat(detail["completed_at_utc"])
    # A full 2 minutes earlier — guaranteed to land in an EARLIER minute
    # regardless of completed_at's own seconds/microseconds component (#522
    # round 4: the comparison is minute-truncated, so a delta smaller than a
    # minute could land in the SAME minute and be wrongly accepted).
    before = (completed_at - timedelta(minutes=2)).isoformat()

    response = await client.post(
        "/api/roasts/run-tpc/tastings",
        json={"stars": 3, "tasted_at_utc": before},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_add_tasting_accepts_tasted_at_exactly_at_completion(
    client: AsyncClient, store: RoastStore
) -> None:
    """#522 Codex P2: exactly-at-completion is accepted (>=, not strictly >)
    — a tasting timestamped the instant cooling ended is unusual but not
    physically impossible."""
    await store.create_run(
        run_id="run-tec", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-tec", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    detail = (await client.get("/api/roasts/run-tec")).json()

    response = await client.post(
        "/api/roasts/run-tec/tastings",
        json={"stars": 3, "tasted_at_utc": detail["completed_at_utc"]},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_add_tasting_accepts_same_minute_as_completion_despite_seconds(
    client: AsyncClient, store: RoastStore
) -> None:
    """#522 round 4/5: the FE's datetime-local picker cannot express seconds,
    so an honest "tasted at the completion minute" entry must NOT 409 just
    because completed_at_utc itself has a non-zero seconds component. The
    exact collision case from the thread: completion at some non-zero
    second, the operator picks that same minute (:00 seconds — all a
    datetime-local input can express). The stored value is then CLAMPED to
    completed_at_utc (round 5), not the raw (sub-minute-earlier) input."""
    await store.create_run(
        run_id="run-minute", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(
        run_id="run-minute", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    detail = (await client.get("/api/roasts/run-minute")).json()
    completed_at = datetime.fromisoformat(detail["completed_at_utc"])
    # completed_at_utc is stamped from real "now" (datetime.now(UTC)), which
    # essentially never lands on exactly :00 seconds — this reproduces the
    # collision without needing to bypass the completed-run immutability
    # trigger to pin an exact seconds value.
    same_minute_no_seconds = completed_at.replace(second=0, microsecond=0).isoformat()

    response = await client.post(
        "/api/roasts/run-minute/tastings",
        json={"stars": 4, "tasted_at_utc": same_minute_no_seconds},
    )
    assert response.status_code == 201
    # #522 round 5: a raw-earlier same-minute value is CLAMPED to
    # completed_at_utc on storage, not persisted as the truncated input —
    # storing the raw sub-minute-early value would compute a small NEGATIVE
    # degassing_offset_hours in the corpus export.
    assert response.json()["tastings"][0]["tasted_at_utc"] == detail["completed_at_utc"]


@pytest.mark.parametrize(
    ("tasted_at", "completed_at", "expected"),
    [
        # The exact collision case: completion at :45s, tasted at the same
        # minute (:00s) — NOT before, despite the raw string being lexically
        # smaller.
        ("2026-07-12T18:05:00+00:00", "2026-07-12T18:05:45+00:00", False),
        # A genuinely earlier minute — still caught.
        ("2026-07-12T18:04:59+00:00", "2026-07-12T18:05:00+00:00", True),
        # Same instant.
        ("2026-07-12T18:05:45+00:00", "2026-07-12T18:05:45+00:00", False),
        # A later minute.
        ("2026-07-12T18:06:00+00:00", "2026-07-12T18:05:45+00:00", False),
    ],
)
def test_before_the_minute(tasted_at: str, completed_at: str, expected: bool) -> None:
    """#522 round 4: unit-level pin of the minute-truncated comparison."""
    assert _before_the_minute(tasted_at, completed_at) is expected


@pytest.mark.asyncio
async def test_list_tastings_empty_for_untasted_completed_run(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="run-empty", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(
        run_id="run-empty", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    response = await client.get("/api/roasts/run-empty/tastings")
    assert response.status_code == 200
    assert response.json() == {"run_id": "run-empty", "tastings": []}


@pytest.mark.asyncio
async def test_list_tastings_unknown_run_404(client: AsyncClient) -> None:
    response = await client.get("/api/roasts/nope/tastings")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_tastings_reflects_active_run(client: AsyncClient, store: RoastStore) -> None:
    """GET is not completed-only — reading the (empty) tasting list for an
    in-progress run is harmless and useful for the detail page to render
    before the roast finishes."""
    await store.create_run(
        run_id="run-active",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    response = await client.get("/api/roasts/run-active/tastings")
    assert response.status_code == 200
    assert response.json() == {"run_id": "run-active", "tastings": []}


# --- operator action queue (E7-S2) ---


async def _make_run(store: RoastStore, run_id: str, phase: RoastPhase) -> None:
    await store.create_run(run_id=run_id, profile=_profile(), config=AppConfig(), agent_phase=phase)


async def _operator_action_rows(
    store: RoastStore, run_id: str
) -> list[tuple[str, str, str | None]]:
    async with store.connection.execute(
        "SELECT action, result, payload_json FROM operator_actions WHERE run_id = ?"
        " ORDER BY id ASC",
        (run_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [(str(r[0]), str(r[1]), None if r[2] is None else str(r[2])) for r in rows]


@pytest.mark.asyncio
async def test_operator_action_accepted_and_queued(
    client: AsyncClient, service: RoastService, store: RoastStore
) -> None:
    await _make_run(store, "run-op", RoastPhase.DEVELOPMENT)
    response = await client.post(
        "/api/roasts/run-op/operator-actions", json={"action": "drop_beans"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "drop_beans"
    assert body["result"] == "accepted"
    assert body["queued"] is True

    assert service.operator_queue.qsize() == 1
    queued = service.operator_queue.get_nowait()
    assert queued.run_id == "run-op"
    assert queued.action is OperatorAction.DROP_BEANS

    rows = await _operator_action_rows(store, "run-op")
    assert rows == [("drop_beans", "accepted", None)]


@pytest.mark.asyncio
async def test_operator_action_rejected_in_wrong_phase(
    client: AsyncClient, service: RoastService, store: RoastStore
) -> None:
    # drop_beans is invalid during preheating (no beans in the drum yet).
    await _make_run(store, "run-pre", RoastPhase.PREHEATING)
    response = await client.post(
        "/api/roasts/run-pre/operator-actions", json={"action": "drop_beans"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "rejected"
    assert body["queued"] is False
    assert "not valid in phase preheating" in body["reason"]

    assert service.operator_queue.qsize() == 0
    rows = await _operator_action_rows(store, "run-pre")
    assert rows == [("drop_beans", "rejected", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", list(RoastPhase))
async def test_emergency_stop_accepted_in_every_phase(
    client: AsyncClient, service: RoastService, store: RoastStore, phase: RoastPhase
) -> None:
    await _make_run(store, f"run-es-{phase.value}", phase)
    response = await client.post(
        f"/api/roasts/run-es-{phase.value}/operator-actions",
        json={"action": "emergency_stop"},
    )
    assert response.json()["result"] == "accepted"
    assert service.operator_queue.qsize() == 1
    queued = service.operator_queue.get_nowait()
    assert queued.action is OperatorAction.EMERGENCY_STOP
    assert queued.run_id == f"run-es-{phase.value}"


@pytest.mark.asyncio
async def test_mark_beans_added_accepted_only_in_preheating(
    client: AsyncClient, store: RoastStore
) -> None:
    # The manual-T0 fallback is preheating-only — not the recovery phase.
    await _make_run(store, "run-mba-pre", RoastPhase.PREHEATING)
    accepted = await client.post(
        "/api/roasts/run-mba-pre/operator-actions", json={"action": "mark_beans_added"}
    )
    assert accepted.json()["result"] == "accepted"

    await _make_run(store, "run-mba-rec", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    rejected = await client.post(
        "/api/roasts/run-mba-rec/operator-actions", json={"action": "mark_beans_added"}
    )
    assert rejected.json()["result"] == "rejected"


@pytest.mark.asyncio
async def test_control_action_accepted_without_matrix_check(
    client: AsyncClient, service: RoastService, store: RoastStore
) -> None:
    # pause_advisory issues no MCP write, so it is accepted for the controller
    # to interpret regardless of phase.
    await _make_run(store, "run-pa", RoastPhase.COOLING)
    response = await client.post(
        "/api/roasts/run-pa/operator-actions", json={"action": "pause_advisory"}
    )
    body = response.json()
    assert body["result"] == "accepted"
    assert body["queued"] is True
    assert service.operator_queue.get_nowait().action is OperatorAction.PAUSE_ADVISORY


@pytest.mark.asyncio
async def test_operator_action_records_payload(
    client: AsyncClient, service: RoastService, store: RoastStore
) -> None:
    await _make_run(store, "run-pl", RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    response = await client.post(
        "/api/roasts/run-pl/operator-actions",
        json={"action": "acknowledge_recovery", "payload": {"resume_to": "cooling"}},
    )
    assert response.json()["result"] == "accepted"
    queued = service.operator_queue.get_nowait()
    assert queued.payload == {"resume_to": "cooling"}
    rows = await _operator_action_rows(store, "run-pl")
    assert rows[0][0] == "acknowledge_recovery"
    assert rows[0][2] is not None and "resume_to" in rows[0][2]


@pytest.mark.asyncio
async def test_operator_action_unknown_run_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/roasts/nope/operator-actions", json={"action": "emergency_stop"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_operator_action_unknown_action_422(client: AsyncClient, store: RoastStore) -> None:
    await _make_run(store, "run-bad", RoastPhase.DEVELOPMENT)
    response = await client.post(
        "/api/roasts/run-bad/operator-actions", json={"action": "frobnicate"}
    )
    assert response.status_code == 422


# --- SSE event stream (E7-S3) ---
#
# The live endpoint is driven directly via stream_events() + the
# StreamingResponse body iterator: httpx's ASGITransport buffers the whole
# response body, which never terminates for an SSE stream, so it cannot drive
# a keepalive stream. Driving the generator directly is deterministic and
# exercises the same code path (subscribe, frame, heartbeat, disconnect
# cleanup) without a real socket.


def _parse_frame(text: str) -> dict[str, object]:
    """Parse one rendered SSE frame into {event, data, id}, skipping comments."""
    event_type: str | None = None
    data: str | None = None
    event_id: int | None = None
    for line in text.split("\n"):
        if line == "" or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip()
        if field == "event":
            event_type = value
        elif field == "data":
            data = value
        elif field == "id":
            event_id = int(value)
    return {
        "event": event_type,
        "data": None if data is None else json.loads(data),
        "id": event_id,
    }


class _FakeRequest:
    """Stands in for a Starlette Request — the SSE endpoint calls
    ``is_disconnected()`` plus ``headers.get(...)``/``query_params.get(...)`` for
    the Last-Event-ID resume (#339). ``headers`` is keyed lower-case to mirror
    Starlette's case-insensitive header access."""

    def __init__(
        self,
        *,
        disconnected: bool = False,
        last_event_id_header: str | None = None,
        last_event_id_query: str | None = None,
    ) -> None:
        self._disconnected = disconnected
        self.headers: dict[str, str] = (
            {"last-event-id": last_event_id_header} if last_event_id_header is not None else {}
        )
        self.query_params: dict[str, str] = (
            {"last_event_id": last_event_id_query} if last_event_id_query is not None else {}
        )

    async def is_disconnected(self) -> bool:
        return self._disconnected


async def _collect_frames(response: StreamingResponse, count: int) -> list[str]:
    """Pull up to ``count`` frames from a StreamingResponse, then close it
    (running the generator's finally → unsubscribe). Stops early if the stream
    ends first."""
    frames: list[str] = []
    iterator = cast(AsyncGenerator[bytes, None], response.body_iterator)
    try:
        async for chunk in iterator:
            frames.append(chunk.decode())
            if len(frames) >= count:
                break
    finally:
        await iterator.aclose()
    return frames


def test_sse_event_types_are_event_kinds_plus_transport() -> None:
    sse_values = {t.value for t in SseEventType}
    kind_values = {k.value for k in RoastEventKind}
    # Every controller event reaches the stream; the only extras are the two
    # transport-only events the API originates.
    assert kind_values <= sse_values
    assert sse_values - kind_values == {"telemetry", "heartbeat"}
    plan_events = {
        "run_started",
        "phase_changed",
        "telemetry",
        "charge_guidance",
        "t0_detected",
        "first_crack",
        "advisory",
        "command_executed",
        "command_failed",
        "safety_alert",
        "fault",
        "recovery_required",
        "recovery_acknowledged",
        "logs_exported",
        "run_completed",
        "heartbeat",
    }
    assert plan_events <= sse_values


def test_sse_event_render_wire_format() -> None:
    event = SseEvent(event=SseEventType.PHASE_CHANGED, data={"phase": "cooling"}, id=7)
    assert event.render() == 'id: 7\nevent: phase_changed\ndata: {"phase": "cooling"}\n\n'
    assert SseEvent(event=SseEventType.HEARTBEAT).render() == "event: heartbeat\ndata: {}\n\n"


def test_event_broadcaster_fans_out_with_sequence_ids() -> None:
    broadcaster = EventBroadcaster()
    first = broadcaster.subscribe()
    second = broadcaster.subscribe()
    assert broadcaster.subscriber_count == 2

    broadcaster.emit(RoastEventKind.RUN_STARTED, {"profile": "House"})
    event_a = first.get_nowait()
    event_b = second.get_nowait()
    assert event_a.event is SseEventType.RUN_STARTED
    assert event_a.data == {"profile": "House"}
    assert event_a.id == 1 and event_b.id == 1

    broadcaster.unsubscribe(first)
    assert broadcaster.subscriber_count == 1
    broadcaster.emit(RoastEventKind.RUN_COMPLETED, {})
    assert second.get_nowait().id == 2


def test_event_broadcaster_emits_typed_telemetry() -> None:
    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe()
    broadcaster.emit_telemetry(
        TelemetryEventData(
            agent_phase=RoastPhase.DEVELOPMENT,
            bean_temp_c=200.0,
            env_temp_c=210.0,
            heat_percent=60,
            fan_percent=40,
            post_fc_recovery_enabled=True,
            post_fc_heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            post_fc_ror_setpoint_c_per_min=6.4,
            post_fc_smoothed_ror_c_per_min=4.8,
            post_fc_effective_heat_ceiling_percent=75,
        )
    )
    event = queue.get_nowait()
    assert event.event is SseEventType.TELEMETRY
    assert event.data["agent_phase"] == "development"
    assert event.data["bean_temp_c"] == 200.0
    assert event.data["post_fc_recovery_enabled"] is True
    assert event.data["post_fc_heat_authority_state"] == "recovering"
    assert event.data["post_fc_ror_setpoint_c_per_min"] == pytest.approx(6.4)
    assert event.data["post_fc_smoothed_ror_c_per_min"] == pytest.approx(4.8)
    assert event.data["post_fc_effective_heat_ceiling_percent"] == 75


def test_event_broadcaster_telemetry_renders_non_finite_as_null() -> None:
    """The real telemetry emit path produces a strict-JSON subscriber frame."""

    def reject_constant(token: str) -> object:
        raise ValueError(f"non-finite JSON token: {token}")

    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe()
    broadcaster.emit_telemetry(
        TelemetryEventData(
            agent_phase=RoastPhase.DEVELOPMENT,
            bean_temp_c=200.0,
            env_temp_c=210.0,
            bean_ror_c_per_min=float("nan"),
        )
    )

    event = queue.get_nowait()
    data_line = next(line for line in event.render().splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "), parse_constant=reject_constant)
    assert payload["bean_ror_c_per_min"] is None


def test_event_broadcaster_wraps_non_dict_payload() -> None:
    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe()
    broadcaster.emit(RoastEventKind.RUN_COMPLETED, ["done"])
    assert queue.get_nowait().data == {"value": ["done"]}


def test_event_broadcaster_drops_for_a_slow_consumer() -> None:
    broadcaster = EventBroadcaster(max_queue=1)
    queue = broadcaster.subscribe()
    broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})
    broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "development"})  # full → dropped
    assert queue.qsize() == 1
    data = queue.get_nowait().data
    # phase_changed is enriched with enabled_actions (E10 option (a), D25).
    assert data["phase"] == "preheating"
    assert data["enabled_actions"] == [
        a.value for a in enabled_operator_actions(RoastPhase.PREHEATING)
    ]


def test_subscribe_replays_only_the_gap_after_last_event_id() -> None:
    """A reconnect with a Last-Event-ID pre-loads exactly the frames newer than
    that id, in order, before live frames resume (#339)."""
    broadcaster = EventBroadcaster()
    broadcaster.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1
    broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})  # id 2
    broadcaster.emit(RoastEventKind.FAULT, {"n": 3})  # id 3

    # Client last applied id 1 → it should receive 2 and 3 only, in order.
    queue = broadcaster.subscribe(last_event_id=1)
    first = queue.get_nowait()
    second = queue.get_nowait()
    assert (first.id, second.id) == (2, 3)
    assert queue.empty()
    # And it keeps receiving live frames after the replayed gap.
    broadcaster.emit(RoastEventKind.RUN_COMPLETED, {})  # id 4
    assert queue.get_nowait().id == 4


def test_subscribe_without_last_event_id_replays_nothing() -> None:
    """A fresh connection (no Last-Event-ID) gets an empty queue — no backfill."""
    broadcaster = EventBroadcaster()
    broadcaster.emit(RoastEventKind.RUN_STARTED, {"n": 1})
    broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})
    queue = broadcaster.subscribe()
    assert queue.empty()


def test_subscribe_with_caught_up_last_event_id_replays_nothing() -> None:
    """Last-Event-ID == the latest id: nothing newer to replay, empty queue."""
    broadcaster = EventBroadcaster()
    broadcaster.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1
    queue = broadcaster.subscribe(last_event_id=1)
    assert queue.empty()


def test_broadcaster_rejects_replay_buffer_larger_than_queue() -> None:
    """A replay buffer bigger than the queue is rejected at construction (#339):
    pre-seeding iterates oldest-first under QueueFull-suppress, so a larger buffer
    would silently drop the NEWEST gap frames — fail loudly instead."""
    with pytest.raises(ValueError, match="must not exceed max_queue"):
        EventBroadcaster(max_queue=10, replay_buffer=11)
    # Equal is allowed (the default config); strictly larger is the only failure.
    EventBroadcaster(max_queue=10, replay_buffer=10)


def test_subscribe_id_beyond_buffer_replays_only_what_remains() -> None:
    """When the client's id is older than the oldest buffered frame, the gap
    before the buffer is unrecoverable — only the buffered frames replay (#339).
    The client re-bases current state from the REST snapshot."""
    broadcaster = EventBroadcaster(replay_buffer=2)
    broadcaster.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1, evicted
    broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})  # id 2
    broadcaster.emit(RoastEventKind.FAULT, {"n": 3})  # id 3
    # Buffer now holds only ids {2, 3}; a client at id 0 cannot get id 1 back.
    queue = broadcaster.subscribe(last_event_id=0)
    replayed = [queue.get_nowait().id, queue.get_nowait().id]
    assert replayed == [2, 3]
    assert queue.empty()


def test_replay_buffer_evicts_oldest_past_capacity() -> None:
    """The ring buffer is bounded: past capacity the oldest frame is evicted, so
    a resume can never replay an id older than the retained window."""
    broadcaster = EventBroadcaster(replay_buffer=3)
    for _ in range(5):  # ids 1..5; buffer keeps only the last 3 (3, 4, 5)
        broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})
    queue = broadcaster.subscribe(last_event_id=0)
    assert [queue.get_nowait().id for _ in range(3)] == [3, 4, 5]
    assert queue.empty()


def test_last_event_id_property_unchanged_by_resume() -> None:
    """``last_event_id`` stays the monotonic published sequence regardless of any
    resume — the settle signal (E10-S1) is unaffected by the ring buffer (#339)."""
    broadcaster = EventBroadcaster()
    broadcaster.emit(RoastEventKind.RUN_STARTED, {"n": 1})
    broadcaster.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})
    assert broadcaster.last_event_id == 2
    broadcaster.subscribe(last_event_id=1)  # no publish → no sequence change
    assert broadcaster.last_event_id == 2


@pytest.mark.parametrize("raw", ["", "  ", "abc", "1.5", "12x", None])
def test_parse_last_event_id_ignores_malformed(raw: str | None) -> None:
    """A missing/malformed Last-Event-ID parses to None (fresh connection),
    never raising on the SSE entry path (#339)."""
    assert _parse_last_event_id(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("7", 7), (" 42 ", 42), ("-1", -1)])
def test_parse_last_event_id_parses_clean_int(raw: str, expected: int) -> None:
    assert _parse_last_event_id(raw) == expected


@pytest.mark.parametrize("elapsed", [None, float("nan"), float("inf")])
def test_backdated_charge_utc_returns_none_for_invalid_elapsed(elapsed: float | None) -> None:
    """#337: a missing / non-finite charge-elapsed defers to the store's own
    ``now`` (None) rather than fabricating a garbage backdated instant."""
    assert RoastRunner._backdated_charge_utc(elapsed) is None  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]


def test_backdated_charge_utc_backdates_by_elapsed() -> None:
    """#337: a finite charge-elapsed yields ``now - elapsed`` as an ISO-8601 UTC
    string in the PAST (the recovery breadcrumb matches the live backdated clock)."""
    before = datetime.now(UTC)
    result = RoastRunner._backdated_charge_utc(120.0)  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    assert result is not None
    charged_at = datetime.fromisoformat(result)
    # ~120 s before now (allow a little slack for test execution time).
    delta = (before - charged_at).total_seconds()
    assert 119.0 <= delta <= 122.0


@pytest.mark.asyncio
async def test_sse_endpoint_resumes_from_last_event_id_header(
    service: RoastService, store: RoastStore
) -> None:
    """The SSE endpoint reads the Last-Event-ID header and replays the gap (#339)."""
    await _make_run(store, "run-resume", RoastPhase.PREHEATING)
    service.events.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1
    service.events.emit(RoastEventKind.PHASE_CHANGED, {"phase": "development"})  # id 2
    request = cast(Request, _FakeRequest(last_event_id_header="1"))
    response = await stream_events("run-resume", request, service)
    # First the opening comment, then the replayed id-2 frame.
    frames = await _collect_frames(response, 2)
    assert frames[0] == ": connected\n\n"
    assert "id: 2" in frames[1]
    assert "event: phase_changed" in frames[1]


@pytest.mark.asyncio
async def test_sse_endpoint_resumes_from_last_event_id_query_param(
    service: RoastService, store: RoastStore
) -> None:
    """Falls back to the ``last_event_id`` query param when the header is absent
    (the hook's explicit-reconnect path, #339)."""
    await _make_run(store, "run-resume-q", RoastPhase.PREHEATING)
    service.events.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1
    service.events.emit(RoastEventKind.FAULT, {"n": 2})  # id 2
    request = cast(Request, _FakeRequest(last_event_id_query="1"))
    response = await stream_events("run-resume-q", request, service)
    frames = await _collect_frames(response, 2)
    assert frames[0] == ": connected\n\n"
    assert "id: 2" in frames[1]
    assert "event: fault" in frames[1]


@pytest.mark.asyncio
async def test_sse_endpoint_ignores_malformed_last_event_id_header(
    service: RoastService, store: RoastStore
) -> None:
    """A malformed Last-Event-ID is ignored: a fresh stream, no replay (#339)."""
    await _make_run(store, "run-bad-id", RoastPhase.PREHEATING)
    service.events.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1 (not replayed)
    request = cast(Request, _FakeRequest(last_event_id_header="not-a-number"))
    response = await stream_events("run-bad-id", request, service)
    # Only the opening comment is immediately available; no buffered frame replays.
    # A subsequent live emit still flows through.
    service.events.emit(RoastEventKind.RUN_COMPLETED, {})  # id 2 (live)
    frames = await _collect_frames(response, 2)
    assert frames[0] == ": connected\n\n"
    assert "id: 2" in frames[1]
    assert "id: 1" not in frames[1]


@pytest.mark.asyncio
async def test_sse_endpoint_header_takes_precedence_over_query_param(
    service: RoastService, store: RoastStore
) -> None:
    """When both are present the header wins; the query param is only a fallback
    for the header-less explicit-reconnect path (#339)."""
    await _make_run(store, "run-prec", RoastPhase.PREHEATING)
    service.events.emit(RoastEventKind.RUN_STARTED, {"n": 1})  # id 1
    service.events.emit(RoastEventKind.PHASE_CHANGED, {"phase": "development"})  # id 2
    service.events.emit(RoastEventKind.FAULT, {"n": 3})  # id 3
    # Header says "resume from 2" (replay only id 3); query param "0" would replay
    # everything — proving the header, not the query param, drove the resume.
    request = cast(Request, _FakeRequest(last_event_id_header="2", last_event_id_query="0"))
    response = await stream_events("run-prec", request, service)
    frames = await _collect_frames(response, 2)
    assert frames[0] == ": connected\n\n"
    assert "id: 3" in frames[1]
    assert "id: 1" not in frames[1] and "id: 2" not in frames[1]


@pytest.mark.asyncio
async def test_sse_endpoint_streams_typed_controller_event(
    service: RoastService, store: RoastStore
) -> None:
    await _make_run(store, "run-sse", RoastPhase.PREHEATING)
    response = await stream_events("run-sse", cast(Request, _FakeRequest()), service)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert service.events.subscriber_count == 1

    service.events.emit(RoastEventKind.PHASE_CHANGED, {"phase": "preheating"})
    frames = await _collect_frames(response, 2)  # ": connected" then the event
    assert service.events.subscriber_count == 0  # closed → unsubscribed

    frame = _parse_frame(frames[1])
    assert frame["event"] == "phase_changed"
    # phase_changed is enriched with enabled_actions (E10 option (a), D25).
    frame_data = cast("dict[str, object]", frame["data"])
    assert frame_data["phase"] == "preheating"
    assert frame_data["enabled_actions"] == [
        a.value for a in enabled_operator_actions(RoastPhase.PREHEATING)
    ]
    assert frame["id"] == 1


@pytest.mark.asyncio
async def test_sse_endpoint_streams_per_tick_telemetry(
    service: RoastService, store: RoastStore
) -> None:
    await _make_run(store, "run-tel", RoastPhase.DEVELOPMENT)
    response = await stream_events("run-tel", cast(Request, _FakeRequest()), service)
    service.events.emit_telemetry(
        TelemetryEventData(agent_phase=RoastPhase.DEVELOPMENT, bean_temp_c=201.5, env_temp_c=211.0)
    )
    frames = await _collect_frames(response, 2)
    frame = _parse_frame(frames[1])
    assert frame["event"] == "telemetry"
    assert isinstance(frame["data"], dict)
    assert frame["data"]["bean_temp_c"] == 201.5
    assert frame["data"]["agent_phase"] == "development"


@pytest.mark.asyncio
async def test_sse_endpoint_emits_heartbeat_when_idle(store: RoastStore) -> None:
    fast = RoastService(store, sse_heartbeat_seconds=0.05)
    await _make_run(store, "run-hb", RoastPhase.DEVELOPMENT)
    response = await stream_events("run-hb", cast(Request, _FakeRequest()), fast)
    # Collect two keepalives so the loop iterates past the first (no event).
    frames = await _collect_frames(response, 3)  # ": connected", heartbeat, heartbeat
    assert _parse_frame(frames[1])["event"] == "heartbeat"
    assert _parse_frame(frames[1])["data"] == {}
    assert _parse_frame(frames[2])["event"] == "heartbeat"


@pytest.mark.asyncio
async def test_sse_endpoint_404_for_unknown_run(service: RoastService) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await stream_events("nope", cast(Request, _FakeRequest()), service)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_sse_disconnect_unsubscribes_and_backend_continues(
    service: RoastService, store: RoastStore
) -> None:
    await _make_run(store, "run-dc", RoastPhase.DEVELOPMENT)
    request = cast(Request, _FakeRequest(disconnected=True))
    response = await stream_events("run-dc", request, service)
    assert service.events.subscriber_count == 1

    # A disconnected client: the stream sends the opening comment, sees the
    # disconnect, and stops — no cooling, no state change, just cleanup.
    frames = await _collect_frames(response, 5)
    assert frames == [": connected\n\n"]
    assert service.events.subscriber_count == 0

    # Backend continues with no client: a fresh stream still receives events.
    response2 = await stream_events("run-dc", cast(Request, _FakeRequest()), service)
    service.events.emit(RoastEventKind.PHASE_CHANGED, {"phase": "development"})
    frames2 = await _collect_frames(response2, 2)
    assert _parse_frame(frames2[1])["event"] == "phase_changed"


# --- E9: live controller-loop wiring (the E7 handoff + restart recovery) ---


def _reading(bean: float, env: float, **kwargs: object) -> RoastTelemetry:
    return RoastTelemetry.model_validate({"bean_temp_c": bean, "env_temp_c": env, **kwargs})


def _live_decision() -> RoastDecision:
    return RoastDecision(
        target_heat=55, target_fan=45, should_drop=False, confidence=0.9, rationale="hold"
    )


async def _live_service(
    store: RoastStore,
    *,
    mcp: FakeMCPClient,
    clock: FakeClock,
    raw_state: "_FakeRawState | None" = None,
    profile: RoastProfile | None = None,
    config: AppConfig | None = None,
    advisor: "FakeAdvisor | None" = None,
) -> tuple[RoastService, str]:
    """Start a live (run_loop=False) service into preheating; return it + run id."""
    resolved_config = config or AppConfig(
        controller=ControllerConfig(telemetry_log_interval_seconds=1.0)
    )
    service = RoastService(
        store,
        config=resolved_config,
        roaster=mcp,
        advisor=advisor or FakeAdvisor([], default_decision=_live_decision()),
        exporter=mcp,
        run_loop=False,
        clock=clock,
        raw_state=raw_state,
    )
    detail = await service.start_roast(profile or _profile())
    return service, detail.id


async def _tick(service: RoastService, clock: FakeClock) -> bool:
    assert service.runner is not None
    clock.advance(3.0)
    return await service.runner.tick_once()


def _start_session_roast_nums(mcp: FakeMCPClient) -> list[int | None]:
    """The recording_roast_num captured on each FakeMCPClient start_session call."""
    return [
        cast("int | None", args["recording_roast_num"])
        for name, args in mcp.calls
        if name == "start_session"
    ]


@pytest.mark.asyncio
async def test_recording_roast_num_is_store_derived_per_origin(
    store: RoastStore,
) -> None:
    """#385: the recording roast number handed to the MCP is prior COMPLETED roasts
    of this origin + 1 — stable across the per-process counter (a fresh service per
    run would otherwise always pass 1)."""
    profile = _profile(origin="Colombia")

    # A fresh service starts the first Colombia roast: store has no completed runs
    # → roast_num 1. Then finalise it so it counts toward the next.
    mcp1 = FakeMCPClient()
    service1 = RoastService(
        store,
        config=AppConfig(),
        roaster=mcp1,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        exporter=mcp1,
        run_loop=False,
        clock=FakeClock(),
    )
    detail = await service1.start_roast(profile)
    assert _start_session_roast_nums(mcp1) == [1]
    await store.complete_run(run_id=detail.id, outcome="completed", agent_phase=RoastPhase.COMPLETE)

    # A brand-new service (per-process counter reset to 0) starts a second Colombia
    # roast: the store-derived count makes it roast_num 2, not 1.
    mcp2 = FakeMCPClient()
    service2 = RoastService(
        store,
        config=AppConfig(),
        roaster=mcp2,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        exporter=mcp2,
        run_loop=False,
        clock=FakeClock(),
    )
    await service2.start_roast(profile)
    assert _start_session_roast_nums(mcp2) == [2]


def _ambient_status(
    *,
    status: str = "ok",
    temperature_c: float | None = 28.49,
    humidity_percent: float | None = 38.6,
    pressure_hpa: float | None = 1008.56,
    reason: str | None = None,
    ambient_running: bool | None = None,
    last_reading_monotonic_seconds: float | None = 10.0,
) -> AmbientStatus:
    """An ``AmbientStatus`` mirror instance (#342, D85).

    Defaults to an ``"ok"`` reading matching the hardware-validated live read
    (28.49 C / 38.6% / 1008.56 hPa). Pass ``status="unavailable"`` (or
    ``"disabled"``) with the numeric fields left ``None`` to exercise the
    fail-soft no-reading case.

    ``ambient_running`` defaults to ``status == "ok"`` — the healthy pairing —
    but is overridable so the STOPPED-BUT-``"ok"`` runtime is expressible
    (#732/#745a): ``_stop_locked`` drops the reader while leaving ``status`` at
    ``"ok"`` over the preserved last reading, which is the one shape a
    ``status``-only gate gets wrong."""
    return AmbientStatus(
        mode="yoctopuce",
        status=status,  # type: ignore[arg-type]  # parametrized over the Literal
        reason=reason,
        ambient_running=(status == "ok") if ambient_running is None else ambient_running,
        temperature_c=temperature_c,
        humidity_percent=humidity_percent,
        pressure_hpa=pressure_hpa,
        last_reading_monotonic_seconds=(last_reading_monotonic_seconds if status == "ok" else None),
    )


def _capture_ambient_writes(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[float | None, float | None, float | None]]:
    """Wrap ``set_ambient`` and return the triads it receives."""
    calls: list[tuple[float | None, float | None, float | None]] = []
    original = store.set_ambient

    async def _record(
        run_id: str,
        *,
        temperature_c: float | None,
        humidity_percent: float | None,
        pressure_hpa: float | None,
    ) -> None:
        calls.append((temperature_c, humidity_percent, pressure_hpa))
        await original(
            run_id,
            temperature_c=temperature_c,
            humidity_percent=humidity_percent,
            pressure_hpa=pressure_hpa,
        )

    monkeypatch.setattr(store, "set_ambient", _record)
    return calls


async def _drive_to_charge(service: RoastService, mcp: FakeMCPClient, clock: FakeClock) -> None:
    """Tick through the debounced T0 transition into a charged snapshot."""
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)
    assert service.runner is not None
    assert service.runner.controller_snapshot().charge_detected is True


def _session_state(
    *,
    fc_status: str,
    audio_running: bool,
    ambient_status: AmbientStatus | None = None,
) -> RoastSessionState:
    """A minimal valid ``RoastSessionState`` with the first-crack status set (#197)
    and the ambient status set (#342, D85; defaults to an ``"ok"`` reading)."""
    fc = FirstCrackStatus(
        mode="audio",
        status=fc_status,  # type: ignore[arg-type]  # parametrized over the Literal
        detected_at_utc=None,
        detected_monotonic_seconds=None,
        allow_manual_override=True,
        audio_running=audio_running,
    )
    return RoastSessionState(
        session_id="s1",
        active=True,
        phase="roasting",
        created_at_utc="2026-06-07T12:00:00+00:00",
        stopped_at_utc=None,
        elapsed_monotonic_seconds=10.0,
        heat_level_percent=50,
        fan_level_percent=30,
        cooling_on=False,
        beans_added_at_utc=None,
        first_crack_at_utc=None,
        beans_dropped_at_utc=None,
        cooling_started_at_utc=None,
        cooling_stopped_at_utc=None,
        faulted_at_utc=None,
        beans_added_monotonic_seconds=None,
        first_crack_monotonic_seconds=None,
        beans_dropped_monotonic_seconds=None,
        cooling_started_monotonic_seconds=None,
        cooling_stopped_monotonic_seconds=None,
        faulted_monotonic_seconds=None,
        roast_elapsed_seconds=None,
        development_time_seconds=None,
        development_percent=None,
        bean_temp_delta_60s_c=None,
        env_temp_delta_60s_c=None,
        bean_ror_c_per_min=None,
        env_ror_c_per_min=None,
        device_state=None,
        t0_status={  # type: ignore[arg-type]  # pydantic coerces the dict to T0Status
            "auto_detection_enabled": True,
            "status": "pending",
            "charge_temperature_c": None,
            "current_drop_c": None,
            "drop_threshold_c": 25.0,
            "detected_bean_temperature_c": None,
        },
        first_crack_status=fc,
        ambient_status=ambient_status or _ambient_status(),
        events=(),
        log_dir=None,
    )


class _FakeRawState:
    """A ``RawStateSource`` returning a fixed ``RoastSessionState`` (#197 test seam)."""

    def __init__(self, state: RoastSessionState | None) -> None:
        self._state = state

    @property
    def last_state(self) -> RoastSessionState | None:
        return self._state

    def set_state(self, state: RoastSessionState | None) -> None:
        """Swap the raw state a later tick observes (#342 once-only-latch test)."""
        self._state = state


@pytest.mark.asyncio
async def test_detail_enriches_active_run_with_live_mic_status(store: RoastStore) -> None:
    """detail() projects the live MCP first-crack status onto the active run's
    ``mic_status`` (#197) — running + detected → OK (green)."""
    await store.create_run(
        run_id="run-live",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    service = RoastService(
        store,
        raw_state=_FakeRawState(_session_state(fc_status="detected", audio_running=True)),
    )
    service.active_run_id = "run-live"
    detail = await service.detail("run-live")
    assert detail.mic_status is not None
    assert detail.mic_status.mic_health is MicHealth.OK
    assert detail.mic_status.fc_status == "detected"


@pytest.mark.asyncio
async def test_detail_mic_status_none_for_completed_run_with_stale_active_pointer(
    store: RoastStore,
) -> None:
    """A completed run never carries live mic_status, even while ``active_run_id``
    still points at it with a populated raw state (#200/Codex).

    ``active_run_id`` is set on start/recovery and not cleared at finalize, so the
    just-completed run can still match the active pointer; ``completed_at_utc`` is
    the authoritative "this is history" gate and history carries ``None``."""
    await store.create_run(
        run_id="run-just-done",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    await store.complete_run(
        run_id="run-just-done", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    service = RoastService(
        store,
        raw_state=_FakeRawState(_session_state(fc_status="detected", audio_running=True)),
    )
    service.active_run_id = "run-just-done"  # finalize did not clear the pointer
    detail = await service.detail("run-just-done")
    assert detail.completed_at_utc is not None
    assert detail.mic_status is None


@pytest.mark.asyncio
async def test_detail_mic_status_none_for_non_active_run(store: RoastStore) -> None:
    """A non-active run carries no live mic_status even when a raw state exists (#197)."""
    await store.create_run(
        run_id="run-old",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    service = RoastService(
        store,
        raw_state=_FakeRawState(_session_state(fc_status="detected", audio_running=True)),
    )
    service.active_run_id = "run-active-other"  # not this run
    detail = await service.detail("run-old")
    assert detail.mic_status is None


@pytest.mark.asyncio
async def test_detail_mic_status_none_before_first_read(store: RoastStore) -> None:
    """The active run carries no mic_status until the first MCP read (#197)."""
    await store.create_run(
        run_id="run-fresh",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    service = RoastService(store, raw_state=_FakeRawState(None))
    service.active_run_id = "run-fresh"
    detail = await service.detail("run-fresh")
    assert detail.mic_status is None


@pytest.mark.asyncio
async def test_detail_mic_status_none_when_no_raw_state_source(store: RoastStore) -> None:
    """API-only mode (no raw-state source wired) → mic_status is None (#197)."""
    await store.create_run(
        run_id="run-apionly",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    service = RoastService(store)  # no raw_state
    service.active_run_id = "run-apionly"
    detail = await service.detail("run-apionly")
    assert detail.mic_status is None


@pytest.mark.asyncio
async def test_submit_operator_action_410_on_terminal_run(store: RoastStore) -> None:
    """An operator action on a completed/faulted run is gone (410), not queued."""
    await store.create_run(
        run_id="run-done",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="run-done", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    service = RoastService(store)
    with pytest.raises(RoastRunGoneError):
        await service.submit_operator_action(
            "run-done", OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )


@pytest.mark.asyncio
async def test_faulted_unacknowledged_run_does_not_410(store: RoastStore) -> None:
    """#206 + #210: a FAULTED run with no ``completed_at`` (the post-#206 common
    case — a fault no longer auto-finalises) is NOT terminal, so an operator action
    is NOT 410'd: stop_cooling / start_cooling / DROP_BEANS (#210 — dump beans from
    the hot drum) / emergency_stop / acknowledge_fault are all accepted end-to-end
    (the matrix pre-check passes), so a fault never strands a physically-running
    machine or scorching beans."""
    await store.create_run(
        run_id="run-faulted",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    service = RoastService(store)
    for action in (
        OperatorAction.STOP_COOLING,
        OperatorAction.START_COOLING,
        OperatorAction.DROP_BEANS,
        OperatorAction.EMERGENCY_STOP,
        OperatorAction.ACKNOWLEDGE_FAULT,
    ):
        result = await service.submit_operator_action(
            "run-faulted", OperatorActionRequest(action=action)
        )
        assert result.result == "accepted", action
        assert result.queued is True, action


@pytest.mark.asyncio
async def test_operator_queue_bound_reports_failed_when_full(store: RoastStore) -> None:
    """A full queue (pathological spam) reports the action failed — never a 500
    and never a silent drop."""
    await store.create_run(
        run_id="run-busy",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    service = RoastService(store)
    for _ in range(service.OPERATOR_QUEUE_MAX):
        service.operator_queue.put_nowait(
            QueuedOperatorAction(run_id="run-busy", action=OperatorAction.EMERGENCY_STOP)
        )
    result = await service.submit_operator_action(
        "run-busy", OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
    )
    assert result.result == "failed"
    assert result.queued is False


@pytest.mark.asyncio
async def test_restart_into_active_phase_enters_recovery_without_resuming(
    store: RoastStore,
) -> None:
    """The restart invariant: a possibly-active persisted run recovers into
    operator_recovery_required and resumes no hardware."""
    await store.create_run(
        run_id="run-crashed",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    mcp = FakeMCPClient()
    service = RoastService(
        store,
        roaster=mcp,
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    await service.recover_on_start()
    recovered = await store.read_run("run-crashed")
    assert recovered is not None
    assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert service.active_run_id == "run-crashed"
    # No heat/fan/session write was issued on recovery.
    assert mcp.commands() == []


@pytest.mark.asyncio
async def test_recover_on_start_auto_finalizes_a_stale_faulted_run(
    store: RoastStore,
) -> None:
    """#331: a prior session's unfinalised FAULTED run is AUTO-FINALISED on restart
    — NOT restored as the active run. A restart is a new session; restoring the
    stale fault as active stranded the operator on it and blocked a fresh roast
    (the roast-3 boot-onto-"test 6" bug). It is moved terminal (outcome ``faulted``,
    fault_reason preserved) so it lands in history, the boot is clean (no active
    run), and no heat/fan/MCP write is issued (restart-never-auto-resumes intact).
    Supersedes the prior #206 "re-enter operable-faulted on restart" behaviour: the
    in-SESSION operable-faulted path is unchanged; this is only the cross-restart
    stale-fault case."""
    await store.create_run(
        run_id="run-faulted-crash",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    # The real bug shape: a fault that was NEVER finalised (completed_at stays NULL
    # — a crash / unacknowledged fault from a prior session). The fault_reason was
    # persisted when it first faulted. Set it directly (no completion, so the
    # immutability trigger — which guards only ALREADY-completed rows — does not fire).
    await store.connection.execute(
        "UPDATE roast_runs SET fault_reason = ? WHERE id = ?",
        ("env 242 C exceeds the hard ceiling 240 C", "run-faulted-crash"),
    )
    await store.connection.commit()
    mcp = FakeMCPClient()
    service = RoastService(
        store,
        roaster=mcp,
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    await service.recover_on_start()
    recovered = await store.read_run("run-faulted-crash")
    assert recovered is not None
    # Auto-finalised: terminal, outcome faulted, in history — NOT the active run.
    assert recovered.completed_at_utc is not None
    assert recovered.outcome == "faulted"
    assert recovered.agent_phase is RoastPhase.FAULTED
    # fault_reason PRESERVED for diagnosis (finalize_stale_faulted_run never touches it).
    assert recovered.fault_reason == "env 242 C exceeds the hard ceiling 240 C"
    # Boot is clean: no active run, no runner/loop started, no MCP write, no resume.
    assert service.active_run_id is None
    assert service.runner is None
    assert mcp.commands() == []
    # The store agrees there is no active run, so a fresh roast is not blocked.
    assert await store.active_run() is None


@pytest.mark.asyncio
async def test_recover_on_start_active_roast_still_recovers_to_recovery_required(
    store: RoastStore,
) -> None:
    """#331 regression guard: the NORMAL recovery path is unchanged — a persisted
    ACTIVE-ROAST phase (not faulted) still enters operator_recovery_required on
    restart, with no heat/fan/MCP write (restart-never-auto-resumes). Only the
    stale-faulted case changed."""
    await store.create_run(
        run_id="run-mid-roast",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    mcp = FakeMCPClient()
    service = RoastService(
        store, roaster=mcp, advisor=FakeAdvisor(), run_loop=False, clock=FakeClock()
    )
    await service.recover_on_start()
    assert service.active_run_id == "run-mid-roast"
    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    snapshot = service.runner.controller_snapshot()
    assert (snapshot.current_heat, snapshot.current_fan) == (0, 0)  # no auto-resume
    assert mcp.commands() == []  # no MCP write on recovery
    # Emergency stop stays available from recovery.
    assert OperatorAction.EMERGENCY_STOP in enabled_operator_actions(
        RoastPhase.OPERATOR_RECOVERY_REQUIRED
    )


@pytest.mark.asyncio
async def test_start_roast_refuses_an_enabled_doctrine_with_an_unknown_cadence(
    store: RoastStore,
) -> None:
    """#732, post-open Codex round 4: the explicit-cadence requirement lives at
    the START boundary, and is enforced there rather than at construction.

    Unset means the cadence comes from a hand-authored MCP yaml this process
    does not read — including one merely sitting in the working directory,
    which `resolve_mcp_yaml_source_path` honours. A cadence wider than the
    freshness bound declines every healthy reading, so c11 would run its
    absent-ambient fallback for the entire roast: green, and meaningless.

    Refusing at start costs a config edit. The earlier placement, as a
    construction rule, cost a running roast instead — recovery rebuilds a
    config for a run already in progress, so a resume silently retired the
    doctrine. Both halves are asserted here: the refusal, and that stating the
    cadence is the way through."""
    service = RoastService(
        store,
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
        config=AppConfig(
            controller=ControllerConfig(
                ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
            ),
            mcp_device=MCPDeviceConfig(),
        ),
    )

    # Driven through the REAL entry point, not the helper: a test that called
    # the helper directly would still pass with the call site deleted from
    # start_roast, which is the wiring that actually has to hold.
    with pytest.raises(RoastConfigError, match="ambient_poll_interval_seconds"):
        await service.start_roast(_profile())
    assert await store.active_run() is None  # refused before any run was persisted

    # Stating it is the way through; and with the doctrine off it never binds.
    for config in (
        AppConfig(
            controller=ControllerConfig(
                ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
            ),
            mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=30.0),
        ),
        AppConfig(mcp_device=MCPDeviceConfig()),
    ):
        ok = RoastService(
            store,
            roaster=FakeMCPClient(),
            advisor=FakeAdvisor(),
            run_loop=False,
            clock=FakeClock(),
            config=config,
        )
        ok._require_explicit_ambient_cadence(config)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_start_roast_checks_the_cadence_of_the_RELOADED_config_not_the_stale_one(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#732, post-open round 6 (Claude and Codex found this independently).

    In live-serve mode `start_roast` reloads config from disk (D76/D78
    apply-next-roast) partway through. The cadence gate ran ABOVE that reload,
    so it judged the config from before the operator's last `PUT /api/config`
    rather than the one the roast actually runs under. This test pins the
    admit direction; its sibling below pins the lockout direction.
    """
    enabled = ControllerConfig(
        ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
    )
    with_cadence = AppConfig(
        controller=enabled, mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=30.0)
    )
    without_cadence = AppConfig(controller=enabled, mcp_device=MCPDeviceConfig())

    def _service(initial: AppConfig, on_disk: AppConfig) -> RoastService:
        def _load() -> tuple[AppConfig, frozenset[str]]:
            return on_disk, frozenset()

        monkeypatch.setattr("roastpilot_agent.api.load_app_config", _load)
        return RoastService(
            store, config=initial, run_loop=False, clock=FakeClock(), live_serve_mode=True
        )

    # The stale value must not ADMIT a roast the live config forbids: a cadence
    # that was explicit last time does not vouch for one that is now unset.
    with pytest.raises(RoastConfigError, match="ambient_poll_interval_seconds"):
        await _service(with_cadence, without_cadence).start_roast(_profile())
    assert await store.active_run() is None


@pytest.mark.asyncio
async def test_start_roast_cannot_lock_itself_out_on_a_config_already_corrected(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#732, post-open round 6: the operator-facing half, pinned on its own.

    Deliberately a SEPARATE test from its sibling above rather than a second
    assertion inside it: sharing one test meant the first `pytest.raises` aborted
    the run before this half executed, so a mutation restoring the original
    ordering was killed only by the other direction and this one was never
    actually exercised.

    The scenario is an operator at a preheated roaster. The running config has
    the doctrine enabled with no cadence, so a start is refused; the operator
    corrects the file and tries again. Because the reload sits BELOW the gate, the
    old ordering never re-read the file: `self._config` stayed stale and every
    retry reproduced the identical refusal until the agent was restarted.
    """
    enabled = ControllerConfig(
        ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
    )
    corrected_on_disk = AppConfig(
        controller=enabled, mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=30.0)
    )

    def _load() -> tuple[AppConfig, frozenset[str]]:
        return corrected_on_disk, frozenset()

    monkeypatch.setattr("roastpilot_agent.api.load_app_config", _load)
    service = RoastService(
        store,
        config=AppConfig(controller=enabled, mcp_device=MCPDeviceConfig()),  # stale + bad
        run_loop=False,
        clock=FakeClock(),
        live_serve_mode=True,
    )

    detail = await service.start_roast(_profile())

    active = await store.active_run()
    assert active is not None and active.run_id == detail.id


def test_recovery_config_reraises_a_failure_the_doctrine_did_not_cause() -> None:
    """#732: the degradation is scoped to the ambient clash and NOTHING else.

    ``_build_recovery_config`` retires the advisory doctrine only when the
    frozen doctrine is enabled; any other validation failure must propagate
    unchanged. This is the safety-preserving half of that fix — the line that
    guarantees a genuinely broken frozen config (here the safety-owned
    ceiling-guard bound, D88 A1) is never silently absorbed by a recovery that
    then proceeds as if nothing were wrong.

    Independent pre-open triage found this branch uncovered suite-wide and
    true only by code reading, which for a fail-closed guard is the state
    worth fixing before it is the state relied on. ``FrozenRunConfig`` carries
    no cross-section validator of its own, so the offending pair is
    constructible exactly as a historical record could hold it."""
    service = RoastService(
        cast(RoastStore, None),
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    frozen = FrozenRunConfig(
        controller=ControllerConfig(
            post_first_crack_control=PostFirstCrackControl(ceiling_guard_temp_c=240.0)
        ),
        safety=SafetyLimits(),
    )
    assert frozen.controller.ambient_fan_doctrine.enabled is False

    with pytest.raises(ValidationError, match="ceiling_guard_temp_c"):
        service._build_recovery_config(frozen)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_recover_on_start_survives_a_frozen_doctrine_the_live_poll_interval_voids(
    store: RoastStore,
) -> None:
    """#732: the ambient cross-section guard must never be able to abort a RECOVERY.

    Recovery is the one place a run's FROZEN controller meets the CURRENT device
    config, and #732's check spans exactly that pair. They can legitimately
    disagree with no repair available, because the run already happened: here a
    run started with the doctrine on at a 90 s bound, and the operator has since
    widened the ambient poll interval to 60 s — a save the live config accepted,
    because by then the live controller had the doctrine off.

    Raising there would strand an operator with a possibly-active run and no
    route into ``operator_recovery_required``. So the clash retires the doctrine
    for the recovered run instead: fail-safe (the advisor gets the same
    absent-ambient branch the freshness gate already produces, and destination
    enforcement is disabled) and, above all, recoverable. Asserted end to end
    rather than on the helper, because the invariant at stake is the restart
    one."""
    await store.create_run(
        run_id="run-doctrine-voided",
        profile=_profile(),
        config=AppConfig(
            controller=ControllerConfig(
                ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
            ),
            # The run's OWN generation was self-consistent when it started; the
            # clash under test is against the CURRENT device config below.
            mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=60.0),
        ),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    # 120 s cadence against a 90 s bound: the reading genuinely cannot arrive
    # fresh, so the doctrine truly is unusable and retiring it is correct.
    live = AppConfig(mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=120.0))
    service = RoastService(
        store,
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
        config=live,
    )

    await service.recover_on_start()

    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    doctrine = service.runner._config.controller.ambient_fan_doctrine  # pyright: ignore[reportPrivateUsage]
    # Only the advisory doctrine was retired, and only because it could not be
    # satisfied — its other fields, and the rest of the frozen generation, are
    # untouched.
    assert doctrine.enabled is False
    assert doctrine.max_reading_age_seconds == 90.0
    assert service.runner._config.controller.tick_interval_seconds == 1.0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_recover_on_start_preserves_a_frozen_doctrine_that_still_fits(
    store: RoastStore,
) -> None:
    """#732, post-open Codex P2: recovery must not retire a WORKING doctrine.

    An earlier revision demanded the bound be twice the cadence, and retired the
    doctrine whenever that failed. But a 90 s bound against a 60 s cadence
    works: a healthy reading is at its oldest just before the next poll, so it
    always arrives inside the bound. Retiring there silently switched a
    recovered run from ambient-aware advice to the absent-ambient branch for the
    rest of the roast — the opposite of the fail-safe framing used to justify
    it, since the run had been fine.

    The rule is now the correctness line (bound >= cadence), so this pair no
    longer clashes at all and the frozen doctrine survives the restart intact.
    Pinned separately from the retirement case because these two differ only in
    the cadence, and it was that single number that made the old behaviour look
    reasonable."""
    await store.create_run(
        run_id="run-doctrine-still-fits",
        profile=_profile(),
        config=AppConfig(
            controller=ControllerConfig(
                ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
            ),
            # The run's OWN generation was self-consistent when it started; the
            # clash under test is against the CURRENT device config below.
            mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=60.0),
        ),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    live = AppConfig(mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=60.0))
    service = RoastService(
        store,
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
        config=live,
    )

    await service.recover_on_start()

    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    doctrine = service.runner._config.controller.ambient_fan_doctrine  # pyright: ignore[reportPrivateUsage]
    assert doctrine.enabled is True
    assert doctrine.max_reading_age_seconds == 90.0


@pytest.mark.asyncio
async def test_recover_on_start_restores_frozen_controller_and_safety_generation(
    store: RoastStore,
) -> None:
    """A restart keeps the active roast's frozen config, not next-roast edits."""
    frozen = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=7.0,
            post_first_crack_control=PostFirstCrackControl(recovery_enabled=False),
        ),
        safety=SafetyLimits(max_bean_temp_c=225.0),
    )
    await store.create_run(
        run_id="run-frozen-generation",
        profile=_profile(),
        config=frozen,
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    process_current = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            post_first_crack_control=PostFirstCrackControl(recovery_enabled=True),
        ),
        safety=SafetyLimits(max_bean_temp_c=229.0),
    )
    mcp = FakeMCPClient()
    service = RoastService(
        store,
        config=process_current,
        roaster=mcp,
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )

    await service.recover_on_start()

    assert service.runner is not None
    snapshot = service.runner.controller_snapshot()
    assert snapshot.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert snapshot.post_fc_recovery_enabled is False
    assert service.runner._config.controller == frozen.controller  # pyright: ignore[reportPrivateUsage]
    assert service.runner._config.safety == frozen.safety  # pyright: ignore[reportPrivateUsage]
    assert service._safety.limits == frozen.safety  # pyright: ignore[reportPrivateUsage]
    assert mcp.commands() == []

    # Once that run is terminal, a fresh start returns to process-current
    # apply-next-roast config even in an explicitly configured test service.
    await store.complete_run(
        run_id="run-frozen-generation",
        outcome="aborted",
        agent_phase=RoastPhase.COMPLETE,
    )
    await service.start_roast(_profile())
    assert service.runner is not None
    assert service.runner._config.controller == process_current.controller  # pyright: ignore[reportPrivateUsage]
    assert service.runner._config.safety == process_current.safety  # pyright: ignore[reportPrivateUsage]
    assert service._safety.limits == process_current.safety  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_live_run_persists_t0_detected_at_on_charge(store: RoastStore) -> None:
    """#235: the live runner persists the absolute charge/T0 instant once the
    controller stamps its charge clock (the debounced T0 transition), so a later
    restart can restore the advisory DTR clock. Before charge the column is
    ``None``; after the debounced T0 it is set and the recovery read carries it."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)  # preheating
    # Not charged yet: no T0 instant persisted.
    assert (await store.read_latest_run()).t0_detected_at_utc is None  # type: ignore[union-attr]
    # Debounce T0: the default t0_debounce_ticks consecutive T0 readings transition
    # into pre-first-crack and stamp the charge clock.
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    persisted = await store.read_latest_run()
    assert persisted is not None
    assert persisted.t0_detected_at_utc is not None  # charge instant now recorded


@pytest.mark.asyncio
async def test_live_run_persists_ambient_on_charge_when_ok(store: RoastStore) -> None:
    """#342 (D85): the live runner persists the ambient triad once the controller
    stamps its charge clock — mirroring the T0 capture (#235) — reading it off the
    already-available raw MCP state (the same state ``mic_status`` projects from,
    no extra MCP round-trip). A ``status == "ok"`` reading persists the real
    values."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(_session_state(fc_status="pending", audio_running=False))
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    # Not charged yet: no ambient persisted.
    not_yet = await store.read_run(run_id)
    assert not_yet is not None
    assert not_yet.ambient_temp_c is None
    # Debounce T0 into pre-first-crack (stamps the charge clock).
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert detail.ambient_temp_c == pytest.approx(28.49)
    assert detail.ambient_humidity_pct == pytest.approx(38.6)
    assert detail.ambient_pressure_hpa == pytest.approx(1008.56)


@pytest.mark.asyncio
async def test_live_run_persists_null_ambient_when_unavailable(store: RoastStore) -> None:
    """#342: an ``"unavailable"``/``"disabled"`` MCP ambient config persists nulls,
    never raises, and the roast continues normally — the MCP's own fail-soft
    contract, mirrored agent-side."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    unavailable = _ambient_status(
        status="unavailable", temperature_c=None, humidity_percent=None, pressure_hpa=None
    )
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=unavailable)
    )
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert detail.ambient_temp_c is None
    assert detail.ambient_humidity_pct is None
    assert detail.ambient_pressure_hpa is None


@pytest.mark.asyncio
async def test_charge_capture_persists_nulls_for_a_stopped_but_ok_runtime(
    store: RoastStore,
) -> None:
    """#745a: a stopped runtime's preserved reading is NOT recorded as the run's ambient.

    ``AmbientSessionRuntime._stop_locked`` drops the reader (``ambient_running``
    goes ``False``) while deliberately leaving ``status`` at ``"ok"`` over the
    last reading, which can then never change. #741 gave the live advisor and
    dashboard paths the ``ambient_running`` gate; the charge-instant corpus
    capture kept its own ``status``-only test and so recorded that frozen
    reading as the run's breadcrumb — the run reads back as "had ambient" while
    every live path correctly saw none.

    That is a mislabelled RP-B arm (#709): the offline eval (#737) reads exactly
    this column and stamps it into every replayed context, so the comparison
    would run on a reading the doctrine never reasoned on.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    # status "ok" with the full triad intact — only the reader has stopped.
    stopped = _ambient_status(status="ok", ambient_running=False)
    assert stopped.temperature_c is not None, "the frozen reading must still be present"
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=stopped)
    )
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)

    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert detail.ambient_temp_c is None
    assert detail.ambient_humidity_pct is None
    assert detail.ambient_pressure_hpa is None


@pytest.mark.asyncio
async def test_charge_capture_persists_nulls_for_an_undateable_reading(
    store: RoastStore,
) -> None:
    """#745, third-round fold: the corpus must not record what the advisor declines.

    A NaN reading stamp on an otherwise ok/running status makes the live path
    decline the reading — the freshness clock cannot date it, so the age is
    unknown and the controller fails closed. Persisting the numeric triad anyway
    would leave the run reading back as "had ambient" while no advisory call
    ever saw it, and #737's offline eval would then stamp that value into every
    replayed context. The capture and the live path share one predicate so the
    hole between the two #745 fixes cannot open.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    undateable = _ambient_status(status="ok").model_copy(
        update={"last_reading_monotonic_seconds": float("nan")}
    )
    assert undateable.temperature_c is not None, "the reading itself must still be present"
    assert undateable.ambient_running is True, "only the STAMP is unusable here"
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=undateable)
    )
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)

    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.ambient_temp_c is None
    assert detail.ambient_humidity_pct is None
    assert detail.ambient_pressure_hpa is None


@pytest.mark.asyncio
async def test_charge_capture_persists_nulls_for_a_non_finite_reading(
    store: RoastStore,
) -> None:
    """#752: a malformed reading must not become the run's corpus breadcrumb.

    SQLite round-trips ``±inf`` faithfully (unlike ``NaN``, which it silently
    stores as ``NULL``), so a non-finite reading survives the write and comes
    back out — which is why ``scripts/rpd_corpus_score.py`` carries a
    ``_finite_or_none`` normalisation on read. That shim guards a reachable
    shape rather than recording an observed row, and reachable is enough: the
    value must be stopped at the reading boundary instead, so the column keeps
    meaning "a real reading of the room at charge" (#342, D85) rather than
    "whatever the probe last emitted".

    Asserted through the whole live path — MCP state, charge detection, store —
    rather than on the predicate alone, because the predicate being right is
    only half the claim; the other half is that this capture path goes through
    it (#745).
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    malformed = _ambient_status(status="ok", temperature_c=float("inf"))
    assert malformed.ambient_running is True, "only the READING is malformed here"
    assert malformed.humidity_percent is not None, "the other members are intact"
    assert malformed.last_reading_monotonic_seconds is not None, "the stamp is usable (#745)"
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=malformed)
    )
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)

    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK  # roast unaffected
    # NULL in the column, not a non-finite float: the whole triad is voided,
    # including the intact members from the same poll.
    assert detail.ambient_temp_c is None
    assert detail.ambient_humidity_pct is None
    assert detail.ambient_pressure_hpa is None


@pytest.mark.asyncio
async def test_ambient_capture_is_fail_soft_on_store_error(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#342: a ``set_ambient`` store failure never crashes the safety tick (mirrors
    the ``_persist_t0_if_charged`` swallow behaviour) — the roast keeps advancing
    and the run's ambient triad simply reads back ``None``."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(_session_state(fc_status="pending", audio_running=False))
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "set_ambient", _boom)
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        finalized = await _tick(service, clock)
        assert not finalized  # the tick keeps advancing, never crashes
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK  # roast unaffected
    assert detail.ambient_temp_c is None  # the failed write never landed


@pytest.mark.asyncio
async def test_ambient_capture_runs_once_not_every_tick(store: RoastStore) -> None:
    """#342: the ambient capture is latched — it persists once at charge, not on
    every subsequent tick, even though the raw MCP state keeps reporting an "ok"
    reading every tick thereafter."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(_session_state(fc_status="pending", audio_running=False))
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    mcp.frames = [_reading(bean=178.0, env=185.0, t0_detected=True)]
    for _ in range(ControllerConfig().t0_debounce_ticks + 1):
        await _tick(service, clock)
    assert service.runner is not None
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]

    # A later ambient READING change (e.g. the probe now unavailable) must never
    # overwrite the already-latched charge-time capture.
    raw_state.set_state(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(
                status="unavailable", temperature_c=None, humidity_percent=None, pressure_hpa=None
            ),
        )
    )
    mcp.frames = [_reading(bean=182.0, env=190.0)]
    await _tick(service, clock)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.ambient_temp_c == pytest.approx(28.49)  # unchanged — latched


@pytest.mark.asyncio
async def test_malformed_ambient_warns_once_per_run(
    store: RoastStore, caplog: pytest.LogCaptureFixture
) -> None:
    """#758: repeated malformed ticks emit one actionable raw-value warning."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    malformed = _ambient_status(temperature_c=float("inf"))
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=malformed)
    )
    service, _ = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)

    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.api"):
        await _drive_to_charge(service, mcp, clock)

    records = [
        record
        for record in caplog.records
        if record.name == "roastpilot_agent.api" and "#758" in record.getMessage()
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "temperature_c=inf" in message
    assert "humidity_percent=38.6" in message
    assert "pressure_hpa=1008.56" in message
    assert "last_reading_monotonic_seconds=10.0" in message
    assert all(term not in message.lower() for term in ("poll", "cadence", "stale"))


@pytest.mark.asyncio
async def test_malformed_ambient_warns_before_charge(
    store: RoastStore, caplog: pytest.LogCaptureFixture
) -> None:
    """#758: the diagnostic is per tick and not gated on charge detection."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    service, _ = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)

    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.api"):
        await _tick(service, clock)

    assert service.runner is not None
    assert service.runner.controller_snapshot().charge_detected is False
    assert sum("#758" in record.getMessage() for record in caplog.records) == 1


@pytest.mark.parametrize(
    "ambient",
    [
        _ambient_status(
            status="unavailable",
            temperature_c=None,
            humidity_percent=None,
            pressure_hpa=None,
        ),
        _ambient_status(ambient_running=False),
        _ambient_status(
            temperature_c=None,
            humidity_percent=None,
            pressure_hpa=None,
            last_reading_monotonic_seconds=None,
        ),
    ],
    ids=("unavailable", "stopped-with-frozen-triad", "live-never-sampled"),
)
@pytest.mark.asyncio
async def test_absent_ambient_never_triggers_the_malformed_warning(
    store: RoastStore,
    caplog: pytest.LogCaptureFixture,
    ambient: AmbientStatus,
) -> None:
    """#758: unavailable, stopped, and never-sampled probes stay quiet."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=ambient)
    )
    service, _ = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)

    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.api"):
        for _ in range(4):
            await _tick(service, clock)

    assert not any("#758" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_malformed_warning_is_not_the_cadence_warning(
    store: RoastStore, caplog: pytest.LogCaptureFixture
) -> None:
    """#758 does not consume or impersonate the independent #732 warning."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            ambient_fan_doctrine=AmbientFanDoctrine(enabled=True),
        ),
        mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=30.0),
    )
    service, _ = await _live_service(
        store, mcp=mcp, clock=clock, raw_state=raw_state, config=config
    )

    with (
        caplog.at_level(logging.WARNING, logger="roastpilot_agent.api"),
        caplog.at_level(logging.WARNING, logger="roastpilot_agent.controller"),
    ):
        await _tick(service, clock)

    assert any(
        record.name == "roastpilot_agent.api" and "#758" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        record.name == "roastpilot_agent.controller" and "#732" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_charge_capture_retries_through_a_transient_malformed_reading(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#758: a malformed charge tick does not permanently spend the latch."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    await _drive_to_charge(service, mcp, clock)
    await _tick(service, clock)
    raw_state.set_state(_session_state(fc_status="pending", audio_running=False))
    await _tick(service, clock)

    detail = await store.read_run(run_id)
    assert detail is not None
    assert (detail.ambient_temp_c, detail.ambient_humidity_pct, detail.ambient_pressure_hpa) == (
        28.49,
        38.6,
        1008.56,
    )
    assert calls == [(28.49, 38.6, 1008.56)]
    assert service.runner is not None
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_charge_capture_retries_a_live_not_yet_sampled_probe(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#758: a live probe awaiting its first sample gets the same bounded retry."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    never_sampled = _ambient_status(
        temperature_c=None,
        humidity_percent=None,
        pressure_hpa=None,
        last_reading_monotonic_seconds=None,
    )
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=never_sampled)
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    await _drive_to_charge(service, mcp, clock)
    raw_state.set_state(_session_state(fc_status="pending", audio_running=False))
    await _tick(service, clock)

    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.ambient_temp_c == pytest.approx(28.49)
    assert calls == [(28.49, 38.6, 1008.56)]


@pytest.mark.asyncio
async def test_charge_capture_gives_up_after_the_grace_window(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#758: a live malformed probe cannot retry past the bounded window."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    await _drive_to_charge(service, mcp, clock)
    assert service.runner is not None
    for _ in range(40):
        if service.runner._ambient_persisted:  # pyright: ignore[reportPrivateUsage]
            break
        await _tick(service, clock)

    elapsed = service.runner.controller_snapshot().charge_elapsed_seconds
    assert elapsed is not None
    assert elapsed > api_module.AMBIENT_CAPTURE_GRACE_SECONDS
    assert calls == [(None, None, None)]
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]

    raw_state.set_state(_session_state(fc_status="pending", audio_running=False))
    await _tick(service, clock)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.ambient_temp_c is None
    assert calls == [(None, None, None)]


@pytest.mark.asyncio
async def test_charge_capture_still_latches_immediately_for_an_absent_probe(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#758: a legitimately unavailable probe gets no retry window."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    unavailable = _ambient_status(
        status="unavailable",
        temperature_c=None,
        humidity_percent=None,
        pressure_hpa=None,
    )
    raw_state = _FakeRawState(
        _session_state(fc_status="pending", audio_running=False, ambient_status=unavailable)
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service, _ = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    await _drive_to_charge(service, mcp, clock)

    assert calls == [(None, None, None)]
    assert service.runner is not None
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_expired_window_store_failure_cannot_be_backfilled_by_recovery(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#796 review finding: latch the expired-window DECISION, not just the write.

    Reproduces the exact reachable-corruption sequence: the grace window
    expires with the probe still malformed, the first post-expiry
    ``set_ambient`` call fails transiently (so ``_ambient_persisted`` stays
    False), and only THEN does the probe recover to a valid reading. Before
    this fix, the early-return guard no longer applied once the triad stopped
    being all-``None`` on the retry tick, so the recovered reading — taken
    arbitrarily late in the roast, possibly during cooling next to a hot
    roaster — would be written into the charge-time ambient columns. The fix
    pins the expired-window triad to all-``None`` before every write attempt,
    so the retry can only ever persist nulls.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    await _drive_to_charge(service, mcp, clock)
    assert service.runner is not None

    original_set_ambient = store.set_ambient
    calls: list[tuple[float | None, float | None, float | None]] = []
    attempts = 0

    async def _fails_once_then_succeeds(
        run_id_arg: str,
        *,
        temperature_c: float | None,
        humidity_percent: float | None,
        pressure_hpa: float | None,
    ) -> None:
        nonlocal attempts
        attempts += 1
        calls.append((temperature_c, humidity_percent, pressure_hpa))
        if attempts == 1:
            raise RuntimeError("disk full")
        await original_set_ambient(
            run_id_arg,
            temperature_c=temperature_c,
            humidity_percent=humidity_percent,
            pressure_hpa=pressure_hpa,
        )

    monkeypatch.setattr(store, "set_ambient", _fails_once_then_succeeds)

    # Advance past the deadline (probe still malformed) until the first
    # post-expiry write is attempted and fails.
    for _ in range(40):
        await _tick(service, clock)
        if attempts >= 1:
            break
    assert attempts == 1
    assert calls == [(None, None, None)]
    assert service.runner._ambient_persisted is False  # pyright: ignore[reportPrivateUsage]

    # The probe recovers to a valid reading before the retry tick.
    raw_state.set_state(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(
                temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
            ),
        )
    )
    await _tick(service, clock)

    assert calls == [(None, None, None), (None, None, None)]
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.ambient_temp_c is None
    assert detail.ambient_humidity_pct is None
    assert detail.ambient_pressure_hpa is None


@pytest.mark.asyncio
async def test_expired_window_write_stays_retryable_after_a_transient_failure(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#796: the fail-soft/retryable contract survives the latch fix — a store
    error on the first post-expiry NULL write must not block a later tick from
    retrying and successfully persisting nulls."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    service, run_id = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    await _drive_to_charge(service, mcp, clock)
    assert service.runner is not None

    original_set_ambient = store.set_ambient
    calls: list[tuple[float | None, float | None, float | None]] = []
    attempts = 0

    async def _fails_once_then_succeeds(
        run_id_arg: str,
        *,
        temperature_c: float | None,
        humidity_percent: float | None,
        pressure_hpa: float | None,
    ) -> None:
        nonlocal attempts
        attempts += 1
        calls.append((temperature_c, humidity_percent, pressure_hpa))
        if attempts == 1:
            raise RuntimeError("disk full")
        await original_set_ambient(
            run_id_arg,
            temperature_c=temperature_c,
            humidity_percent=humidity_percent,
            pressure_hpa=pressure_hpa,
        )

    monkeypatch.setattr(store, "set_ambient", _fails_once_then_succeeds)

    for _ in range(40):
        await _tick(service, clock)
        if attempts >= 1:
            break
    assert attempts == 1
    assert service.runner._ambient_persisted is False  # pyright: ignore[reportPrivateUsage]

    # Probe still malformed on the retry tick — the retry must still fire and
    # this time succeed.
    await _tick(service, clock)

    assert attempts == 2
    assert calls == [(None, None, None), (None, None, None)]
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.ambient_temp_c is None


@pytest.mark.parametrize(
    "charge_elapsed_seconds",
    [None, float("nan"), float("-inf"), float("inf")],
    ids=("none", "nan", "neg-inf", "inf"),
)
@pytest.mark.asyncio
async def test_an_unusable_charge_clock_cannot_affect_the_window(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    charge_elapsed_seconds: float | None,
) -> None:
    """#758 review finding 1: the window does not consult the charge clock at all.

    It used to. That clock freezes at drop (``_effective_now`` returns
    ``min(now, drop_monotonic)``), so a roast dropped inside the window held it
    below the bound forever and wrote whatever the probe reported whenever it
    recovered — a cooling-time reading stamped as "the room at charge". The
    window is now a wall-clock deadline, so no value of this field, however
    degenerate, changes the outcome: with the deadline still open every case
    retries without writing.

    ``-inf`` is parametrised deliberately: under the old charge-clock guard it
    was the ONE value the ``math.isfinite`` clause actually caught, and the
    parametrisation that named that clause omitted it, so the mutation removing
    the clause left the test passing.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service, _ = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    assert service.runner is not None
    snapshot = replace(
        service.runner.controller_snapshot(),
        charge_detected=True,
        charge_elapsed_seconds=charge_elapsed_seconds,
    )

    await service.runner._persist_ambient_if_charged(  # pyright: ignore[reportPrivateUsage]
        snapshot
    )

    assert calls == []
    assert service.runner._ambient_persisted is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_frozen_charge_clock_cannot_hold_the_window_open(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#758 review finding 1, the defect itself: a drop must not extend the window.

    Advancing only the runner's clock past the deadline must latch NULLs even
    though the charge clock is pinned below the bound, so a probe recovering
    later can never overwrite the run's charge ambient with a cooling reading.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(temperature_c=float("inf")),
        )
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service, _ = await _live_service(store, mcp=mcp, clock=clock, raw_state=raw_state)
    assert service.runner is not None
    # Charge clock frozen at 12 s, as a drop inside the window would leave it.
    frozen = replace(
        service.runner.controller_snapshot(),
        charge_detected=True,
        charge_elapsed_seconds=12.0,
    )
    await service.runner._persist_ambient_if_charged(frozen)  # pyright: ignore[reportPrivateUsage]
    assert calls == []

    clock.advance(api_module.AMBIENT_CAPTURE_GRACE_SECONDS + 1.0)
    await service.runner._persist_ambient_if_charged(frozen)  # pyright: ignore[reportPrivateUsage]

    assert calls == [(None, None, None)]
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_restart_restores_charge_clock_so_resumed_dtr_survives(store: RoastStore) -> None:
    """#235 end to end: a persisted charge instant restores the advisory DTR clock
    across a restart→operator-resume, so the advisor context's charge-referenced
    ``roast_elapsed_seconds`` (the DTR denominator, #219) is non-zero and correct
    instead of collapsing to ``0.0``. Advisory/display-only: recovery still enters
    operator_recovery_required and never auto-resumes heat/fan."""
    # A run that crashed mid pre-first-crack with the charge instant 120 s ago.
    await store.create_run(
        run_id="run-resume-dtr",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    charged_at = (datetime.now(UTC) - timedelta(seconds=120.0)).isoformat()
    await store.record_t0_detected_at("run-resume-dtr", charged_at)

    clock = FakeClock()
    mcp = FakeMCPClient()
    advisor = FakeAdvisor([], default_decision=_live_decision())
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        roaster=mcp,
        advisor=advisor,
        run_loop=False,
        clock=clock,
    )
    await service.recover_on_start()
    assert mcp.commands() == []  # restart never auto-resumes heat/fan
    recovered = await store.read_run("run-resume-dtr")
    assert recovered is not None
    assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED

    # Operator resumes into pre-first-crack; a turned-bean tick consults the advisor.
    await service.submit_operator_action(
        "run-resume-dtr",
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY,
            payload={"resume_to": "roasting_pre_first_crack"},
        ),
    )
    assert service.runner is not None
    # The resume tick re-engages the deterministic pre-FC policy (no advisor
    # consult — D35/#222). The advisor is consulted post-FC, so drive first crack
    # to DEVELOPMENT, then the development consult carries the restored DTR clock.
    mcp.frames = [_reading(bean=150.0, env=185.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # drains the resume action
    mcp.frames = [
        _reading(bean=185.0, env=200.0, bean_ror_c_per_min=4.0, first_crack_detected=True)
    ]
    await _tick(service, clock)  # first crack → DEVELOPMENT
    mcp.frames = [_reading(bean=186.0, env=201.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # development consult

    assert advisor.contexts, "advisor should be consulted after resume + FC"
    ctx = advisor.contexts[-1]
    # The DTR denominator is the restored charge clock — non-zero, ≈120 s, NOT 0.0.
    assert ctx.roast_elapsed_seconds > 0.0
    assert ctx.roast_elapsed_seconds == pytest.approx(120.0, abs=10.0)


@pytest.mark.asyncio
async def test_restart_inside_the_grace_window_does_not_reopen_the_capture(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#758 review finding 2: a restart must not re-open the grace window.

    Before the window existed, the capture always wrote on the first charged
    tick, so a CHARGED run was already captured by the time any restart could
    happen and ``recover()``'s seed had nothing to do. The window makes
    charged-but-uncaptured routinely reachable, and a fresh runner would restart
    its own deadline and write whatever the probe reports NOW as this run's
    charge ambient — a mid-roast reading in the column that means "the room at
    charge". A runner-local deadline cannot close that; only the seed can.

    Here the run charged (``t0_detected_at_utc`` set) with ``ambient_captured``
    still 0, and the probe is healthy again post-restart. Nothing may be written.
    """
    await store.create_run(
        run_id="run-window-restart",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    await store.record_t0_detected_at(
        "run-window-restart", t0_detected_at_utc="2026-08-11T09:00:00+00:00"
    )

    clock = FakeClock()
    mcp = FakeMCPClient()
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(
                temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
            ),
        )
    )
    calls = _capture_ambient_writes(store, monkeypatch)
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        run_loop=False,
        clock=clock,
        raw_state=raw_state,
    )
    await service.recover_on_start()
    assert service.runner is not None
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]

    snapshot = replace(service.runner.controller_snapshot(), charge_detected=True)
    await service.runner._persist_ambient_if_charged(  # pyright: ignore[reportPrivateUsage]
        snapshot
    )

    assert calls == []
    detail = await store.read_run("run-window-restart")
    assert detail is not None
    assert detail.ambient_temp_c is None


@pytest.mark.asyncio
async def test_restart_seeds_ambient_latch_so_resume_never_reclobbers(store: RoastStore) -> None:
    """#342: a run whose ambient triad was already captured pre-restart must NOT
    re-capture on the post-restart resume tick — ``recover()`` seeds
    ``_ambient_persisted`` from the persisted ``ambient_captured`` flag (mirroring
    the T0 clock restore), so a transient post-restart probe reading (e.g. now
    unavailable) can never overwrite the good pre-restart corpus value."""
    await store.create_run(
        run_id="run-resume-ambient",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )
    await store.set_ambient(
        "run-resume-ambient", temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
    )

    clock = FakeClock()
    mcp = FakeMCPClient()
    # Post-restart the probe now reads unavailable — must not clobber the capture.
    raw_state = _FakeRawState(
        _session_state(
            fc_status="pending",
            audio_running=False,
            ambient_status=_ambient_status(
                status="unavailable", temperature_c=None, humidity_percent=None, pressure_hpa=None
            ),
        )
    )
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        run_loop=False,
        clock=clock,
        raw_state=raw_state,
    )
    await service.recover_on_start()
    assert service.runner is not None
    # Seeded from the persisted ambient_captured flag.
    assert service.runner._ambient_persisted is True  # pyright: ignore[reportPrivateUsage]

    await service.submit_operator_action(
        "run-resume-ambient",
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY,
            payload={"resume_to": "roasting_pre_first_crack"},
        ),
    )
    mcp.frames = [_reading(bean=150.0, env=185.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)

    detail = await store.read_run("run-resume-ambient")
    assert detail is not None
    assert detail.ambient_temp_c == pytest.approx(28.49)  # unchanged — never re-captured


@pytest.mark.asyncio
async def test_recover_faulted_then_acknowledge_preserves_fault_reason(
    store: RoastStore,
) -> None:
    """#206 regression: ``RoastRunner.recover_faulted()`` must latch
    ``_captured_fault_reason`` BEFORE ``_flush_events()`` drains the FAULT event
    from the emitter buffer. Without the latch, the fault_reason column is None
    after recover→acknowledge because the buffer is empty when ``_handle_completion``
    fires on the first tick after ack.

    NB (#331): this exercises ``recover_faulted`` DIRECTLY rather than via
    ``recover_on_start`` — restart recovery now AUTO-FINALISES a stale faulted run
    (#331) instead of re-entering it operable, but ``recover_faulted`` itself (the
    in-session operable-faulted path) is unchanged and still owns the #206 latch, so
    its regression coverage lives here on the method directly."""
    await store.create_run(
        run_id="run-fault-reason",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    clock = FakeClock()
    mcp = FakeMCPClient()
    service = RoastService(
        store,
        roaster=mcp,
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=clock,
    )
    # Build the runner + re-enter operable-faulted directly (the in-session path).
    runner = await service._build_runner(  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
        "run-fault-reason", _profile()
    )
    assert runner is not None
    service.active_run_id = "run-fault-reason"
    await runner.recover_faulted(_profile())
    assert service.runner is not None

    # Acknowledge the fault → finalises on the next tick.
    accepted = await service.submit_operator_action(
        "run-fault-reason", OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert accepted.result == "accepted"
    finalized = await _tick(service, clock)
    assert finalized

    final = await store.read_run("run-fault-reason")
    assert final is not None
    assert final.outcome == "faulted"
    assert final.fault_reason is not None  # the latch fix — was None before #206 patch


@pytest.mark.asyncio
async def test_recover_on_start_noop_for_completed_run(store: RoastStore) -> None:
    await store.create_run(
        run_id="run-fin", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.COMPLETE
    )
    await store.complete_run(run_id="run-fin", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    service = RoastService(store, roaster=FakeMCPClient(), advisor=FakeAdvisor(), run_loop=False)
    await service.recover_on_start()
    assert service.runner is None  # nothing to recover


async def _drive_to_recovery(
    store: RoastStore,
) -> tuple[RoastService, FakeMCPClient, FakeClock, str]:
    """Start a live run and trip the pre-T0 overrun rule → recovery."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    # A pre-T0 bean overrun (>200 °C in preheating, no T0) → RECOVERY verdict.
    mcp.frames = [_reading(bean=205.0, env=210.0)]
    await _tick(service, clock)
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED  # type: ignore[union-attr]
    return service, mcp, clock, run_id


@pytest.mark.asyncio
async def test_acknowledge_recovery_resumes_to_payload_target(store: RoastStore) -> None:
    service, _mcp, clock, run_id = await _drive_to_recovery(store)
    result = await service.submit_operator_action(
        run_id,
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY, payload={"resume_to": "cooling"}
        ),
    )
    assert result.result == "accepted"
    await _tick(service, clock)
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.COOLING  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_acknowledge_recovery_rejects_missing_or_invalid_target(store: RoastStore) -> None:
    """Recovery resume needs a valid ``resume_to`` in the recovery transition
    row: missing, a non-phase string, and ``starting`` are all rejected — the
    run stays in recovery (never a guessed resume)."""
    service, _mcp, clock, run_id = await _drive_to_recovery(store)
    for payload in (None, {"resume_to": "garbage"}, {"resume_to": "starting"}):
        await service.submit_operator_action(
            run_id,
            OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_RECOVERY, payload=payload),
        )
        await _tick(service, clock)
        detail = await store.read_run(run_id)
        assert detail is not None
        assert detail.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_operator_mark_beans_added_executes_via_queue(store: RoastStore) -> None:
    """The manual-T0 fallback flows operator → queue → full safety → MCP."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)  # preheating
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.MARK_BEANS_ADDED)
    )
    assert result.result == "accepted"
    await _tick(service, clock)
    assert "mark_beans_added" in mcp.commands()


@pytest.mark.asyncio
async def test_operator_action_route_returns_410_on_terminal_run(
    client: AsyncClient, store: RoastStore
) -> None:
    await store.create_run(
        run_id="route-done",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.COMPLETE,
    )
    await store.complete_run(
        run_id="route-done", outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    response = await client.post(
        "/api/roasts/route-done/operator-actions", json={"action": "emergency_stop"}
    )
    assert response.status_code == 410


@pytest.mark.asyncio
async def test_recover_on_start_is_noop_in_api_only_mode(store: RoastStore) -> None:
    service = RoastService(store)  # no roaster wired
    await service.recover_on_start()
    assert service.runner is None


@pytest.mark.asyncio
async def test_recover_on_start_is_noop_for_idle_run(store: RoastStore) -> None:
    await store.create_run(
        run_id="run-idle", profile=_profile(), config=AppConfig(), agent_phase=RoastPhase.IDLE
    )
    service = RoastService(store, roaster=FakeMCPClient(), advisor=FakeAdvisor(), run_loop=False)
    await service.recover_on_start()
    assert service.runner is None


@pytest.mark.asyncio
async def test_run_loop_runs_in_background_and_shuts_down(store: RoastStore) -> None:
    """The production path: start_roast spawns the tick loop as a background
    task; shutdown cancels it cleanly."""
    config = AppConfig(
        controller=ControllerConfig(
            tick_interval_seconds=0.001, telemetry_log_interval_seconds=0.0001
        )
    )
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service = RoastService(
        store,
        config=config,
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        run_loop=True,  # real background loop
    )
    detail = await service.start_roast(_profile())

    # #531: poll for a persisted telemetry point rather than a fixed sleep.
    # The background loop's wall-clock pace varies under CI runner load, so a
    # tight `asyncio.sleep(0.03)` was a timing flake (the loop had not yet
    # ticked far enough to persist a row when asserted on #529's CI). Mirrors
    # the same poll-until-condition pattern already used for the fault/
    # finalisation waits below in this file — bounded so a genuine
    # never-persists regression still fails the test promptly.
    for _ in range(400):
        if await store.read_telemetry_points(detail.id):
            break
        await asyncio.sleep(0.005)
    await service.shutdown()
    points = await store.read_telemetry_points(detail.id)
    assert points, "the background loop persisted telemetry"


@pytest.mark.asyncio
async def test_recover_on_start_runs_loop_in_background(store: RoastStore) -> None:
    await store.create_run(
        run_id="run-rec",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    config = AppConfig(controller=ControllerConfig(tick_interval_seconds=0.001))
    service = RoastService(
        store,
        config=config,
        roaster=FakeMCPClient([_reading(150.0, 160.0)]),
        advisor=FakeAdvisor(),
        run_loop=True,
    )
    await service.recover_on_start()
    await asyncio.sleep(0.02)
    await service.shutdown()
    recovered = await store.read_run("run-rec")
    assert recovered is not None
    assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED


async def _drive_live_to_cooling(
    store: RoastStore, *, exporter_ok: bool
) -> tuple[RoastService, FakeMCPClient, FakeClock, str]:
    """Replay a live run preheat→T0→FC→drop into cooling, ready to stop."""
    clock = FakeClock()
    export_result = None
    if exporter_ok:
        from roastpilot_agent.mcp_client import ExportRoastLogResult

        export_result = ExportRoastLogResult(
            session_id="s",
            log_dir="d",
            jsonl_path="d/r.jsonl",
            csv_path="d/r.csv",
            summary_path="d/r.summary",
            ready=True,
            note="ok",
        )
    mcp = FakeMCPClient([_reading(178.0, 185.0)], export_result=export_result)
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    await _tick(service, clock)  # preheat
    mcp.frames = [_reading(95.0, 150.0, t0_detected=True)]
    for _ in range(3):
        await _tick(service, clock)
    mcp.frames = [_reading(196.0, 205.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)  # → development
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    mcp.frames = [_reading(205.0, 210.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)  # → cooling
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.COOLING  # type: ignore[union-attr]
    return service, mcp, clock, run_id


@pytest.mark.asyncio
async def test_telemetry_frame_surfaces_development_time_and_dtr(store: RoastStore) -> None:
    """#220: the live telemetry SSE frame carries server-authoritative development
    time + DTR. Pre-FC both are null (the operator readouts show '—'); once first
    crack transitions the run into development, the frame carries
    ``development_elapsed_seconds`` and a charge-referenced ``development_percent``
    (DTR as a share of the whole roast) the dashboard renders directly — no
    client-side derivation. Asserted on the real publish path."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, _run_id = await _live_service(store, mcp=mcp, clock=clock)
    queue = service.events.subscribe()

    def latest_telemetry() -> TelemetryEventData:
        frames = [
            f
            for f in _drain_queue(queue)
            if f.event is SseEventType.TELEMETRY and f.data.get("bean_temp_c") is not None
        ]
        assert frames, "no telemetry frame published"
        return TelemetryEventData.model_validate(frames[-1].data)

    # Pre-FC: development readouts are null.
    await _tick(service, clock)  # preheat
    pre_fc = latest_telemetry()
    assert pre_fc.development_elapsed_seconds is None
    assert pre_fc.development_percent is None

    # Charge (debounced T0), then first crack → development.
    mcp.frames = [_reading(95.0, 150.0, t0_detected=True)]
    for _ in range(3):
        await _tick(service, clock)
    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # Bean temps stay comfortably below the D88 ceiling-guard default
    # (196 °C, now on by default post-#495) — this test is about the
    # telemetry frame's development-time/DTR fields, not the guard, so a
    # deterministic auto-drop here would end the roast before either
    # assertion below runs.
    mcp.frames = [_reading(180.0, 205.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)  # → development (FC instant: dev elapsed == 0)
    # One more tick so development time has actually elapsed past the FC instant.
    mcp.frames = [_reading(185.0, 208.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)
    post_fc = latest_telemetry()
    assert post_fc.development_elapsed_seconds is not None
    assert post_fc.development_elapsed_seconds > 0.0
    assert post_fc.development_percent is not None
    # DTR is a percentage of the WHOLE (charge-referenced) roast: a sane share
    # bounded above by 100 (development can't exceed the charge clock it divides).
    assert 0.0 < post_fc.development_percent <= 100.0
    # The two readouts are DISTINCT values, not a ratio of each other.
    assert post_fc.development_percent != post_fc.development_elapsed_seconds


@pytest.mark.asyncio
async def test_telemetry_frame_carries_live_ambient_triad(store: RoastStore) -> None:
    """#464 (D86): the live SSE telemetry frame mirrors the MCP's latest
    ambient triad every tick — the same live/observability path as
    ``mic_status`` (#197), distinct from the one-time charge-instant capture
    (#342, D85, untouched by this)."""
    clock = FakeClock()
    mcp = FakeMCPClient(
        [
            _reading(
                178.0,
                185.0,
                ambient_temp_c=21.4,
                ambient_humidity_pct=45.2,
                ambient_pressure_hpa=1013.1,
            )
        ]
    )
    service, _run_id = await _live_service(store, mcp=mcp, clock=clock)
    queue = service.events.subscribe()

    def latest_telemetry() -> TelemetryEventData:
        frames = [
            f
            for f in _drain_queue(queue)
            if f.event is SseEventType.TELEMETRY and f.data.get("bean_temp_c") is not None
        ]
        assert frames, "no telemetry frame published"
        return TelemetryEventData.model_validate(frames[-1].data)

    await _tick(service, clock)
    frame = latest_telemetry()
    assert frame.ambient_temp_c == 21.4
    assert frame.ambient_humidity_pct == 45.2
    assert frame.ambient_pressure_hpa == 1013.1


@pytest.mark.asyncio
async def test_telemetry_frame_ambient_none_when_unavailable(store: RoastStore) -> None:
    """#464 (D86): no ambient reading this tick (disabled/unavailable MCP
    config, or an older MCP with no ambient support) → the SSE frame carries
    None for the whole triad, fail-soft — never a crash or a fault."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, _run_id = await _live_service(store, mcp=mcp, clock=clock)
    queue = service.events.subscribe()

    await _tick(service, clock)
    frames = [
        f
        for f in _drain_queue(queue)
        if f.event is SseEventType.TELEMETRY and f.data.get("bean_temp_c") is not None
    ]
    assert frames, "no telemetry frame published"
    frame = TelemetryEventData.model_validate(frames[-1].data)
    assert frame.ambient_temp_c is None
    assert frame.ambient_humidity_pct is None
    assert frame.ambient_pressure_hpa is None


def _drain_queue(queue: "asyncio.Queue[SseEvent]") -> list[SseEvent]:
    frames: list[SseEvent] = []
    while True:
        try:
            frames.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return frames


@pytest.mark.asyncio
async def test_queue_dispatch_routes_every_control_action(store: RoastStore) -> None:
    """Each operator action drains to its controller handler — exercised from
    preheating (matrix-rejected actions still dispatch; pause/resume toggle)."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    for action in (
        OperatorAction.MARK_FIRST_CRACK,
        OperatorAction.START_COOLING,
        OperatorAction.PAUSE_ADVISORY,
        OperatorAction.RESUME_ADVISORY,
    ):
        await service.submit_operator_action(run_id, OperatorActionRequest(action=action))
    await _tick(service, clock)
    # Still preheating (the writes were matrix-rejected; pause/resume don't move
    # phase) — the run survives the whole batch.
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.PREHEATING  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_emergency_stop_via_queue_faults_but_stays_operable(store: RoastStore) -> None:
    """#206: an e-stop faults the run but NO LONGER auto-finalises it. The faulted
    run stays operable — loop alive, run still active, completed_at null, not 410'd
    — so the operator can still engage/stop cooling on a physically-running machine
    before acknowledging the fault."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    await service.submit_operator_action(
        run_id,
        OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP, payload={"reason": "kill"}),
    )
    finalized = await _tick(service, clock)
    assert not finalized  # the fault does not finalise — the loop keeps running
    assert service.runner is not None and not service.runner.finalized
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.FAULTED
    assert detail.completed_at_utc is None  # still live until acknowledged
    assert detail.outcome is None
    assert "emergency_stop" in mcp.commands()
    # The run is still the active run, so an operator action is NOT 410'd.
    assert (await store.active_run()) is not None
    accepted = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    assert accepted.result == "accepted"


@pytest.mark.asyncio
async def test_acknowledge_recovery_racing_estop_does_not_strand_the_run(
    store: RoastStore,
) -> None:
    """An ``acknowledge_recovery`` queued alongside an e-stop in the same tick must
    not reset a faulting run to idle: the e-stop sorts first → faulted, and a
    recovery-ack from faulted is a no-op (it only resumes from
    operator_recovery_required). Post-#206 the fault no longer auto-finalises, so
    the run stays operable-faulted (active, completed_at null) — never an
    idle-but-uncompleted run that ``active_run`` would treat as still active."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_RECOVERY)
    )
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
    )
    finalized = await _tick(service, clock)  # e-stop sorts first → faulted; ack ignored
    assert not finalized  # the fault does not finalise (#206)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.FAULTED
    assert detail.completed_at_utc is None  # operable-faulted, not idle-stranded
    assert (await store.active_run()) is not None  # still the active run


@pytest.mark.asyncio
async def test_acknowledge_outside_recovery_is_recorded_failed(store: RoastStore) -> None:
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_RECOVERY)
    )
    await _tick(service, clock)
    # Not recovery/terminal → no resume, run stays preheating.
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.PREHEATING  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_completion_export_failure_still_completes(store: RoastStore) -> None:
    service, _mcp, clock, run_id = await _drive_live_to_cooling(store, exporter_ok=False)
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    finalized = await _tick(service, clock)
    assert finalized
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.outcome == "completed"
    assert detail.export_manifest is None  # export raised → no manifest, run still completes


@pytest.mark.asyncio
async def test_tick_once_after_finalize_is_idempotent(store: RoastStore) -> None:
    service, _mcp, clock, run_id = await _drive_live_to_cooling(store, exporter_ok=True)
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    assert await _tick(service, clock)  # finalizes
    assert await _tick(service, clock)  # already finalized → still True, no double complete


@pytest.mark.asyncio
async def test_event_flush_tolerates_a_failed_row(store: RoastStore) -> None:
    """A persistence error on one event never crashes the tick loop."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, _run_id = await _live_service(store, mcp=mcp, clock=clock)
    calls = {"n": 0}
    original = store.record_event

    async def flaky(**kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk blip")
        await original(**kwargs)  # type: ignore[arg-type]

    store.record_event = flaky  # type: ignore[method-assign]
    # A tick that emits at least one event must not raise despite the failure.
    await _tick(service, clock)
    store.record_event = original  # type: ignore[method-assign]
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_background_loop_stays_alive_on_fault_until_acknowledged(
    store: RoastStore,
) -> None:
    """#206: a hard fault no longer stops the background loop — it keeps ticking
    so the operator can still control cooling on a physically-running machine. The
    loop stops only once the operator acknowledges the fault, which finalises the
    run with outcome ``faulted``."""
    config = AppConfig(controller=ControllerConfig(tick_interval_seconds=0.001))
    mcp = FakeMCPClient([_reading(250.0, 200.0)])  # bean over the hard ceiling → e-stop
    service = RoastService(
        store,
        config=config,
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        run_loop=True,
    )
    detail = await service.start_roast(_profile())

    # Poll for the fault rather than a fixed sleep. The background loop's
    # wall-clock pace varies under CI load, so a tight `asyncio.sleep(0.03)` was a
    # timing flake (the loop had not yet reached the fault tick when asserted).
    # This asserts the SAME state — bounded so a genuine never-faults regression
    # still fails the test promptly.
    async def _agent_phase() -> RoastPhase | None:
        run = await store.read_run(detail.id)
        return None if run is None else run.agent_phase

    for _ in range(400):
        if await _agent_phase() is RoastPhase.FAULTED:
            break
        await asyncio.sleep(0.005)
    # The run has faulted but the loop is still alive (not finalised) — operable.
    faulting = await store.read_run(detail.id)
    assert faulting is not None
    assert faulting.agent_phase is RoastPhase.FAULTED
    assert faulting.completed_at_utc is None
    assert service.runner is not None and not service.runner.finalized
    # The operator acknowledges the fault → finalises (outcome faulted) + loop stops.
    await service.submit_operator_action(
        detail.id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    # Poll for finalisation (loop stop) instead of a fixed sleep. ``runner`` is
    # already narrowed non-None by the assert above.
    for _ in range(400):
        if service.runner.finalized:
            break
        await asyncio.sleep(0.005)
    await service.shutdown()
    finished = await store.read_run(detail.id)
    assert finished is not None
    assert finished.outcome == "faulted"
    assert finished.completed_at_utc is not None
    assert service.runner.finalized


@pytest.mark.asyncio
async def test_206_estop_in_preheating_then_cool_then_acknowledge(store: RoastStore) -> None:
    """The exact #206 scenario, end to end: an e-stop in preheating faults the run
    but does NOT finalise it (no power cycle); the operator then STOPS COOLING on
    the still-live faulted run (accepted, MCP write issued), then ACKNOWLEDGES the
    fault, which finalises the run (outcome ``faulted``) and stops the loop."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)  # preheating

    # 1) E-stop in preheating → fault, but the run stays live (not finalised).
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP, payload={"reason": "x"})
    )
    assert not await _tick(service, clock)
    faulted = await store.read_run(run_id)
    assert faulted is not None
    assert faulted.agent_phase is RoastPhase.FAULTED
    assert faulted.completed_at_utc is None
    assert "emergency_stop" in mcp.commands()

    # 2) STOP COOLING is accepted on the faulted run (no power cycle) and the MCP
    #    write is issued on the next tick — the run stays faulted, not completed.
    accepted = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    assert accepted.result == "accepted"
    assert not await _tick(service, clock)
    assert "stop_cooling" in mcp.commands()
    still_faulted = await store.read_run(run_id)
    assert still_faulted is not None
    assert still_faulted.agent_phase is RoastPhase.FAULTED
    assert still_faulted.completed_at_utc is None

    # 3) ACKNOWLEDGE the fault → finalises with outcome faulted and the loop stops.
    ack = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert ack.result == "accepted"
    assert await _tick(service, clock)  # finalises this tick → loop stops
    final = await store.read_run(run_id)
    assert final is not None
    assert final.outcome == "faulted"
    assert final.completed_at_utc is not None
    assert service.runner is not None and service.runner.finalized
    # Now terminal: a further operator action is 410'd.
    with pytest.raises(RoastRunGoneError):
        await service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
        )


@pytest.mark.asyncio
async def test_acknowledge_fault_is_audit_only_issues_no_mcp_write(store: RoastStore) -> None:
    """#117: acknowledging a fault is audit-only — it records the operator
    acknowledgement and finalises the run, but the ACK ITSELF issues NO roaster
    command and never re-triggers emergency_stop. The only MCP write in the trace
    is the e-stop that caused the fault; the ack tick adds nothing."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)  # preheating
    # E-stop → fault (the one and only roaster write up to the ack).
    await service.submit_operator_action(
        run_id,
        OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP, payload={"reason": "x"}),
    )
    assert not await _tick(service, clock)
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.FAULTED  # type: ignore[union-attr]
    commands_before_ack = list(mcp.commands())
    # The fault was caused by exactly one roaster write: the e-stop.
    assert commands_before_ack.count("emergency_stop") == 1

    # Acknowledge the fault → finalises, but issues no further MCP write.
    ack = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert ack.result == "accepted"
    assert await _tick(service, clock)  # finalises this tick
    # The command trace is UNCHANGED by the ack — no roaster write, no second e-stop.
    assert mcp.commands() == commands_before_ack
    final = await store.read_run(run_id)
    assert final is not None and final.outcome == "faulted"


@pytest.mark.asyncio
async def test_acknowledge_fault_clears_promptly_under_wedged_mcp(store: RoastStore) -> None:
    """#332: ``acknowledge_fault`` must finalise the run on the SAME tick it is
    drained, without blocking on a fresh escalation read, even when the MCP child
    is wedged (slow/failed reads).

    Repro of the roast-3 "slow to clear" report: a FAULT-latched run (not e-stop —
    consecutive MCP read failures, so ``_latched_verdict`` is FAULT) re-reads the
    child every latched tick in ``_maybe_escalate_while_latched`` looking for an
    upward escalation. That re-read runs BETWEEN the drain and the completion check
    in the same ``tick_once``, so on a wedged child it blocks ~``call_timeout_seconds``
    and delays finalisation AFTER the operator has already acknowledged. The fix
    (``note_fault_acknowledged``) skips the escalation re-read once the fault is
    acknowledged — the run is being torn down and heat is already off, so there is
    nothing to escalate into.

    Hardware-free + deterministic: a read-counting fake whose reads RAISE (a
    dead/failed child). The assertion is on the READ COUNT, not wall-clock — the
    acknowledge tick must NOT issue a fresh escalation read."""
    clock = FakeClock()
    log: list[str] = []
    # Every read raises → after max_consecutive_mcp_failures (3) the run FAULTs,
    # latched at the FAULT verdict (the escalation-re-read case, not e-stop). The
    # explicit log records every ``read`` so the assertion is on the read count.
    mcp = FakeMCPClient([RuntimeError("wedged child")], log=log)
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    # Tick to the fault: 3 consecutive failing reads cross the threshold.
    for _ in range(3):
        await _tick(service, clock)
    faulted = await store.read_run(run_id)
    assert faulted is not None and faulted.agent_phase is RoastPhase.FAULTED
    # A latched tick with NO operator action DOES re-read (the escalation probe) —
    # confirm the mechanism is real, so the test pins the path the fix narrows.
    reads_before = log.count("read")
    await _tick(service, clock)
    assert log.count("read") > reads_before, "latched tick should probe for escalation"
    # Now acknowledge + tick: the ack tick must FINALISE and must NOT issue another
    # escalation read (that read is the wedged-child latency the operator hit).
    ack = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert ack.result == "accepted"
    reads_at_ack = log.count("read")
    assert await _tick(service, clock)  # finalises on the ack tick
    assert log.count("read") == reads_at_ack, (
        "acknowledge tick must not block on a fresh escalation read"
    )
    final = await store.read_run(run_id)
    assert final is not None and final.outcome == "faulted"


@pytest.mark.asyncio
async def test_acknowledge_fault_outside_faulted_is_recorded_failed(store: RoastStore) -> None:
    """acknowledge_fault is meaningless outside FAULTED: from preheating the drain
    records a failed operator action and never finalises the run."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)  # preheating
    await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert not await _tick(service, clock)
    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is RoastPhase.PREHEATING
    assert detail.completed_at_utc is None


@pytest.mark.asyncio
async def test_mark_first_crack_via_queue_enters_development(store: RoastStore) -> None:
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    service, run_id = await _live_service(store, mcp=mcp, clock=clock)
    await _tick(service, clock)  # preheat
    mcp.frames = [_reading(95.0, 150.0, t0_detected=True)]
    for _ in range(3):
        await _tick(service, clock)  # → roasting_pre_first_crack
    mcp.frames = [_reading(190.0, 200.0, t0_detected=True)]  # no auto-FC
    await _tick(service, clock)  # prime the controller's last validated reading
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.MARK_FIRST_CRACK)
    )
    assert result.result == "accepted"
    await _tick(service, clock)
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.DEVELOPMENT  # type: ignore[union-attr]
    assert "mark_first_crack" in mcp.commands()
    first_crack = [
        event
        for event in (await service.timeline(run_id)).events
        if event.kind is RoastEventKind.FIRST_CRACK
    ]
    assert len(first_crack) == 1
    assert first_crack[0].payload == {
        "source": "operator",
        "bean_temp_c": 190.0,
    }


@pytest.mark.asyncio
async def test_start_cooling_via_queue_resumes_from_recovery(store: RoastStore) -> None:
    service, mcp, clock, run_id = await _drive_to_recovery(store)
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.START_COOLING)
    )
    assert result.result == "accepted"
    await _tick(service, clock)
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.COOLING  # type: ignore[union-attr]
    assert "start_cooling" in mcp.commands()


@pytest.mark.asyncio
async def test_lifespan_runs_recovery_on_startup_and_shutdown(store: RoastStore) -> None:
    """The app lifespan classifies a possibly-active persisted run into recovery
    on startup and stops the loop on shutdown."""
    await store.create_run(
        run_id="run-life",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.DEVELOPMENT,
    )
    service = RoastService(
        store,
        roaster=FakeMCPClient([_reading(150.0, 160.0)]),
        advisor=FakeAdvisor(),
        run_loop=False,
    )
    app = create_app(service)
    async with app.router.lifespan_context(app):
        recovered = await store.read_run("run-life")
        assert recovered is not None
        assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED


# --- #303: bean-profile library CRUD API ---


def _bean_input(**overrides: object) -> dict[str, object]:
    """A valid POST/PUT /api/bean-profiles body; override per test case."""
    base: dict[str, object] = {
        "name": "Colombia washed",
        "bean_origin": "Colombia",
        "default_bean_weight_grams": 250.0,
        "initial_heat_percent": 70,
        "initial_fan_percent": 40,
        "target_drop_temp_c": 205.0,
        "target_development_percent": 20.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_bean_profiles_empty_list(client: AsyncClient) -> None:
    response = await client.get("/api/bean-profiles")
    assert response.status_code == 200
    assert response.json() == {"profiles": []}


@pytest.mark.asyncio
async def test_create_bean_profile_returns_201_with_id_and_timestamps(
    client: AsyncClient,
) -> None:
    response = await client.post("/api/bean-profiles", json=_bean_input(name="Kenya AA"))
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["name"] == "Kenya AA"
    assert body["created_at"] == body["updated_at"]
    assert body["default_bean_weight_grams"] == 250.0
    # And it now lists.
    listed = await client.get("/api/bean-profiles")
    assert [p["id"] for p in listed.json()["profiles"]] == [body["id"]]


@pytest.mark.asyncio
async def test_create_bean_profile_claims_draft_header_once(
    client: AsyncClient, store: RoastStore
) -> None:
    """#588: the opaque metadata header is fail-closed and one-use."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider", model_slug="model", prompt_version="v1"
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=1,
        request_tokens=1,
        response_tokens=1,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft_from("https://vendor.example/bean"),
    )
    headers = {"X-RoastPilot-Draft-Attempt-Id": attempt_id}
    created = await client.post(
        "/api/bean-profiles", json=_bean_input(name="Edited"), headers=headers
    )
    assert created.status_code == 201
    replay = await client.post(
        "/api/bean-profiles", json=_bean_input(name="Edited"), headers=headers
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]
    mismatch = await client.post(
        "/api/bean-profiles", json=_bean_input(name="Replay"), headers=headers
    )
    assert mismatch.status_code == 409
    assert mismatch.headers["X-RoastPilot-Conflict-Code"] == "draft_attempt_already_claimed"
    unknown = await client.post(
        "/api/bean-profiles",
        json=_bean_input(name="Unknown attempt"),
        headers={"X-RoastPilot-Draft-Attempt-Id": "0" * 32},
    )
    assert unknown.status_code == 409
    assert "X-RoastPilot-Conflict-Code" not in unknown.headers
    malformed = await client.post(
        "/api/bean-profiles",
        json=_bean_input(name="Malformed"),
        headers={"X-RoastPilot-Draft-Attempt-Id": "not-an-id"},
    )
    assert malformed.status_code == 422


@pytest.mark.asyncio
async def test_create_bean_profile_validation_error_is_422(client: AsyncClient) -> None:
    response = await client.post(
        "/api/bean-profiles", json=_bean_input(default_bean_weight_grams=0)
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_bean_profile_edits_in_place(client: AsyncClient) -> None:
    created = (await client.post("/api/bean-profiles", json=_bean_input())).json()
    response = await client.put(
        f"/api/bean-profiles/{created['id']}",
        json=_bean_input(name="Edited", target_drop_temp_c=190.0),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["created_at"] == created["created_at"]
    # updated_at advances (monotonic over the real server clock); the strict
    # bump-was-actually-written assertion is pinned at the store layer with a
    # controlled clock (test_bean_profiles.test_update_bumps_updated_at_*).
    assert body["updated_at"] >= created["updated_at"]
    assert body["name"] == "Edited"
    assert body["target_drop_temp_c"] == 190.0


@pytest.mark.asyncio
async def test_update_unknown_bean_profile_is_404(client: AsyncClient) -> None:
    response = await client.put("/api/bean-profiles/nope", json=_bean_input())
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_archived_bean_profile_is_404(client: AsyncClient) -> None:
    """#304 (augment): editing an archived profile is a 404, not a phantom 200 —
    the store's rowcount guard surfaces as the not-found error."""
    created = (await client.post("/api/bean-profiles", json=_bean_input())).json()
    assert (await client.delete(f"/api/bean-profiles/{created['id']}")).status_code == 200
    response = await client.put(
        f"/api/bean-profiles/{created['id']}", json=_bean_input(name="Edited")
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_bean_profile_validation_error_is_422(client: AsyncClient) -> None:
    created = (await client.post("/api/bean-profiles", json=_bean_input())).json()
    response = await client.put(
        f"/api/bean-profiles/{created['id']}",
        json=_bean_input(initial_heat_percent=101),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_bean_profile_archives(client: AsyncClient) -> None:
    created = (await client.post("/api/bean-profiles", json=_bean_input())).json()
    response = await client.delete(f"/api/bean-profiles/{created['id']}")
    assert response.status_code == 200
    assert response.json() == {"id": created["id"], "result": "archived"}
    # Gone from the dropdown.
    listed = await client.get("/api/bean-profiles")
    assert listed.json() == {"profiles": []}


@pytest.mark.asyncio
async def test_delete_unknown_bean_profile_is_404(client: AsyncClient) -> None:
    response = await client.delete("/api/bean-profiles/never-existed")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_lifespan_seeds_bean_profiles_idempotently(store: RoastStore) -> None:
    """#303: the app lifespan seeds the built-in bean profiles (each present once
    after two startups — idempotent). Ordered by name, so Colombia < El Salvador
    < Ethiopia < Guatemala."""
    from roastpilot_agent.seed import (
        COLOMBIA_HUILA_ID,
        EL_SALVADOR_DIAMANTE_ID,
        ETHIOPIA_KOKE_ID,
        GUATEMALA_EL_DURAZNO_ID,
        SUMATRA_MANDHELING_ID,
    )

    service = RoastService(store)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        first = await store.list_bean_profiles()
    async with app.router.lifespan_context(app):
        second = await store.list_bean_profiles()
    # Name-ordered: Colombia < El Salvador < Ethiopia < Guatemala < Sumatra.
    expected = [
        COLOMBIA_HUILA_ID,
        EL_SALVADOR_DIAMANTE_ID,
        ETHIOPIA_KOKE_ID,
        GUATEMALA_EL_DURAZNO_ID,
        SUMATRA_MANDHELING_ID,
    ]
    assert [p.id for p in first] == expected
    assert [p.id for p in second] == expected  # not double-inserted


@pytest.mark.asyncio
async def test_seeded_ethiopia_profile_is_served_over_http(store: RoastStore) -> None:
    """#303: end-to-end seam — lifespan seed → GET /api/bean-profiles returns the
    Ethiopia Koke profile with its locked values over HTTP (the FE-visible path)."""
    from roastpilot_agent.seed import (
        COLOMBIA_HUILA_ID,
        EL_SALVADOR_DIAMANTE_ID,
        ETHIOPIA_KOKE_ID,
        GUATEMALA_EL_DURAZNO_ID,
        SUMATRA_MANDHELING_ID,
    )

    service = RoastService(store)
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        response = await http.get("/api/bean-profiles")
    assert response.status_code == 200
    profiles = response.json()["profiles"]
    by_id = {p["id"]: p for p in profiles}
    assert set(by_id) == {
        ETHIOPIA_KOKE_ID,
        COLOMBIA_HUILA_ID,
        GUATEMALA_EL_DURAZNO_ID,
        EL_SALVADOR_DIAMANTE_ID,
        SUMATRA_MANDHELING_ID,
    }
    koke = by_id[ETHIOPIA_KOKE_ID]
    assert koke["name"] == "Ethiopia Yirgacheffe Koke (Natural)"
    assert koke["processing"] == "natural"
    assert koke["altitude_m"] == 1885
    assert koke["default_bean_weight_grams"] == 250.0
    assert koke["target_drop_temp_c"] == 195.0  # roast-2 tuning: latest acceptable drop
    assert koke["target_development_percent"] == 13.0
    colombia = by_id[COLOMBIA_HUILA_ID]
    assert colombia["name"] == "Colombia Excelso Huila (Washed)"
    assert colombia["processing"] == "washed"
    assert colombia["altitude_m"] == 1600
    assert colombia["target_drop_temp_c"] == 195.0
    assert colombia["target_development_percent"] == 16.0


# ---------------------------------------------------------------------------
# POST /api/beans/draft-from-url (#573 phase 1 — add-bean-from-URL)
# ---------------------------------------------------------------------------
#
# The service delegates to ``bean_sourcing.draft_bean_profile_from_url`` (its
# own module-level function, tested exhaustively in test_bean_sourcing.py);
# these tests monkeypatch that reference on ``roastpilot_agent.api`` to a
# deterministic double, so no real fetch/LLM call happens here either — this
# level is only about the route's request/response/error-code wiring, and
# that this DRAFT endpoint creates no saved profile. The bounded, sanitized
# telemetry baseline is asserted separately below.


def _draft_from(url: str) -> BeanProfileDraft:
    return BeanProfileDraft(
        name="Kenya Kiambu AA (Washed)",
        bean_origin="Kenya",
        processing="washed",
        source_url=url,
        initial_heat_percent=100,
        initial_fan_percent=30,
        target_drop_temp_c=195.0,
        target_development_percent=15.0,
        default_bean_weight_grams=250.0,
        field_sources={"name": "on_page", "target_development_percent": "origin_estimated"},
        scouting_note="Scouting run — de-risked first-roast targets.",
    )


@pytest.mark.asyncio
async def test_recommend_from_catalogue_route_returns_read_only_ranked_products(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D121: catalogue selection returns products but creates no saved profile."""
    from roastpilot_agent.models import CatalogueRecommendationList

    async def fake_recommend(self: object, url: str) -> CatalogueRecommendationList:
        assert url == "https://vendor.example/collections/green"
        return CatalogueRecommendationList.model_validate(
            {
                "recommendations": [
                    {
                        "candidate_id": "candidate-01",
                        "product_url": "https://vendor.example/products/kenya",
                        "name": "Kenya Kiambu",
                        "country": "Kenya",
                        "processing": "washed",
                        "score": 3,
                        "reason_codes": [
                            "missing_country",
                            "missing_processing",
                            "novel_country_processing",
                        ],
                        "reasons": ["country", "process", "pair"],
                    }
                ],
                "discovered_count": 4,
                "extracted_count": 3,
            }
        )

    monkeypatch.setattr(RoastService, "recommend_beans_from_catalogue", fake_recommend)
    response = await client.post(
        "/api/beans/recommend-from-catalogue",
        json={"url": "https://vendor.example/collections/green"},
    )
    assert response.status_code == 200
    assert response.json()["recommendations"][0]["product_url"].endswith("/products/kenya")
    assert (await client.get("/api/bean-profiles")).json() == {"profiles": []}


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (BeanFetchError("catalogue fetch rejected"), 422, "catalogue fetch rejected"),
        (
            BeanExtractionUnavailableError("provider unavailable"),
            503,
            "catalogue extraction temporarily unavailable",
        ),
        (BeanExtractionError("no supported products"), 422, "no supported products"),
    ],
)
@pytest.mark.asyncio
async def test_recommend_from_catalogue_route_maps_domain_errors(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    """D121: catalogue route preserves input-versus-dependency status semantics."""

    async def fail_recommend(self: object, url: str) -> object:
        del self, url
        raise error

    monkeypatch.setattr(RoastService, "recommend_beans_from_catalogue", fail_recommend)
    response = await client.post(
        "/api/beans/recommend-from-catalogue",
        json={"url": "https://vendor.example/collections/green"},
    )
    assert response.status_code == expected_status
    assert expected_detail in response.json()["detail"]


@pytest.mark.asyncio
async def test_catalogue_service_records_nonclaimable_terminal_attempt(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D121: successful catalogue work is monitored without storing its products."""
    from roastpilot_agent.catalogue_recommendations import CatalogueRankingContext
    from roastpilot_agent.models import CatalogueRecommendationList

    async def fake_recommend(
        url: str,
        *,
        context: CatalogueRankingContext,
        advisor_config: object,
        sourcing_config: object,
        diagnostics: BeanSourcingDiagnostics,
    ) -> CatalogueRecommendationList:
        del advisor_config, sourcing_config
        assert url == "https://vendor.example/collections/green?secret=no-store"
        assert context.roster_countries == frozenset()
        diagnostics.request_tokens = 9
        diagnostics.response_tokens = 3
        diagnostics.usage_reported_requests = 1
        return CatalogueRecommendationList(
            recommendations=[], discovered_count=2, extracted_count=1
        )

    monkeypatch.setattr("roastpilot_agent.api.recommend_from_catalogue", fake_recommend)
    service = RoastService(store)
    result = await service.recommend_beans_from_catalogue(
        "https://vendor.example/collections/green?secret=no-store"
    )
    assert result.discovered_count == 2
    async with store.connection.execute("SELECT * FROM bean_sourcing_attempts") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["prompt_version"] == "v1-catalogue-v1"
    assert row["outcome"] == "success"
    assert (row["request_tokens"], row["response_tokens"]) == (9, 3)
    assert (row["catalogue_discovered_count"], row["catalogue_extracted_count"]) == (2, 1)
    assert row["draft_snapshot_json"] is None
    persisted = json.dumps(dict(row))
    assert "vendor.example" not in persisted
    assert "secret=no-store" not in persisted


@pytest.mark.asyncio
async def test_catalogue_ranking_context_uses_active_roster_and_completed_high_ratings(
    service: RoastService, store: RoastStore
) -> None:
    profile_input = BeanProfileInput.model_validate(
        _draft_from("https://vendor.example/products/guatemala").model_dump()
        | {"country": "Guatemala", "processing": "honey", "is_blend": False}
    )
    await store.create_bean_profile(profile_input)
    incomplete_profile = BeanProfileInput.model_validate(
        _draft_from("https://vendor.example/products/mystery").model_dump()
        | {"country": None, "processing": None, "is_blend": False}
    )
    await store.create_bean_profile(incomplete_profile)
    archived_profile = await store.create_bean_profile(
        BeanProfileInput.model_validate(
            _draft_from("https://vendor.example/products/archived").model_dump()
            | {"country": "Archived", "processing": "natural", "is_blend": False}
        )
    )
    await store.delete_bean_profile(archived_profile.id)

    async def add_rated_run(
        run_id: str,
        *,
        country: str,
        processing: Literal["washed", "natural", "honey"],
        outcome: Literal["completed", "faulted"],
        rating: Literal[3, 4, 5],
        excluded: bool = False,
    ) -> None:
        profile = _profile(name=run_id, origin=country).model_copy(
            update={"country": country, "processing": processing}
        )
        await store.create_run(
            run_id=run_id,
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await store.complete_run(
            run_id=run_id,
            outcome=outcome,
            agent_phase=RoastPhase.COMPLETE if outcome == "completed" else RoastPhase.FAULTED,
        )
        await store.set_operator_rating(run_id, rating=rating)
        if excluded:
            await store.set_run_excluded(run_id, excluded=True)

    await add_rated_run("high", country="Kenya", processing="washed", outcome="completed", rating=4)
    await add_rated_run(
        "low", country="Colombia", processing="natural", outcome="completed", rating=3
    )
    await add_rated_run(
        "faulted", country="Brazil", processing="natural", outcome="faulted", rating=5
    )
    await add_rated_run(
        "excluded",
        country="Ethiopia",
        processing="washed",
        outcome="completed",
        rating=5,
        excluded=True,
    )

    context = await service._catalogue_ranking_context()  # pyright: ignore[reportPrivateUsage]
    profile_axes, _ = await store.catalogue_ranking_axes()
    assert (None, None) in profile_axes
    assert ("Archived", "natural") not in profile_axes
    assert context.roster_countries == frozenset({"guatemala"})
    assert context.roster_processes == frozenset({"honey"})
    assert context.roster_pairs == frozenset({("guatemala", "honey")})
    assert context.rated_pairs == frozenset({("kenya", "washed")})


@pytest.mark.asyncio
async def test_catalogue_route_rejects_concurrent_and_oversized_requests(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from roastpilot_agent.models import CatalogueRecommendationList

    entered = asyncio.Event()
    release = asyncio.Event()
    # Isolate this cross-request test from the module singleton so binding its
    # waiter to pytest's per-test loop cannot affect the existing draft-route test.
    monkeypatch.setattr(api_module, "_draft_bean_from_url_semaphore", asyncio.Semaphore(1))

    async def slow_recommend(self: object, url: str) -> CatalogueRecommendationList:
        del self, url
        entered.set()
        await release.wait()
        return CatalogueRecommendationList(
            recommendations=[], discovered_count=1, extracted_count=1
        )

    monkeypatch.setattr(RoastService, "recommend_beans_from_catalogue", slow_recommend)
    first = asyncio.create_task(
        client.post(
            "/api/beans/recommend-from-catalogue",
            json={"url": "https://vendor.example/collections/green"},
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    concurrent = await client.post(
        "/api/beans/recommend-from-catalogue",
        json={"url": "https://vendor.example/collections/other"},
    )
    assert concurrent.status_code == 429
    release.set()
    assert (await first).status_code == 200

    oversized_url = await client.post(
        "/api/beans/recommend-from-catalogue", json={"url": "x" * 4097}
    )
    assert oversized_url.status_code == 422
    assert oversized_url.json()["detail"] == "URL exceeds 4096-character limit"
    oversized_body = await client.post(
        "/api/beans/recommend-from-catalogue",
        json={"url": "https://vendor.example/collections/green", "padding": "x" * 70_000},
    )
    assert oversized_body.status_code == 413
    malformed = await client.post(
        "/api/beans/recommend-from-catalogue", json={"url": "https://[::1"}
    )
    assert malformed.status_code == 422
    assert "invalid URL syntax" in malformed.json()["detail"]


def _empty_catalogue_result() -> object:
    """Build the minimal typed catalogue result used by service lifecycle tests."""
    from roastpilot_agent.models import CatalogueRecommendationList

    return CatalogueRecommendationList(recommendations=[], discovered_count=1, extracted_count=1)


async def _catalogue_attempt_outcomes(store: RoastStore) -> list[str]:
    """Return persisted catalogue outcomes in admission order for lifecycle tests."""
    async with store.connection.execute(
        "SELECT outcome FROM bean_sourcing_attempts"
        " WHERE prompt_version = ? ORDER BY started_at_utc, id",
        (CATALOGUE_EXTRACTION_PROMPT_VERSION,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [str(row["outcome"]) for row in rows]


@pytest.mark.asyncio
async def test_catalogue_service_rejects_before_context_work_when_roast_is_active(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("must not build catalogue context or call a provider while roasting")

    monkeypatch.setattr(RoastService, "_catalogue_ranking_context", fail_if_called)
    started = await client.post("/api/roasts", json=_profile().model_dump())
    assert started.status_code == 201

    response = await client.post(
        "/api/beans/recommend-from-catalogue",
        json={"url": "https://vendor.example/collections/green"},
    )
    assert response.status_code == 409
    assert "active" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_cancel_outcome", ["raise", "return", "error"])
async def test_catalogue_service_rechecks_active_roast_after_context_snapshot(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    post_cancel_outcome: Literal["raise", "return", "error"],
) -> None:
    from roastpilot_agent.catalogue_recommendations import CatalogueRankingContext

    context_started = asyncio.Event()
    release_context = asyncio.Event()

    async def delayed_context() -> CatalogueRankingContext:
        context_started.set()
        try:
            await release_context.wait()
        except asyncio.CancelledError:
            if post_cancel_outcome == "return":
                return CatalogueRankingContext(
                    roster_countries=frozenset(),
                    roster_processes=frozenset(),
                    roster_pairs=frozenset(),
                    rated_pairs=frozenset(),
                )
            if post_cancel_outcome == "error":
                raise RuntimeError("synthetic post-preemption context error") from None
            raise
        raise AssertionError("roast start failed to preempt context building")

    async def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("provider must not run after a roast wins the context-build race")

    monkeypatch.setattr(service, "_catalogue_ranking_context", delayed_context)
    monkeypatch.setattr("roastpilot_agent.api.recommend_from_catalogue", fail_if_called)
    recommendation = asyncio.create_task(
        service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    )
    await asyncio.wait_for(context_started.wait(), timeout=2.0)
    await service.start_roast(_profile())
    with pytest.raises(RoastRunConflictError, match="preempted"):
        await recommendation
    assert await _catalogue_attempt_outcomes(store) == ["preempted"]
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_cancel_outcome", ["raise", "return", "error"])
async def test_direct_catalogue_cancellation_during_context_wins(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    post_cancel_outcome: Literal["raise", "return", "error"],
) -> None:
    from roastpilot_agent.catalogue_recommendations import CatalogueRankingContext

    entered = asyncio.Event()

    async def delayed_context() -> CatalogueRankingContext:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if post_cancel_outcome == "return":
                return CatalogueRankingContext(
                    roster_countries=frozenset(),
                    roster_processes=frozenset(),
                    roster_pairs=frozenset(),
                    rated_pairs=frozenset(),
                )
            if post_cancel_outcome == "error":
                raise RuntimeError("synthetic post-cancel context error") from None
            raise
        raise AssertionError("direct cancellation failed to stop context building")

    monkeypatch.setattr(service, "_catalogue_ranking_context", delayed_context)
    recommendation = asyncio.create_task(
        service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    recommendation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recommendation
    assert await _catalogue_attempt_outcomes(store) == ["cancelled"]
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_catalogue_cancellation_while_waiting_to_swap_context_is_terminal(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.catalogue_recommendations import CatalogueRankingContext

    swap_lock_held = asyncio.Event()

    async def context_holding_swap_lock() -> CatalogueRankingContext:
        await service._start_lock.acquire()  # pyright: ignore[reportPrivateUsage]
        swap_lock_held.set()
        return CatalogueRankingContext(
            roster_countries=frozenset(),
            roster_processes=frozenset(),
            roster_pairs=frozenset(),
            rated_pairs=frozenset(),
        )

    monkeypatch.setattr(service, "_catalogue_ranking_context", context_holding_swap_lock)
    recommendation = asyncio.create_task(
        service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    )
    await asyncio.wait_for(swap_lock_held.wait(), timeout=2.0)
    await asyncio.sleep(0)
    try:
        recommendation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recommendation
    finally:
        service._start_lock.release()  # pyright: ignore[reportPrivateUsage]
    assert await _catalogue_attempt_outcomes(store) == ["cancelled"]
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_catalogue_active_run_recheck_error_is_terminal(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0
    original_active_run = store.active_run

    async def failing_second_active_run() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original_active_run()
        raise RuntimeError("synthetic active-run recheck failure")

    monkeypatch.setattr(store, "active_run", failing_second_active_run)
    with pytest.raises(RuntimeError, match="active-run recheck failure"):
        await service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    assert "catalogue recommendation active-run recheck failed" in caplog.text
    assert await _catalogue_attempt_outcomes(store) == ["provider_error"]
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_catalogue_context_error_propagates_logs_and_unregisters(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failed_context() -> object:
        raise RuntimeError("synthetic context read failure")

    monkeypatch.setattr(service, "_catalogue_ranking_context", failed_context)
    with pytest.raises(RuntimeError, match="context read failure"):
        await service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    assert "catalogue recommendation context snapshot failed" in caplog.text
    assert await _catalogue_attempt_outcomes(store) == ["provider_error"]
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_catalogue_rechecks_active_run_after_completed_context_task(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from roastpilot_agent.catalogue_recommendations import CatalogueRankingContext

    async def context_that_observes_concurrent_run() -> CatalogueRankingContext:
        await store.create_run(
            run_id="context-race",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        return CatalogueRankingContext(
            roster_countries=frozenset(),
            roster_processes=frozenset(),
            roster_pairs=frozenset(),
            rated_pairs=frozenset(),
        )

    async def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("provider must not run after an active run appears")

    monkeypatch.setattr(service, "_catalogue_ranking_context", context_that_observes_concurrent_run)
    monkeypatch.setattr("roastpilot_agent.api.recommend_from_catalogue", fail_if_called)
    with pytest.raises(RoastRunConflictError, match="active"):
        await service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    assert await _catalogue_attempt_outcomes(store) == ["preempted"]
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_cancel_outcome", ["raise", "return", "error"])
async def test_catalogue_service_preemption_is_reported_and_unregistered(
    service: RoastService,
    monkeypatch: pytest.MonkeyPatch,
    post_cancel_outcome: Literal["raise", "return", "error"],
) -> None:
    entered = asyncio.Event()

    async def slow_recommend(*args: object, **kwargs: object) -> object:
        del args, kwargs
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if post_cancel_outcome == "return":
                return _empty_catalogue_result()
            if post_cancel_outcome == "error":
                raise BeanFetchError("synthetic post-preemption error") from None
            raise

    monkeypatch.setattr("roastpilot_agent.api.recommend_from_catalogue", slow_recommend)
    task = asyncio.create_task(
        service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    async with service._start_lock:  # pyright: ignore[reportPrivateUsage]
        await service._preempt_bean_drafts_for_roast_start()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RoastRunConflictError, match="preempted"):
        await task
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_cancel_outcome", ["raise", "return", "error"])
async def test_direct_catalogue_cancellation_wins_when_inner_suppresses_it(
    service: RoastService,
    monkeypatch: pytest.MonkeyPatch,
    post_cancel_outcome: Literal["raise", "return", "error"],
) -> None:
    entered = asyncio.Event()

    async def uncooperative_recommend(*args: object, **kwargs: object) -> object:
        del args, kwargs
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if post_cancel_outcome == "error":
                raise BeanFetchError("synthetic post-cancel error") from None
            if post_cancel_outcome == "return":
                return _empty_catalogue_result()
            raise

    monkeypatch.setattr("roastpilot_agent.api.recommend_from_catalogue", uncooperative_recommend)
    task = asyncio.create_task(
        service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_catalogue_cancellation_during_attempt_admission_is_terminally_recorded(
    service: RoastService, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished: list[str] = []

    async def cancelled_admission(**kwargs: object) -> tuple[str, bool]:
        del kwargs
        return "attempt-id", True

    async def record_finish(attempt_id: str, **kwargs: object) -> None:
        assert attempt_id == "attempt-id"
        finished.append(cast(str, kwargs["outcome"]))

    monkeypatch.setattr(service, "_start_bean_attempt_bounded", cancelled_admission)
    monkeypatch.setattr(service, "_finish_bean_attempt_bounded", record_finish)
    with pytest.raises(asyncio.CancelledError):
        await service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    assert finished == ["cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_outcome"),
    [
        (BeanFetchError("fetch"), "fetch_error"),
        (BeanExtractionUnavailableError("provider"), "provider_error"),
        (BeanExtractionError("page"), "extraction_error"),
        (RuntimeError("unexpected"), "provider_error"),
    ],
)
async def test_catalogue_service_classifies_terminal_failures(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_outcome: str,
) -> None:
    async def failed(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise failure

    monkeypatch.setattr("roastpilot_agent.api.recommend_from_catalogue", failed)
    with pytest.raises(type(failure), match=str(failure)):
        await service.recommend_beans_from_catalogue("https://vendor.example/collections/green")
    async with store.connection.execute("SELECT outcome FROM bean_sourcing_attempts") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["outcome"] == expected_outcome


@pytest.mark.asyncio
async def test_draft_bean_from_url_happy_path(
    client: AsyncClient, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, object, object]] = []

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        calls.append((url, advisor_config, sourcing_config))
        assert isinstance(diagnostics, BeanSourcingDiagnostics)
        diagnostics.request_tokens = 101
        diagnostics.response_tokens = 22
        diagnostics.usage_reported_requests = 1
        return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    response = await client.post(
        "/api/beans/draft-from-url",
        json={"url": "https://vendor.example/products/kenya-kiambu"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Kenya Kiambu AA (Washed)"
    assert body["source_url"] == "https://vendor.example/products/kenya-kiambu"
    assert body["field_sources"]["target_development_percent"] == "origin_estimated"
    # #627: the empty form — a draft with no captured evidence quotes still
    # carries the key, as an empty object, not an omission.
    assert body["field_evidence"] == {}
    assert len(body["draft_attempt_id"]) == 32
    assert "id" not in body  # never persisted / never mints a library id
    # It reused the service's configured advisor + bean_sourcing config (BYOK).
    assert len(calls) == 1
    assert calls[0][0] == "https://vendor.example/products/kenya-kiambu"
    async with store.connection.execute(
        "SELECT provider, model_slug, prompt_version, outcome, request_tokens,"
        " response_tokens, usage_evidence FROM bean_sourcing_attempts WHERE id = ?",
        (body["draft_attempt_id"],),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["prompt_version"] == "v1"
    assert row["outcome"] == "success"
    assert (row["request_tokens"], row["response_tokens"], row["usage_evidence"]) == (
        101,
        22,
        "exact",
    )


@pytest.mark.asyncio
async def test_draft_timeout_records_unknown_usage_without_sensitive_error(
    client: AsyncClient, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: a provider timeout is countable but never misreported as zero spend."""

    async def timed_out(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        assert isinstance(diagnostics, BeanSourcingDiagnostics)
        diagnostics.timed_out_runs = 1
        raise BeanExtractionUnavailableError("provider secret raw error")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", timed_out)
    response = await client.post(
        "/api/beans/draft-from-url", json={"url": "https://vendor.example/private?key=x"}
    )
    assert response.status_code == 503
    async with store.connection.execute(
        "SELECT outcome, request_tokens, response_tokens, usage_evidence,"
        " timed_out_runs FROM bean_sourcing_attempts ORDER BY started_at_utc DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("provider_error", None, None, "unknown", 1)
    async with store.connection.execute(
        "SELECT * FROM bean_sourcing_attempts ORDER BY started_at_utc DESC LIMIT 1"
    ) as cursor:
        full_row = await cursor.fetchone()
    assert full_row is not None
    persisted = json.dumps(dict(full_row), sort_keys=True)
    assert "provider secret raw error" not in persisted
    assert "vendor.example" not in persisted
    assert "key=x" not in persisted


@pytest.mark.asyncio
async def test_malformed_provider_url_is_admitted_and_terminalized(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: malformed provider metadata cannot bypass attempt telemetry."""
    config = AppConfig()
    config = config.model_copy(
        update={"advisor": config.advisor.model_copy(update={"provider_base_url": "https://["})}
    )
    service = RoastService(store, config=config)

    async def unavailable(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        del url, advisor_config, sourcing_config, diagnostics
        raise BeanExtractionUnavailableError("malformed provider configuration")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", unavailable)
    with pytest.raises(BeanExtractionUnavailableError):
        await service.draft_bean_from_url("https://vendor.example/bean")
    async with store.connection.execute(
        "SELECT model_slug, outcome FROM bean_sourcing_attempts"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == (config.advisor.model_slug, "provider_error")


@pytest.mark.asyncio
async def test_draft_bean_from_url_response_carries_field_evidence(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#627: the endpoint returns ``field_evidence`` as-is — the populated
    form — verifying nothing on the response path (route/service/pydantic
    serialization) drops or filters it."""

    def _draft_with_evidence(url: str) -> BeanProfileDraft:
        return BeanProfileDraft(
            name="Kenya Kiambu AA (Washed)",
            bean_origin="Kenya",
            processing="washed",
            source_url=url,
            initial_heat_percent=100,
            initial_fan_percent=30,
            target_drop_temp_c=195.0,
            target_development_percent=15.0,
            default_bean_weight_grams=250.0,
            field_sources={"processing": "origin_estimated"},
            field_evidence={
                "processing": "Fully washed and dried on raised beds.",
                "altitude_m": "Grown at 1,900 masl.",
            },
            scouting_note="Scouting run — de-risked first-roast targets.",
        )

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        return _draft_with_evidence(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    response = await client.post(
        "/api/beans/draft-from-url",
        json={"url": "https://vendor.example/products/kenya-kiambu"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["field_evidence"] == {
        "processing": "Fully washed and dried on raised beds.",
        "altitude_m": "Grown at 1,900 masl.",
    }


@pytest.mark.asyncio
async def test_draft_bean_from_url_never_creates_a_saved_profile(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#573 human-in-the-loop safeguard: drafting must not touch the saved
    bean-profile library — it stays empty until the operator explicitly
    POSTs to /api/bean-profiles."""

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    await client.post(
        "/api/beans/draft-from-url", json={"url": "https://vendor.example/products/kenya"}
    )
    listed = await client.get("/api/bean-profiles")
    assert listed.json() == {"profiles": []}


@pytest.mark.asyncio
async def test_draft_bean_from_url_fetch_error_is_422(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        raise BeanFetchError(f"vendor page fetch failed for {url!r}")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    response = await client.post(
        "/api/beans/draft-from-url", json={"url": "https://vendor.example/products/down"}
    )
    assert response.status_code == 422
    assert "fetch failed" in response.json()["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@[bad?access_token=SECRET-QUERY-656#fragment-secret",
        "ftp://vendor.example/products/kenya?x='\"&access_token=SECRET-QUERY-656",
        "https:\n//user:SECRET-QUERY-656／password@vendor.example/path"
        "?access_token=SECRET-QUERY-656#fragment-secret",
        "https://user:SECRET-QUERY-656＠vendor.example/path"
        "?access_token=SECRET-QUERY-656#fragment-secret",
        "https://vendor.example？access_token=SECRET-QUERY-656/path",
        "https://vendor.example＃access_token=SECRET-QUERY-656/path",
        "https://SECRET-QUERY-656？x@vendor.example/path"
        "?access_token=SECRET-QUERY-656#fragment-secret",
        "https://SECRET-QUERY-656＃x@vendor.example/path"
        "?access_token=SECRET-QUERY-656#fragment-secret",
        "https://SECRET-QUERY-656？x＠vendor.example/path"
        "?access_token=SECRET-QUERY-656#fragment-secret",
        "https:/user:SECRET-QUERY-656@vendor.example/path?access_token=SECRET-QUERY-656",
        "https:user:SECRET-QUERY-656@vendor.example/path?access_token=SECRET-QUERY-656",
        *(
            f"{slashes}user:SECRET-QUERY-656@vendor.example/path?access_token=SECRET-QUERY-656"
            for slashes in ("///", "////")
        ),
        *(
            f"{prefix}//user:SECRET-QUERY-656{userinfo}vendor.example/path"
            "?access_token=SECRET-QUERY-656"
            for prefix, userinfo in (("https：", "@"), ("1https﹕", "＠"), ("1https:", "@"))
        ),
        " //user:SECRET-QUERY-656＠vendor.example/path"
        "?access_token=SECRET-QUERY-656#fragment-secret",
    ],
)
@pytest.mark.asyncio
async def test_draft_bean_from_url_parse_failure_detail_strips_sensitive_url_parts(
    client: AsyncClient, url: str
) -> None:
    """#656: malformed URL details are sanitized before repr/interpolation."""
    response = await client.post("/api/beans/draft-from-url", json={"url": url})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "SECRET-QUERY-656" not in detail
    assert "access_token" not in detail
    assert "user:password@" not in detail
    assert "fragment-secret" not in detail
    assert "?" not in detail


@pytest.mark.asyncio
async def test_draft_bean_from_url_bounds_body_before_framework_parsing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declared, chunked, and understated oversized bodies fail before the endpoint."""
    admission = mock.Mock()
    admission.acquire = mock.AsyncMock()
    provider = mock.AsyncMock(side_effect=AssertionError("must not reach provider"))
    redact = mock.Mock(side_effect=AssertionError("must not redact oversized body"))
    monkeypatch.setattr("roastpilot_agent.api._draft_bean_from_url_semaphore", admission)
    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", provider)
    monkeypatch.setattr("roastpilot_agent.api.redact_url_for_error", redact)
    too_large = b'{"url":"' + (b"x" * _DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES) + b'"}'
    valid_prefix = b'{"url":"https://vendor.example/bean"}'
    streamed_prefix = valid_prefix + (
        b" " * (_DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES - len(valid_prefix))
    )
    headers = {"content-type": "application/json"}
    expected = {"detail": "request body exceeds 65536-byte limit"}

    response = await client.post("/api/beans/draft-from-url", content=too_large, headers=headers)
    assert response.status_code == 413
    assert response.json() == expected

    async def streamed_body() -> AsyncIterator[bytes]:
        yield streamed_prefix
        yield b" "

    response = await client.post(
        "/api/beans/draft-from-url", content=streamed_body(), headers=headers
    )
    assert response.status_code == 413
    assert response.json() == expected
    response = await client.post(
        "/api/beans/draft-from-url",
        content=streamed_body(),
        headers={**headers, "content-length": "1"},
    )
    assert response.status_code == 413
    assert response.json() == expected
    admission.acquire.assert_not_awaited()
    provider.assert_not_awaited()
    redact.assert_not_called()

    exact = (b" " * (_DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES - 2)) + b"{}"
    assert (
        await client.post("/api/beans/draft-from-url", content=exact, headers=headers)
    ).status_code == 422
    assert (
        await client.post("/api/bean-profiles", content=too_large, headers=headers)
    ).status_code != 413


@pytest.mark.asyncio
async def test_draft_bean_from_url_bounds_url_before_redaction_or_admission(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized malformed URL is rejected in constant work with no secret echo."""
    admission = mock.Mock()
    admission.acquire = mock.AsyncMock()
    redact = mock.Mock(side_effect=AssertionError("must not redact oversized URL"))
    provider = mock.AsyncMock(side_effect=BeanFetchError("boundary URL reached provider"))
    monkeypatch.setattr("roastpilot_agent.api._draft_bean_from_url_semaphore", admission)
    monkeypatch.setattr("roastpilot_agent.api.redact_url_for_error", redact)
    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", provider)

    oversized = "https://user:SECRET-QUERY-656@[" + ("x" * 4096)
    response = await client.post("/api/beans/draft-from-url", json={"url": oversized})
    assert response.status_code == 422
    assert response.json() == {"detail": "URL exceeds 4096-character limit"}
    redact.assert_not_called()
    admission.acquire.assert_not_awaited()
    provider.assert_not_awaited()

    prefix = "https://vendor.example/"
    boundary = prefix + ("😀" * (4096 - len(prefix)))
    encoded = json.dumps({"url": boundary}).encode()
    assert len(encoded) < _DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES
    response = await client.post(
        "/api/beans/draft-from-url", content=encoded, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "boundary URL reached provider"
    admission.acquire.assert_awaited_once()
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_draft_bean_from_url_malformed_port_detail_strips_sensitive_text(
    client: AsyncClient,
) -> None:
    """#656: lazy port parse failures do not echo port or URL secrets."""
    url = "https://vendor.example:access_token=SECRET-QUERY-656/path?query_secret=SECRET-QUERY-656"
    response = await client.post("/api/beans/draft-from-url", json={"url": url})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "malformed port" in detail
    assert "SECRET-QUERY-656" not in detail
    assert "access_token" not in detail
    assert "query_secret" not in detail
    assert "Port could not be cast" not in detail


@pytest.mark.asyncio
async def test_draft_bean_from_url_extraction_error_is_422(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        raise BeanExtractionError("could not determine a bean name and origin from the page")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    response = await client.post(
        "/api/beans/draft-from-url", json={"url": "https://vendor.example/products/thin"}
    )
    assert response.status_code == 422
    assert "could not determine" in response.json()["detail"]


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("bean identity extraction exceeded the 45s deadline", id="provider_timeout"),
        pytest.param(
            "bean identity extraction provider error: upstream down", id="model_api_error"
        ),
        pytest.param(
            "bean identity extraction could not build its model: missing optional dependency",
            id="advisor_dependency_error",
        ),
        pytest.param(
            "bean identity extraction returned a malformed shape: exceeded max retries",
            id="unexpected_model_behavior",
        ),
    ],
)
@pytest.mark.asyncio
async def test_draft_bean_from_url_dependency_origin_extraction_error_is_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    """#613: every DEPENDENCY-origin cause ``_extract_bean_identity`` maps to
    ``BeanExtractionUnavailableError`` (provider timeout, ``ModelAPIError``,
    ``AdvisorDependencyError``/``AdvisorError``, and validation-retry
    exhaustion via ``UnexpectedModelBehavior`` — see
    ``test_bean_sourcing.py`` for the FunctionModel-driven proof that each
    real cause actually raises this subclass) surfaces as **503**, never the
    uniform 422 a bare ``BeanExtractionError`` used to get — the vendor page
    may have been fine; the failure is operational, not the caller's input."""

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        raise BeanExtractionUnavailableError(message)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    response = await client.post(
        "/api/beans/draft-from-url", json={"url": "https://vendor.example/products/outage"}
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "temporarily unavailable" in detail
    assert message in detail


@pytest.mark.asyncio
async def test_draft_bean_from_url_rejects_empty_url_body(client: AsyncClient) -> None:
    response = await client.post("/api/beans/draft-from-url", json={"url": ""})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_roast_service_draft_bean_from_url_uses_activated_config_without_reload(
    service: RoastService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#596/D107: drafting cannot activate newly saved config early.

    The service passes its currently activated ``AdvisorConfig`` and
    ``BeanSourcingConfig`` through to the separate BYOK extraction call.
    It does not reload saved config; that boundary remains ``start_roast``
    so an active roast's configuration stays frozen.
    """
    captured: dict[str, object] = {}

    def fail_if_reloaded() -> tuple[AppConfig, set[str]]:
        raise AssertionError("drafting must not reload saved configuration")

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        captured["url"] = url
        captured["advisor_config"] = advisor_config
        captured["sourcing_config"] = sourcing_config
        return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.load_app_config", fail_if_reloaded)
    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    draft = await service.draft_bean_from_url("https://vendor.example/products/kenya")
    assert isinstance(draft, BeanProfileDraft)
    assert captured["advisor_config"] is service._config.advisor  # pyright: ignore[reportPrivateUsage]
    assert (
        captured["sourcing_config"] is service._config.bean_sourcing  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.asyncio
async def test_roast_service_draft_bean_from_url_raises_when_a_roast_is_active(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#587 P1: uses the SAME persisted active-run signal ``start_roast``
    and ``health`` already use (``store.active_run()``) — checked BEFORE
    the fetch/LLM call, which must never be invoked."""

    async def fail_if_called(
        url: str, *, advisor_config: object, sourcing_config: object
    ) -> object:
        pytest.fail("must not fetch/extract while a roast is active")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fail_if_called)
    await store.create_run(
        run_id="run-active",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    with pytest.raises(RoastRunConflictError, match="active"):
        await service.draft_bean_from_url("https://vendor.example/products/kenya")


@pytest.mark.asyncio
async def test_cancellation_during_success_finalization_commits_then_propagates(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: shielding telemetry never consumes request cancellation."""
    finalizer_entered = asyncio.Event()
    release_finalizer = asyncio.Event()
    original_finish = store.finish_bean_sourcing_attempt

    async def delayed_finish(*args: object, **kwargs: object) -> None:
        finalizer_entered.set()
        await release_finalizer.wait()
        await original_finish(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    async def successful_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        return _draft_from(url)

    monkeypatch.setattr(store, "finish_bean_sourcing_attempt", delayed_finish)
    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", successful_draft)
    request_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(finalizer_entered.wait(), timeout=2.0)
    request_task.cancel()
    release_finalizer.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    async with store.connection.execute(
        "SELECT outcome FROM bean_sourcing_attempts ORDER BY started_at_utc DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["outcome"] == "success"


@pytest.mark.asyncio
async def test_cancellation_during_admission_terminalizes_then_propagates(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: cancellation cannot strand admission or an open transaction."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original_start = store.start_bean_sourcing_attempt

    async def delayed_start(**kwargs: object) -> str:
        entered.set()
        await release.wait()
        return await original_start(**kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(store, "start_bean_sourcing_attempt", delayed_start)
    request_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    request_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    async with store.connection.execute(
        "SELECT outcome FROM bean_sourcing_attempts ORDER BY started_at_utc DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_attempt_admission_timeout_cancels_owned_task(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: a wedged admission is cancelled and fails before remote work."""
    never = asyncio.Event()

    async def stalled_start(**kwargs: object) -> str:
        del kwargs
        await never.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(store, "start_bean_sourcing_attempt", stalled_start)
    monkeypatch.setattr(api_module, "_BEAN_DRAFT_FINALIZE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="timed out admitting"):
        await service._start_bean_attempt_bounded(  # pyright: ignore[reportPrivateUsage]
            provider="provider", model_slug="model", prompt_version="v1"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", [False, RuntimeError("temporary lock")])
async def test_attempt_lease_heartbeat_stops_or_retries(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    first_result: bool | RuntimeError,
) -> None:
    """#588: lease heartbeats stop on terminal rows and retry transient errors."""

    async def no_wait(_: float) -> None:
        return None

    outcomes: list[bool | RuntimeError] = (
        [first_result, False] if isinstance(first_result, RuntimeError) else [first_result]
    )

    async def renew(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        outcome = outcomes.pop(0)
        if isinstance(outcome, RuntimeError):
            raise outcome
        return outcome

    monkeypatch.setattr(api_module.asyncio, "sleep", no_wait)  # pyright: ignore[reportPrivateImportUsage]
    monkeypatch.setattr(store, "renew_bean_sourcing_attempt_lease", renew)
    await service._renew_bean_attempt_lease("attempt")  # pyright: ignore[reportPrivateUsage]
    assert not outcomes


@pytest.mark.asyncio
async def test_attempt_lease_heartbeat_propagates_cancellation(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: service shutdown cancellation is never swallowed by renewal."""

    async def no_wait(_: float) -> None:
        return None

    async def cancelled(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(api_module.asyncio, "sleep", no_wait)  # pyright: ignore[reportPrivateImportUsage]
    monkeypatch.setattr(store, "renew_bean_sourcing_attempt_lease", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service._renew_bean_attempt_lease(  # pyright: ignore[reportPrivateUsage]
            "attempt"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_deadline", [False, True])
async def test_attempt_finalization_timeout_cancels_owned_task(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, expired_deadline: bool
) -> None:
    """#588: both timeout guards cancel a wedged terminal ledger write."""
    never = asyncio.Event()

    async def stalled_finish(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await never.wait()

    monkeypatch.setattr(store, "finish_bean_sourcing_attempt", stalled_finish)
    monkeypatch.setattr(api_module, "_BEAN_DRAFT_FINALIZE_TIMEOUT_SECONDS", 0.01)
    if expired_deadline:
        # latency, deadline, then the first remaining-time calculation
        ticks = iter((0.0, 0.0, 1.0))
        service = RoastService(store, clock=lambda: next(ticks))
    else:
        service = RoastService(store)
    with pytest.raises(RuntimeError, match="timed out finalizing"):
        await service._finish_bean_attempt_bounded(  # pyright: ignore[reportPrivateUsage]
            "attempt",
            outcome="cancelled",
            started_monotonic=0.0,
            diagnostics=BeanSourcingDiagnostics(),
        )


@pytest.mark.asyncio
async def test_unexpected_provider_failure_records_partial_usage(
    client: AsyncClient, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: unexpected failures retain observed usage without claiming exactness."""

    async def failed(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        del url, advisor_config, sourcing_config
        assert isinstance(diagnostics, BeanSourcingDiagnostics)
        diagnostics.request_tokens = 17
        diagnostics.usage_reported_requests = 1
        diagnostics.usage_unreported_requests = 1
        raise RuntimeError("provider transport broke")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", failed)
    with pytest.raises(RuntimeError, match="provider transport broke"):
        await client.post("/api/beans/draft-from-url", json={"url": "https://vendor.example/bean"})
    async with store.connection.execute(
        "SELECT outcome, request_tokens, response_tokens, usage_evidence"
        " FROM bean_sourcing_attempts ORDER BY started_at_utc DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("provider_error", 17, 0, "partial")


@pytest.mark.asyncio
async def test_expiry_scheduler_does_not_lose_wakeup_after_empty_query(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: a success notification racing an empty query forces a re-query."""
    calls = 0
    queried_twice = asyncio.Event()

    async def racing_next_expiry() -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            service._bean_draft_expiry_wakeup.set()  # pyright: ignore[reportPrivateUsage]
        else:
            queried_twice.set()
        return None

    monkeypatch.setattr(store, "next_bean_sourcing_expiry", racing_next_expiry)
    service._ensure_bean_draft_expiry_task()  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(queried_twice.wait(), timeout=2.0)
    assert calls >= 2
    await service.shutdown()


@pytest.mark.asyncio
async def test_seed_starts_expiry_owner_and_ensure_wakes_existing_task(
    service: RoastService, store: RoastStore
) -> None:
    """#588: startup owns future expiry and later saves wake that same owner."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider", model_slug="model", prompt_version="v1"
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=1,
        request_tokens=1,
        response_tokens=1,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft_from("https://vendor.example/bean"),
    )
    await service.seed_bean_profiles()
    task = service._bean_draft_expiry_task  # pyright: ignore[reportPrivateUsage]
    assert task is not None and not task.done()
    service._bean_draft_expiry_wakeup.clear()  # pyright: ignore[reportPrivateUsage]
    service._ensure_bean_draft_expiry_task()  # pyright: ignore[reportPrivateUsage]
    assert service._bean_draft_expiry_wakeup.is_set()  # pyright: ignore[reportPrivateUsage]
    await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_clears_unclaimed_draft_snapshot(
    service: RoastService, store: RoastStore
) -> None:
    """#588: orderly shutdown removes a still-live correlation baseline."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider",
        model_slug="model",
        prompt_version="v1",
        owner_instance_id=service.instance_id,
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=1,
        request_tokens=1,
        response_tokens=1,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft_from("https://vendor.example/bean"),
    )

    await service.shutdown()

    async with store.connection.execute(
        "SELECT outcome, draft_snapshot_json, claim_expires_at_utc"
        " FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("success", None, None)


@pytest.mark.asyncio
async def test_expiry_scheduler_retries_transient_store_failure(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: one SQLite failure cannot permanently disable retention."""
    calls = 0
    retried = asyncio.Event()

    async def flaky_expiry(*, now_utc: str | None = None) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient SQLite failure")
        retried.set()
        return 0

    monkeypatch.setattr(store, "expire_bean_sourcing_drafts", flaky_expiry)
    monkeypatch.setattr(api_module, "_BEAN_DRAFT_EXPIRY_RETRY_SECONDS", 0.01)
    service._ensure_bean_draft_expiry_task()  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(retried.wait(), timeout=2.0)
    assert calls >= 2
    await service.shutdown()


@pytest.mark.asyncio
async def test_expiry_scheduler_caps_sleep_before_rechecking_wall_clock(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: long UTC expiries are rechecked after a bounded monotonic sleep."""
    future = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    monkeypatch.setattr(store, "next_bean_sourcing_expiry", mock.AsyncMock(return_value=future))
    observed_timeout: float | None = None

    async def capture_wait(awaitable: object, *, timeout: float) -> None:
        nonlocal observed_timeout
        observed_timeout = timeout
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        api_module.asyncio,  # pyright: ignore[reportPrivateImportUsage]
        "wait_for",
        capture_wait,
    )
    with pytest.raises(asyncio.CancelledError):
        await service._bean_draft_expiry_loop()  # pyright: ignore[reportPrivateUsage]
    assert observed_timeout == api_module._BEAN_DRAFT_EXPIRY_MAX_SLEEP_SECONDS  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_expiry_scheduler_clears_persisted_snapshot_at_boundary(
    service: RoastService, store: RoastStore
) -> None:
    """#588: the real service timer clears an expired persisted draft."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="openrouter",
        model_slug="test/model",
        prompt_version="v1",
    )
    completed = datetime.now(UTC) - timedelta(hours=24) + timedelta(milliseconds=50)
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=10,
        request_tokens=3,
        response_tokens=4,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft_from("https://vendor.example/products/kenya"),
        completed_at_utc=completed.isoformat(),
    )
    service._ensure_bean_draft_expiry_task()  # pyright: ignore[reportPrivateUsage]

    async def snapshot_is_cleared() -> bool:
        async with store.connection.execute(
            "SELECT draft_snapshot_json FROM bean_sourcing_attempts WHERE id = ?",
            (attempt_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None and row["draft_snapshot_json"] is None

    async with asyncio.timeout(2.0):
        while not await snapshot_is_cleared():
            await asyncio.sleep(0.01)
    await service.shutdown()


@pytest.mark.asyncio
async def test_slow_draft_bean_from_url_does_not_block_start_roast(
    service: RoastService, store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#657: roast start preempts and drains a stalled provider first."""
    draft_entered = asyncio.Event()
    cancellation_received = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        draft_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            await release_cleanup.wait()
            cleanup_finished.set()
            raise

    original_create_run = store.create_run

    async def create_run_after_draft_cleanup(
        *,
        run_id: str,
        profile: RoastProfile,
        config: AppConfig,
        agent_phase: RoastPhase,
        started_at_utc: str | None = None,
    ) -> None:
        assert cleanup_finished.is_set(), "draft cancellation must drain before persistence"
        await original_create_run(
            run_id=run_id,
            profile=profile,
            config=config,
            agent_phase=agent_phase,
            started_at_utc=started_at_utc,
        )

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", slow_draft)
    monkeypatch.setattr(store, "create_run", create_run_after_draft_cleanup)

    draft_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(draft_entered.wait(), timeout=2.0)

    start_task = asyncio.create_task(service.start_roast(_profile()))
    try:
        await asyncio.wait_for(cancellation_received.wait(), timeout=2.0)
        assert not start_task.done(), "start must briefly drain provider cancellation"
        release_cleanup.set()
        detail = await asyncio.wait_for(start_task, timeout=2.0)
        with pytest.raises(RoastRunConflictError, match="preempted by a roast-start attempt"):
            await draft_task
    finally:
        release_cleanup.set()
        for task in (draft_task, start_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert cleanup_finished.is_set()
    assert isinstance(detail, RoastDetail)
    assert detail.id


@pytest.mark.asyncio
async def test_draft_preemption_before_inner_task_first_runs(service: RoastService) -> None:
    """#657: a registered-but-not-yet-started pipeline is cancellable."""
    provider_started = False

    async def not_yet_started() -> BeanProfileDraft:
        nonlocal provider_started
        provider_started = True
        return _draft_from("https://vendor.example/products/kenya")

    inner_task = asyncio.create_task(not_yet_started())
    operation = api_module._BeanSourcingOperation(inner_task)  # pyright: ignore[reportPrivateUsage]
    service._bean_draft_operations[inner_task] = operation  # pyright: ignore[reportPrivateUsage]
    try:
        async with service._start_lock:  # pyright: ignore[reportPrivateUsage]
            await service._preempt_bean_drafts_for_roast_start()  # pyright: ignore[reportPrivateUsage]
        assert inner_task.cancelled()
        assert operation.preempted_by_start is True
        assert provider_started is False
    finally:
        service._bean_draft_operations.pop(inner_task, None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
@pytest.mark.parametrize("post_cancel_outcome", ["return", "error"])
async def test_direct_draft_request_cancellation_propagates_and_unregisters(
    service: RoastService,
    monkeypatch: pytest.MonkeyPatch,
    post_cancel_outcome: Literal["return", "error"],
) -> None:
    """#657: caller cancellation wins even if the inner task suppresses it."""
    draft_entered = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def slow_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        draft_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            if post_cancel_outcome == "error":
                raise BeanFetchError("synthetic error after cancellation") from None
            return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", slow_draft)
    draft_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(draft_entered.wait(), timeout=2.0)

    draft_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draft_task

    assert provider_cancelled.is_set()
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_external_cancellation_wins_race_with_roast_start(
    service: RoastService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#657: simultaneous caller cancellation remains cancellation, not 409."""
    draft_entered = asyncio.Event()
    start_preemption_received = asyncio.Event()
    hold_cleanup = asyncio.Event()

    async def slow_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        draft_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            start_preemption_received.set()
            await hold_cleanup.wait()
            raise

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", slow_draft)
    draft_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(draft_entered.wait(), timeout=2.0)
    start_task = asyncio.create_task(service.start_roast(_profile()))
    await asyncio.wait_for(start_preemption_received.wait(), timeout=2.0)

    draft_task.cancel()
    hold_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await draft_task
    detail = await asyncio.wait_for(start_task, timeout=2.0)

    assert isinstance(detail, RoastDetail)
    assert not service._bean_draft_operations  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_draft_preemption_message_is_honest_when_roast_start_fails(
    service: RoastService,
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#657: preemption reports an attempted start, not a guaranteed run."""
    draft_entered = asyncio.Event()

    async def slow_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        draft_entered.set()
        await asyncio.Event().wait()

    async def fail_create_run(**kwargs: object) -> None:
        raise RuntimeError("synthetic create failure")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", slow_draft)
    monkeypatch.setattr(store, "create_run", fail_create_run)
    draft_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(draft_entered.wait(), timeout=2.0)

    with pytest.raises(RuntimeError, match="synthetic create failure"):
        await service.start_roast(_profile())
    with pytest.raises(RoastRunConflictError) as error:
        await draft_task

    assert "roast-start attempt" in str(error.value)
    assert "if the start failed" in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("post_cancel_outcome", ["return", "error"])
async def test_uncooperative_draft_cleanup_cannot_indefinitely_block_start(
    service: RoastService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    post_cancel_outcome: Literal["return", "error"],
) -> None:
    """#657: preemption wins even when cleanup suppresses cancellation."""
    draft_entered = asyncio.Event()
    cancellation_received = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_cleanup(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        draft_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            await release_cleanup.wait()
            if post_cancel_outcome == "error":
                raise BeanFetchError("synthetic error after preemption") from None
            return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", slow_cleanup)
    monkeypatch.setattr(api_module, "_BEAN_DRAFT_CANCELLATION_GRACE_SECONDS", 0.0)
    draft_task = asyncio.create_task(
        service.draft_bean_from_url("https://vendor.example/products/kenya")
    )
    await asyncio.wait_for(draft_entered.wait(), timeout=2.0)

    detail = await asyncio.wait_for(service.start_roast(_profile()), timeout=2.0)
    await asyncio.wait_for(cancellation_received.wait(), timeout=2.0)
    assert "cancellation grace" in caplog.text
    assert isinstance(detail, RoastDetail)

    release_cleanup.set()
    with pytest.raises(RoastRunConflictError, match="preempted"):
        await draft_task


@pytest.mark.asyncio
async def test_draft_bean_from_url_returns_429_when_concurrency_exhausted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#587 fix 5: each draft-from-url call is a billable BYOK LLM request,
    so a module-level semaphore bounds how many requests can be ADMITTED at
    once. Fixed at 1, not 2 (#587 P2, round 6), so a SECOND concurrent
    request must fail fast with 429 (no queuing at all — the single slot is
    already taken); releasing the first then lets it complete normally.
    #657 keeps this draft-only admission control while letting roast starts
    proceed independently."""
    entered = 0
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        nonlocal entered
        entered += 1
        first_entered.set()
        await release.wait()
        return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)

    # The one admission slot is taken and executing inside fake_draft.
    task1 = asyncio.create_task(
        client.post("/api/beans/draft-from-url", json={"url": "https://vendor.example/products/1"})
    )
    await asyncio.wait_for(first_entered.wait(), timeout=2.0)

    # A second concurrent request exhausts the (single) semaphore slot
    # immediately -> 429, with no queuing.
    overflow_response = await client.post(
        "/api/beans/draft-from-url", json={"url": "https://vendor.example/products/overflow"}
    )
    assert overflow_response.status_code == 429

    release.set()
    result1 = await task1
    assert result1.status_code == 200
    assert entered == 1


@pytest.mark.asyncio
async def test_draft_bean_from_url_conflicts_when_a_roast_is_active(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#587 P1: bean extraction is a billable LLM call that can occupy the
    SAME backend an active roast's post-FC advisor needs for control advice
    — most acutely on a resource-constrained local provider (Ollama, which
    serialises inference) — starving those calls into
    ``ControllerConfig.advisory_timeout_seconds`` and the sustained-outage
    safety fallback after 3 consecutive failures. Must 409 BEFORE any
    fetch/LLM work: proven here by a ``draft_bean_profile_from_url`` double
    that ``pytest.fail``s if ever invoked."""

    async def fail_if_called(
        url: str, *, advisor_config: object, sourcing_config: object
    ) -> object:
        pytest.fail("must not fetch/extract while a roast is active")

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fail_if_called)

    started = await client.post("/api/roasts", json=_profile().model_dump())
    assert started.status_code == 201

    response = await client.post(
        "/api/beans/draft-from-url",
        json={"url": "https://vendor.example/products/kenya"},
    )
    assert response.status_code == 409
    assert "active" in response.json()["detail"]


@pytest.mark.asyncio
async def test_draft_bean_from_url_works_when_idle(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #587 P1 active-roast guard must not false-positive when idle —
    the ordinary happy path keeps working once the guard is added."""

    async def fake_draft(
        url: str, *, advisor_config: object, sourcing_config: object, diagnostics: object
    ) -> object:
        return _draft_from(url)

    monkeypatch.setattr("roastpilot_agent.api.draft_bean_profile_from_url", fake_draft)
    response = await client.post(
        "/api/beans/draft-from-url",
        json={"url": "https://vendor.example/products/kenya"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/config + PUT /api/config (D78 / #418 PR b)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _isolated_roastpilot_env() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Isolate ROASTPILOT_* env vars for config-route tests.

    ``load_app_config()`` calls ``_inject_saved_as_env()``, which writes saved
    values into ``os.environ`` as ``ROASTPILOT_*`` keys.  Those writes are not
    tracked by ``monkeypatch`` (the key did not exist before the call), so they
    bleed into subsequent tests (same env-pollution class fixed in PR-a via the
    sentinel pattern in test_config_store.py).

    Fix: snapshot the set of ROASTPILOT_* keys before the test, then delete any
    new keys added during the test in teardown.  ``monkeypatch`` restores the
    ``ROASTPILOT_CONFIG_FILE`` override set by the test itself; this fixture
    handles the injected non-file keys.
    """
    import os

    before: set[str] = {k for k in os.environ if k.startswith("ROASTPILOT_")}
    yield
    # Delete any ROASTPILOT_* keys that were absent before the test (injected
    # by _inject_saved_as_env during the test body).
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key not in before:
            del os.environ[key]


@pytest.mark.asyncio
async def test_get_config_returns_snapshot(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """GET /api/config returns a well-formed AppConfigSnapshot with all sections.

    Routes through a real create_app with no saved-config file on disk so all
    saved_value entries are None and effective_value equals the schema default.
    """
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    response = await client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    # All three top-level sections must be present.
    assert "controller" in body
    assert "advisor" in body
    assert "safety" in body
    # Each field must carry the four required keys.
    tick = body["controller"]["tick_interval_seconds"]
    assert tick["effective_value"] == 1.0
    assert tick["saved_value"] is None  # no file written yet
    assert tick["default"] == 1.0
    assert tick["read_only"] is True
    assert tick["env_overridden"] is False
    # Safety fields are present and read-only.
    max_bean = body["safety"]["max_bean_temp_c"]
    assert max_bean["read_only"] is True
    assert max_bean["effective_value"] == 230.0


@pytest.mark.asyncio
async def test_get_config_500_on_malformed_file(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """GET /api/config returns 500 when the saved-config YAML is malformed."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("{\n  broken: yaml: here\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))
    response = await client.get("/api/config")
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_get_config_500_on_schema_invalid_file(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """GET /api/config returns 500 when the saved-config is valid YAML but
    violates the schema (``ValidationError`` from ``load_app_config``).

    Mirrors ``test_get_config_500_on_malformed_file`` for the schema-invalid
    case — both must produce a clean HTTPException(500), not a raw traceback
    (claude-review low, PR #425).
    """
    cfg_path = tmp_path / "config.yaml"
    # Valid YAML but advisor.timeout_seconds must be a float — a non-numeric
    # string is a schema violation that raises ValidationError.
    cfg_path.write_text("advisor:\n  timeout_seconds: 'not_a_number'\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))
    response = await client.get("/api/config")
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_put_config_writes_and_returns_snapshot(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """PUT /api/config persists editable fields and returns the updated snapshot."""
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    body = {
        "controller": {
            "pre_first_crack_levers": {
                "heat_target_percent": 88,
                "fan_target_percent": 25,
            }
        },
        "advisor": {
            "model_slug": "openai/gpt-4o-mini",
        },
    }
    response = await client.put("/api/config", json=body)
    assert response.status_code == 200
    data = response.json()
    # Controller field reflects the written value.
    heat = data["controller"]["pre_fc_heat_target_percent"]
    assert heat["saved_value"] == 88
    assert heat["effective_value"] == 88
    assert heat["env_overridden"] is False
    # Advisor field reflects the written value.
    slug = data["advisor"]["model_slug"]
    assert slug["saved_value"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_put_config_422_on_out_of_range(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """PUT /api/config returns 422 when an edit field violates its schema bounds.

    heat_target_percent max is 100; sending 150 is a field-level Pydantic error
    that FastAPI turns into a 422 before the handler runs.
    """
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    body = {
        "controller": {
            "pre_first_crack_levers": {
                "heat_target_percent": 150,
            }
        }
    }
    response = await client.put("/api/config", json=body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_put_config_500_on_malformed_existing_file(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """PUT /api/config returns 500 when the existing saved file is malformed.

    persist_config_edit reads the existing file before merging; a malformed
    file raises ConfigFileError which the handler converts to 500.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(": bad: yaml\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_path))
    body = {"advisor": {"model_slug": "openai/gpt-4o"}}
    response = await client.put("/api/config", json=body)
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_put_config_no_safety_field_in_edit_type(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """PUT /api/config ignores unknown fields including any 'safety' key.

    AppConfigEdit has no 'safety' field.  FastAPI/Pydantic silently ignores
    extra keys by default (model_config default), so a body with a 'safety'
    key is accepted but the safety section is not written.
    """
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    body = {
        "safety": {"max_bean_temp_c": 999},  # must not reach the file
        "advisor": {"temperature": 0.5},
    }
    response = await client.put("/api/config", json=body)
    assert response.status_code == 200
    # Only the advisor change should appear; safety section absent from file.
    import yaml

    cfg_path = tmp_path / "config.yaml"
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "safety" not in (saved or {})
    temp = response.json()["advisor"]["temperature"]
    assert temp["saved_value"] == 0.5


@pytest.mark.asyncio
async def test_put_config_first_run_creates_parent_dir(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """PUT /api/config creates the parent directory when it does not yet exist.

    On first install ~/.roastpilot/ is absent; filelock raises OSError constructing
    the .lock file when the parent is missing.  The fix adds mkdir before FileLock
    (Codex P1 finding — first-run lock dir).
    """
    nested_cfg = tmp_path / "subdir" / "nested" / "config.yaml"
    assert not nested_cfg.parent.exists()
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(nested_cfg))

    body = {"advisor": {"model_slug": "openai/gpt-4o"}}
    response = await client.put("/api/config", json=body)
    assert response.status_code == 200
    assert nested_cfg.exists(), "config.yaml must be written after the parent dir is created"


@pytest.mark.asyncio
async def test_put_config_sequential_writes_no_stale_env_overridden(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """Two sequential PUTs of the same field: second value wins; env_overridden stays False.

    Without the snapshot/restore fix for _inject_saved_as_env, the first PUT
    left the injected env key in os.environ so the second PUT's load_app_config()
    saw it as a real env-var override and returned env_overridden=True — wrong.
    """
    import os

    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    # Remove any ROASTPILOT_ADVISOR__MODEL_SLUG env var (the test must not have one).
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG", raising=False)

    body1 = {"advisor": {"model_slug": "openai/gpt-4o"}}
    r1 = await client.put("/api/config", json=body1)
    assert r1.status_code == 200
    # After first PUT: field must not be env_overridden.
    slug1 = r1.json()["advisor"]["model_slug"]
    assert slug1["effective_value"] == "openai/gpt-4o"
    assert slug1["env_overridden"] is False

    body2 = {"advisor": {"model_slug": "openai/gpt-4.1"}}
    r2 = await client.put("/api/config", json=body2)
    assert r2.status_code == 200
    slug2 = r2.json()["advisor"]["model_slug"]
    assert slug2["effective_value"] == "openai/gpt-4.1"
    # Must still be False — the value came from the file, not a real env var.
    assert slug2["env_overridden"] is False
    _ = os  # imported above for possible future assertions


@pytest.mark.asyncio
async def test_get_config_nested_env_override_sets_env_overridden(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """A nested trim field overridden by env var appears env_overridden=True.

    Codex P2 finding: before the fix the nested trim fields all used
    env_var=None in build_config_snapshot(), so env_overridden was always
    False regardless of whether an env var was set.  After the fix the full
    nested env-var path is supplied so the badge is accurate.
    """
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    env_key = "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_HEAT_PERCENT"
    monkeypatch.setenv(env_key, "55")

    response = await client.get("/api/config")
    assert response.status_code == 200
    field = response.json()["controller"]["late_maillard_trim_heat_percent"]
    assert field["effective_value"] == 55
    assert field["env_overridden"] is True


@pytest.mark.asyncio
async def test_put_config_422_on_cross_field_violation(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
) -> None:
    """PUT /api/config returns 422 for a cross-field constraint violation.

    Field-level bounds (e.g. heat_target_percent > 100) are caught by FastAPI
    before the handler runs and already produce 422.  This test covers the
    case where the merged config violates a cross-field constraint — e.g.
    min_trim > max_trim — which can only be detected after merging with the
    existing saved file.  The handler now catches pydantic.ValidationError and
    converts it to 422 rather than letting it propagate as 500 (Codex P2).
    """
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))

    # First write a valid combination: min_trim=30, base_trim=40, max_trim=50.
    # The model validator requires min_trim <= base_trim <= max_trim.
    setup_body = {
        "controller": {
            "pre_first_crack_levers": {
                "late_maillard_trim": {
                    "min_trim": 30,
                    "base_trim": 40,
                    "max_trim": 50,
                }
            }
        }
    }
    r1 = await client.put("/api/config", json=setup_body)
    assert r1.status_code == 200

    # Now raise min_trim above the already-saved max_trim (50).  The merged config
    # will have min_trim=70 + max_trim=50, violating the cross-field invariant.
    # This is only detectable after merging with the existing saved file.
    bad_body = {
        "controller": {
            "pre_first_crack_levers": {
                "late_maillard_trim": {"min_trim": 70}  # > max_trim (50) → invalid
            }
        }
    }
    r2 = await client.put("/api/config", json=bad_body)
    assert r2.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "offending_key"),
    [
        ({"advisor": {"timeout_seconds": float("inf")}}, "timeout_seconds"),
        (
            {
                "controller": {
                    "pre_first_crack_levers": {"late_maillard_trim": {"k_ror": float("inf")}}
                }
            },
            "k_ror",
        ),
    ],
)
async def test_put_config_422_and_does_not_persist_non_finite_values(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_roastpilot_env: None,
    body: dict[str, object],
    offending_key: str,
) -> None:
    """T7: merged PUT config rejects and never writes non-finite values."""
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(config_path))

    response = await client.put(
        "/api/config",
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert not config_path.exists() or offending_key not in config_path.read_text(encoding="utf-8")


# --- GET /api/config/devices ---
#
# Both enumeration helpers use lazy imports (import inside the try block) so
# that a missing PortAudio native library or missing wheel never crashes server
# startup.  Tests patch _enumerate_serial and _enumerate_audio_inputs directly
# on the api module for the happy-path / fail-soft / empty variants (cleanest,
# hardware-free), and add one test that patches the sounddevice import itself to
# verify the ImportError path (the Playwright regression scenario).


class _FakePort:
    """Minimal stand-in for a ``serial.tools.list_ports_common.ListPortInfo``."""

    def __init__(self, device: str, description: str, hwid: str) -> None:
        self.device = device
        self.description = description
        self.hwid = hwid


@pytest.mark.asyncio
async def test_get_devices_happy_path(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/config/devices returns serial + audio results from fake enumerators.

    Patches _enumerate_serial and _enumerate_audio_inputs directly so the route
    handler, response model, and JSON serialisation are fully exercised while the
    test stays hardware-free and deterministic.
    """
    import roastpilot_agent.api as _api_mod

    def _fake_serial() -> tuple[list[_api_mod.DeviceOption], str | None]:
        return (
            [
                _api_mod.DeviceOption(
                    value="/dev/tty.usbmodem1401",
                    label="/dev/tty.usbmodem1401",
                    note="Hottop Roaster",
                )
            ],
            None,
        )

    def _fake_audio() -> tuple[list[_api_mod.DeviceOption], str | None]:
        return (
            [
                _api_mod.DeviceOption(
                    value="USB PnP Sound Device",
                    label="USB PnP Sound Device",
                    note="Input · 1 ch · 48,000 Hz",
                )
            ],
            None,
        )

    monkeypatch.setattr(_api_mod, "_enumerate_serial", _fake_serial)
    monkeypatch.setattr(_api_mod, "_enumerate_audio_inputs", _fake_audio)

    response = await client.get("/api/config/devices")
    assert response.status_code == 200
    body = response.json()

    # Both top-level keys must be present.
    assert "serial" in body
    assert "audio_input" in body
    assert body["serial_error"] is None
    assert body["audio_input_error"] is None

    # Serial: one port, correctly mapped.
    assert len(body["serial"]) == 1
    assert body["serial"][0]["value"] == "/dev/tty.usbmodem1401"
    assert body["serial"][0]["label"] == "/dev/tty.usbmodem1401"
    assert body["serial"][0]["note"] == "Hottop Roaster"

    # Audio: value must be the device NAME (the MCP matches audio_input_device
    # by name substring, not by PortAudio integer index).
    assert len(body["audio_input"]) == 1
    assert body["audio_input"][0]["value"] == "USB PnP Sound Device"
    assert body["audio_input"][0]["label"] == "USB PnP Sound Device"
    assert body["audio_input"][0]["note"] == "Input · 1 ch · 48,000 Hz"


@pytest.mark.asyncio
async def test_get_devices_serial_error_is_soft(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/config/devices returns 200 with serial_error when serial fails.

    Fail-soft: a serial enumeration error must surface as an empty serial list +
    a non-None serial_error string, while the audio source is unaffected.
    """
    import roastpilot_agent.api as _api_mod

    def _serial_denied() -> tuple[list[_api_mod.DeviceOption], str | None]:
        return [], "serial: permission denied"

    monkeypatch.setattr(_api_mod, "_enumerate_serial", _serial_denied)
    monkeypatch.setattr(
        _api_mod,
        "_enumerate_audio_inputs",
        lambda: (
            [
                _api_mod.DeviceOption(
                    value="Built-in Mic", label="Built-in Mic", note="Input · 1 ch · 44,100 Hz"
                )
            ],  # noqa: E501
            None,
        ),
    )

    response = await client.get("/api/config/devices")
    assert response.status_code == 200
    body = response.json()

    assert body["serial"] == []
    assert "serial: permission denied" in body["serial_error"]
    assert len(body["audio_input"]) == 1
    assert body["audio_input_error"] is None


@pytest.mark.asyncio
async def test_get_devices_audio_error_is_soft(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/config/devices returns 200 with audio_input_error when PortAudio fails.

    Fail-soft: a PortAudio failure must not prevent serial ports from being
    returned.  The audio source degrades to an empty list with an error string.
    """
    import roastpilot_agent.api as _api_mod

    monkeypatch.setattr(
        _api_mod,
        "_enumerate_serial",
        lambda: (
            [_api_mod.DeviceOption(value="/dev/ttyUSB0", label="/dev/ttyUSB0", note="USB Serial")],
            None,
        ),
    )

    def _audio_portaudio_gone() -> tuple[list[_api_mod.DeviceOption], str | None]:
        return [], "PortAudio not found"

    monkeypatch.setattr(_api_mod, "_enumerate_audio_inputs", _audio_portaudio_gone)

    response = await client.get("/api/config/devices")
    assert response.status_code == 200
    body = response.json()

    assert len(body["serial"]) == 1
    assert body["serial_error"] is None
    assert body["audio_input"] == []
    assert "PortAudio not found" in body["audio_input_error"]


@pytest.mark.asyncio
async def test_get_devices_both_empty_is_valid(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/config/devices returns 200 with empty lists when no devices found.

    An environment with no serial ports and no audio inputs (e.g. a minimal CI
    container) must return a valid 200 response with empty lists and no errors.
    """
    import roastpilot_agent.api as _api_mod

    def _no_serial() -> tuple[list[_api_mod.DeviceOption], str | None]:
        return [], None

    def _no_audio() -> tuple[list[_api_mod.DeviceOption], str | None]:
        return [], None

    monkeypatch.setattr(_api_mod, "_enumerate_serial", _no_serial)
    monkeypatch.setattr(_api_mod, "_enumerate_audio_inputs", _no_audio)

    response = await client.get("/api/config/devices")
    assert response.status_code == 200
    body = response.json()
    assert body["serial"] == []
    assert body["serial_error"] is None
    assert body["audio_input"] == []
    assert body["audio_input_error"] is None


def test_enumerate_audio_inputs_import_error_is_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_enumerate_audio_inputs degrades gracefully when sounddevice cannot be imported.

    This is the Playwright-regression scenario: the sounddevice wheel is present
    but PortAudio is not installed natively, so ``import sounddevice`` raises
    ``OSError`` or ``ImportError`` at import time.  The lazy-import inside
    _enumerate_audio_inputs must absorb that failure and return an empty list
    with a non-None error string — never propagate the ImportError to the caller.
    """
    import sys

    import roastpilot_agent.api as _api_mod

    # Drop the real module from the cache so the lazy import re-executes.
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    # None is the Python sentinel meaning "import was attempted and failed".
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # type: ignore[arg-type]

    devices, error = _api_mod._enumerate_audio_inputs()  # pyright: ignore[reportPrivateUsage]
    assert devices == []
    assert error is not None
    assert "sounddevice" in error.lower() or "import" in error.lower() or "None" in error


def test_enumerate_serial_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_enumerate_serial implementation: happy path and comports() error.

    Calls _enumerate_serial() directly (not via the HTTP route) so the
    lazy-import + comports() code path is exercised and counted toward
    patch coverage.  The sys.modules patch must cover all three levels of
    the ``serial.tools.list_ports`` namespace so that Python's import
    machinery resolves the lazy ``import serial.tools.list_ports as _lp``
    inside the function to our fake object.
    """
    import sys
    import types

    import roastpilot_agent.api as _api_mod

    # --- happy path: two ports, sorted by device path ---
    def _fake_comports_sorted() -> list[_FakePort]:
        # Deliberately out of order to exercise the sort.
        return [
            _FakePort("/dev/ttyUSB1", "Second port", "HWB"),
            _FakePort("/dev/ttyUSB0", "First port", "HWA"),
        ]

    fake_lp = types.ModuleType("serial.tools.list_ports")
    fake_lp.comports = _fake_comports_sorted  # type: ignore[attr-defined]
    fake_tools = types.ModuleType("serial.tools")
    fake_tools.list_ports = fake_lp  # type: ignore[attr-defined]
    fake_serial = types.ModuleType("serial")
    fake_serial.tools = fake_tools  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_lp)

    devices, error = _api_mod._enumerate_serial()  # pyright: ignore[reportPrivateUsage]
    assert error is None
    assert len(devices) == 2
    # Sorted by device path: ttyUSB0 before ttyUSB1.
    assert devices[0].value == "/dev/ttyUSB0"
    assert devices[0].note == "First port"
    assert devices[1].value == "/dev/ttyUSB1"

    # --- error path: comports() raises ---
    def _boom_comports() -> list[object]:
        raise OSError("no such file: /dev/ttyUSB0")

    fake_lp_err = types.ModuleType("serial.tools.list_ports")
    fake_lp_err.comports = _boom_comports  # type: ignore[attr-defined]
    fake_serial.tools.list_ports = fake_lp_err  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_lp_err)

    devices2, error2 = _api_mod._enumerate_serial()  # pyright: ignore[reportPrivateUsage]
    assert devices2 == []
    assert error2 is not None
    assert "no such file" in error2


def test_enumerate_audio_inputs_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_enumerate_audio_inputs implementation: happy path, filtering, and error.

    Calls _enumerate_audio_inputs() directly so the lazy-import + query_devices()
    code path is exercised for patch coverage.  Patches sounddevice in sys.modules.
    """
    import sys

    import roastpilot_agent.api as _api_mod

    # --- happy path: mix of input-capable and output-only devices ---
    class _FakeSDHappy:
        def query_devices(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "USB PnP Sound Device",
                    "max_input_channels": 1,
                    "max_output_channels": 0,
                    "default_samplerate": 48000.0,
                },
                # Output-only: must be filtered out.
                {
                    "name": "Built-in Output",
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                    "default_samplerate": 44100.0,
                },
                # Multi-channel input, non-integer samplerate edge-case.
                {
                    "name": "Aggregate Device",
                    "max_input_channels": 4,
                    "max_output_channels": 2,
                    "default_samplerate": "variable",  # triggers the '?' branch
                },
            ]

    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSDHappy())  # type: ignore[arg-type]

    devices, error = _api_mod._enumerate_audio_inputs()  # pyright: ignore[reportPrivateUsage]
    assert error is None
    # Output-only device filtered out; two input-capable devices remain.
    assert len(devices) == 2
    # value must be the device NAME so the Config UI saves the correct
    # audio_input_device substring (the MCP matches by name, not index).
    assert devices[0].value == "USB PnP Sound Device"
    assert devices[0].label == "USB PnP Sound Device"
    assert "48,000" in devices[0].note
    assert devices[1].value == "Aggregate Device"
    assert devices[1].label == "Aggregate Device"
    assert "?" in devices[1].note  # non-numeric samplerate

    # --- error path: query_devices() raises ---
    class _FakeSDError:
        def query_devices(self) -> list[object]:
            raise RuntimeError("PortAudio: invalid device")

    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSDError())  # type: ignore[arg-type]

    devices2, error2 = _api_mod._enumerate_audio_inputs()  # pyright: ignore[reportPrivateUsage]
    assert devices2 == []
    assert error2 is not None
    assert "PortAudio" in error2


# --- #567 Slice B: reference-curve retrieval plumbing ---


async def _seed_reference_run(
    store: RoastStore,
    run_id: str,
    *,
    profile: RoastProfile,
    rating: Literal[1, 2, 3, 4, 5] = 5,
) -> None:
    """Seed a completed, rated, same-bean reference run with development-phase
    telemetry so #567 retrieval can find + build it — mirrors
    ``tests/test_store.py``'s own Slice A seeding helpers
    (``_seed_completed_run`` / ``_record_row``), duplicated locally rather
    than imported since those are private to that test module."""
    await store.create_run(
        run_id=run_id, profile=profile, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await store.record_telemetry(
        run_id=run_id,
        tick=1,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=600.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=182.0, env_temp_c=195.0, bean_ror_c_per_min=7.0),
        development_percent=1.0,
        charge_elapsed_seconds=600.0,
    )
    await store.record_telemetry(
        run_id=run_id,
        tick=2,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=715.0,
        interval_seconds=0.0,
        telemetry=RoastTelemetry(bean_temp_c=190.0, env_temp_c=200.0, bean_ror_c_per_min=4.0),
        development_percent=15.1,
        charge_elapsed_seconds=715.0,
    )
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.set_operator_rating(run_id, rating=rating)


async def _drive_to_development_consult(
    service: RoastService, mcp: FakeMCPClient, clock: FakeClock
) -> None:
    """Drive a fresh (``_live_service``-started) run from preheating through
    charge → first crack → one development tick, so the advisor is
    consulted post-FC — the #567 reference fields only ever populate on a
    DEVELOPMENT-phase consult. Mirrors the tick sequence
    ``test_telemetry_frame_surfaces_development_time_and_dtr`` already
    exercises for the same charge → FC → development path."""
    await _tick(service, clock)  # preheat
    mcp.frames = [_reading(95.0, 150.0, t0_detected=True)]
    for _ in range(3):  # debounce (t0_debounce_ticks default 3)
        await _tick(service, clock)
    mcp.frames = [_reading(180.0, 205.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)  # → development (FC instant)
    mcp.frames = [_reading(185.0, 208.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)  # a real development consult


@pytest.mark.asyncio
async def test_reference_curve_flag_off_zero_retrieval_and_empty_context(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE invariant this slice must prove (default OFF is byte-for-byte
    today's behaviour): with ``reference_curve.enabled`` at its default
    ``False``, NO store reference read happens at all — a spy on
    ``RoastStore.load_reference_roast`` records zero calls — and the built
    ``AdvisorContext``'s reference fields stay empty/``None``. A qualifying
    same-bean reference is seeded FIRST, so a false pass ("nothing to find
    anyway") is impossible: if retrieval ran at all, it would find this run."""
    profile = _profile()
    await _seed_reference_run(store, "ref-flag-off", profile=profile)

    calls = 0
    original = RoastStore.load_reference_roast

    async def spy(self: RoastStore, *args: object, **kwargs: object) -> ReferenceRoast | None:
        nonlocal calls
        calls += 1
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RoastStore, "load_reference_roast", spy)

    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    advisor = FakeAdvisor([], default_decision=_live_decision())
    service, _run_id = await _live_service(
        store, mcp=mcp, clock=clock, profile=profile, advisor=advisor
    )
    assert service._config.controller.reference_curve.enabled is False  # pyright: ignore[reportPrivateUsage]

    await _drive_to_development_consult(service, mcp, clock)

    assert calls == 0, "flag off must perform zero store reference reads"
    assert advisor.contexts, "advisor should be consulted post-FC"
    ctx = advisor.contexts[-1]
    assert ctx.reference_curve == []
    assert ctx.reference_landmarks is None


@pytest.mark.asyncio
async def test_reference_curve_flag_on_populates_advisor_context(store: RoastStore) -> None:
    """Flag ON + a qualifying same-bean rated completed run: a fresh start
    retrieves it, and the built ``AdvisorContext`` carries the curve +
    landmarks."""
    profile = _profile()
    await _seed_reference_run(store, "ref-flag-on", profile=profile, rating=4)

    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    advisor = FakeAdvisor([], default_decision=_live_decision())
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            reference_curve=ReferenceCurve(enabled=True),
        )
    )
    service, _run_id = await _live_service(
        store, mcp=mcp, clock=clock, profile=profile, config=config, advisor=advisor
    )
    await _drive_to_development_consult(service, mcp, clock)

    assert advisor.contexts, "advisor should be consulted post-FC"
    ctx = advisor.contexts[-1]
    assert ctx.reference_curve != []
    assert ctx.reference_landmarks is not None
    assert ctx.reference_landmarks.operator_rating == 4
    assert ctx.reference_landmarks.drop_temp_c == pytest.approx(190.0)


@pytest.mark.asyncio
async def test_reference_curve_flag_on_retrieves_exactly_once_never_per_tick(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag-ON analogue of the flag-off invariant
    (``test_reference_curve_flag_off_zero_retrieval_and_empty_context``):
    retrieval happens ONCE, at fresh-start construction (``_build_runner``),
    never per tick and never per advisor consult. The prior
    controller-level test (``test_advisor_context_reference_fields_populated_
    from_cached_reference_roast``) only proves the controller reads back
    whatever it was constructed with — it can't catch a regression that
    moves retrieval INTO the tick path. This is the exact regression class
    the slice exists to prevent: a future refactor that re-derives the
    reference on every tick/consult would still pass every other test here
    but fail this one."""
    profile = _profile()
    await _seed_reference_run(store, "ref-flag-on-once", profile=profile, rating=4)

    calls = 0
    original = RoastStore.load_reference_roast

    async def spy(self: RoastStore, *args: object, **kwargs: object) -> ReferenceRoast | None:
        nonlocal calls
        calls += 1
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RoastStore, "load_reference_roast", spy)

    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    advisor = FakeAdvisor([], default_decision=_live_decision())
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            reference_curve=ReferenceCurve(enabled=True),
        )
    )
    service, _run_id = await _live_service(
        store, mcp=mcp, clock=clock, profile=profile, config=config, advisor=advisor
    )
    assert calls == 1, "retrieval must happen exactly once, at fresh-start construction"

    await _drive_to_development_consult(service, mcp, clock)
    assert calls == 1, "must not re-retrieve on the FC edge / first development consult"

    # Two more post-FC ticks — more consults (DEVELOPMENT's advisory heartbeat
    # is unthrottled by default) — must still leave the retrieval count at 1.
    mcp.frames = [_reading(186.0, 209.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)
    mcp.frames = [_reading(187.0, 210.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)

    assert len(advisor.contexts) >= 2, "at least two post-FC consults must have run"
    assert calls == 1, "retrieval must stay ONCE across multiple post-FC consults, never per tick"
    ctx = advisor.contexts[-1]
    assert ctx.reference_curve != []
    assert ctx.reference_landmarks is not None


@pytest.mark.asyncio
async def test_reference_curve_retrieval_fail_soft_never_blocks_start(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``load_reference_roast`` that raises must degrade to no reference —
    never block ``start_roast``, and never fault the run (design note §6.2:
    a must-fix, not an optional nicety, mirroring
    ``RoastRunner._persist_t0_if_charged``'s established fail-soft shape)."""
    profile = _profile()
    await _seed_reference_run(store, "ref-fail-soft", profile=profile)

    async def boom(self: RoastStore, *args: object, **kwargs: object) -> ReferenceRoast | None:
        raise RuntimeError("store hiccup")

    monkeypatch.setattr(RoastStore, "load_reference_roast", boom)

    clock = FakeClock()
    mcp = FakeMCPClient([_reading(178.0, 185.0)])
    advisor = FakeAdvisor([], default_decision=_live_decision())
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            reference_curve=ReferenceCurve(enabled=True),
        )
    )
    # start_roast (which retrieves the reference internally) must not raise.
    service, run_id = await _live_service(
        store, mcp=mcp, clock=clock, profile=profile, config=config, advisor=advisor
    )
    await _drive_to_development_consult(service, mcp, clock)

    detail = await store.read_run(run_id)
    assert detail is not None
    assert detail.agent_phase is not RoastPhase.FAULTED
    assert advisor.contexts
    ctx = advisor.contexts[-1]
    assert ctx.reference_curve == []
    assert ctx.reference_landmarks is None


@pytest.mark.asyncio
async def test_resume_reretrieves_reference_when_flag_on(store: RoastStore) -> None:
    """#567 design note §6.5: a resumed run rebuilds its context with a
    FRESHLY retrieved reference (flag on) at the point ``recover_on_start``
    builds a new controller — never persisted/restored across the restart
    boundary. Mirrors
    ``test_restart_restores_charge_clock_so_resumed_dtr_survives``'s own
    restart → operator-resume → post-FC-consult sequence."""
    profile = _profile()
    await _seed_reference_run(store, "ref-resume-on", profile=profile, rating=4)
    await store.create_run(
        run_id="run-resume-ref-on",
        profile=profile,
        config=AppConfig(
            controller=ControllerConfig(
                telemetry_log_interval_seconds=1.0,
                reference_curve=ReferenceCurve(enabled=True),
            )
        ),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )

    clock = FakeClock()
    mcp = FakeMCPClient()
    advisor = FakeAdvisor([], default_decision=_live_decision())
    # Process-current config is the opposite: next-roast OFF must not alter the
    # already-active run's frozen ON generation during restart recovery.
    config = AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=9.0))
    service = RoastService(
        store, config=config, roaster=mcp, advisor=advisor, run_loop=False, clock=clock
    )
    await service.recover_on_start()
    assert mcp.commands() == []  # restart never auto-resumes heat/fan
    recovered = await store.read_run("run-resume-ref-on")
    assert recovered is not None
    assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED

    await service.submit_operator_action(
        "run-resume-ref-on",
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY,
            payload={"resume_to": "roasting_pre_first_crack"},
        ),
    )
    assert service.runner is not None
    mcp.frames = [_reading(bean=150.0, env=185.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # drains the resume action
    mcp.frames = [
        _reading(bean=185.0, env=200.0, bean_ror_c_per_min=4.0, first_crack_detected=True)
    ]
    await _tick(service, clock)  # first crack → DEVELOPMENT
    mcp.frames = [_reading(bean=186.0, env=201.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # development consult

    assert advisor.contexts, "advisor should be consulted after resume + FC"
    ctx = advisor.contexts[-1]
    assert ctx.reference_curve != []
    assert ctx.reference_landmarks is not None
    assert ctx.reference_landmarks.operator_rating == 4


@pytest.mark.asyncio
async def test_resume_does_not_retrieve_reference_when_flag_off(store: RoastStore) -> None:
    """The flag-off mirror of ``test_resume_reretrieves_reference_when_flag_on``:
    the SAME restart → operator-resume → post-FC-consult sequence, with a
    qualifying same-bean reference seeded, but the default (flag off)
    config — the resumed run's context must stay empty/``None`` exactly
    like a fresh start's."""
    profile = _profile()
    await _seed_reference_run(store, "ref-resume-off", profile=profile, rating=4)
    await store.create_run(
        run_id="run-resume-ref-off",
        profile=profile,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
    )

    clock = FakeClock()
    mcp = FakeMCPClient()
    advisor = FakeAdvisor([], default_decision=_live_decision())
    # Process-current config is the opposite: next-roast ON must not alter the
    # already-active run's frozen OFF generation during restart recovery.
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=9.0,
            reference_curve=ReferenceCurve(enabled=True),
        )
    )
    service = RoastService(
        store, config=config, roaster=mcp, advisor=advisor, run_loop=False, clock=clock
    )
    await service.recover_on_start()
    assert mcp.commands() == []  # restart never auto-resumes heat/fan
    recovered = await store.read_run("run-resume-ref-off")
    assert recovered is not None
    assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED

    await service.submit_operator_action(
        "run-resume-ref-off",
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY,
            payload={"resume_to": "roasting_pre_first_crack"},
        ),
    )
    assert service.runner is not None
    mcp.frames = [_reading(bean=150.0, env=185.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # drains the resume action
    mcp.frames = [
        _reading(bean=185.0, env=200.0, bean_ror_c_per_min=4.0, first_crack_detected=True)
    ]
    await _tick(service, clock)  # first crack → DEVELOPMENT
    mcp.frames = [_reading(bean=186.0, env=201.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # development consult

    assert advisor.contexts, "advisor should be consulted after resume + FC"
    ctx = advisor.contexts[-1]
    assert ctx.reference_curve == []
    assert ctx.reference_landmarks is None


@pytest.mark.asyncio
async def test_retrieve_reference_for_excludes_the_run_being_started(store: RoastStore) -> None:
    """#567 design note §2: retrieval runs AFTER ``create_run``, so it only
    ever sees COMPLETED prior runs — never the run currently being started,
    even though that run's own frozen profile trivially matches its own
    origin_slug + charge weight."""
    profile = _profile()
    await store.create_run(
        run_id="the-run-being-started",
        profile=profile,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(reference_curve=ReferenceCurve(enabled=True))),
    )
    reference = await service._retrieve_reference_for(profile)  # pyright: ignore[reportPrivateUsage]
    assert reference is None


@pytest.mark.asyncio
async def test_retrieve_reference_for_skips_the_store_when_no_origin_slug(
    store: RoastStore,
) -> None:
    """A profile whose identity fields yield no usable slug
    (:func:`~roastpilot_agent.models.recording_origin_slug` returns
    ``None`` — punctuation-only ``name``/``bean_origin``, no ``country``)
    is treated the same as flag-off: no store call is attempted at all."""
    punctuation_only_profile = RoastProfile(
        name="---",
        bean_origin="...",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(reference_curve=ReferenceCurve(enabled=True))),
    )
    reference = await service._retrieve_reference_for(  # pyright: ignore[reportPrivateUsage]
        punctuation_only_profile
    )
    assert reference is None
