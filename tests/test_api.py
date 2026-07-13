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
import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from roastpilot_agent import __version__
from roastpilot_agent.advisor import AdvisorContext, FakeAdvisor, RoastDecision
from roastpilot_agent.api import (
    EventBroadcaster,
    QueuedOperatorAction,
    RoastRunConflictError,
    RoastRunGoneError,
    RoastRunner,
    RoastService,
    _before_the_minute,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    _parse_last_event_id,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    create_app,
    stream_events,
)
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import (
    AmbientStatus,
    FirstCrackStatus,
    MCPServerProcess,
    RoastSessionState,
)
from roastpilot_agent.models import (
    ChargeWeightRequest,
    MicHealth,
    OperatorAction,
    OperatorActionRequest,
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
from roastpilot_agent.store import RoastStore
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
        )
    )
    event = queue.get_nowait()
    assert event.event is SseEventType.TELEMETRY
    assert event.data["agent_phase"] == "development"
    assert event.data["bean_temp_c"] == 200.0


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
) -> tuple[RoastService, str]:
    """Start a live (run_loop=False) service into preheating; return it + run id."""
    config = AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0))
    service = RoastService(
        store,
        config=config,
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_live_decision()),
        exporter=mcp,
        run_loop=False,
        clock=clock,
        raw_state=raw_state,
    )
    detail = await service.start_roast(_profile())
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
) -> AmbientStatus:
    """An ``AmbientStatus`` mirror instance (#342, D85).

    Defaults to an ``"ok"`` reading matching the hardware-validated live read
    (28.49 C / 38.6% / 1008.56 hPa). Pass ``status="unavailable"`` (or
    ``"disabled"``) with the numeric fields left ``None`` to exercise the
    fail-soft no-reading case."""
    return AmbientStatus(
        mode="yoctopuce",
        status=status,  # type: ignore[arg-type]  # parametrized over the Literal
        reason=reason,
        ambient_running=status == "ok",
        temperature_c=temperature_c,
        humidity_percent=humidity_percent,
        pressure_hpa=pressure_hpa,
        last_reading_monotonic_seconds=10.0 if status == "ok" else None,
    )


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
    runner = service._build_runner("run-fault-reason")  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
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
    await asyncio.sleep(0.03)  # let the background loop tick a few times
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
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.MARK_FIRST_CRACK)
    )
    assert result.result == "accepted"
    await _tick(service, clock)
    assert (await store.read_run(run_id)).agent_phase is RoastPhase.DEVELOPMENT  # type: ignore[union-attr]
    assert "mark_first_crack" in mcp.commands()


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
