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
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

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
    RoastRunGoneError,
    RoastService,
    create_app,
    stream_events,
)
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import FirstCrackStatus, MCPServerProcess, RoastSessionState
from roastpilot_agent.models import (
    MicHealth,
    OperatorAction,
    OperatorActionRequest,
    RoastCommand,
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


@pytest.mark.asyncio
async def test_history_summary_projects_bean_identity(
    client: AsyncClient, store: RoastStore
) -> None:
    """#164: the history list projects country / species / blend marker from the
    frozen profile so the table can show them without opening each run."""
    profile = _profile().model_dump()
    profile.update({"country": "Colombia", "bean_species": "arabica", "is_blend": True})
    created = await client.post("/api/roasts", json=profile)
    run_id = created.json()["id"]
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)

    history = await client.get("/api/roasts")
    assert history.status_code == 200
    row = next(r for r in history.json()["runs"] if r["id"] == run_id)
    assert row["country"] == "Colombia"
    assert row["bean_species"] == "arabica"
    assert row["is_blend"] is True


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
    """Stands in for a Starlette Request — the SSE generator only calls
    ``is_disconnected()``."""

    def __init__(self, *, disconnected: bool = False) -> None:
        self._disconnected = disconnected

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
    store: RoastStore, *, mcp: FakeMCPClient, clock: FakeClock
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
    )
    detail = await service.start_roast(_profile())
    return service, detail.id


async def _tick(service: RoastService, clock: FakeClock) -> bool:
    assert service.runner is not None
    clock.advance(3.0)
    return await service.runner.tick_once()


def _session_state(*, fc_status: str, audio_running: bool) -> RoastSessionState:
    """A minimal valid ``RoastSessionState`` with the first-crack status set (#197)."""
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
    """#206: a FAULTED run with no ``completed_at`` (the post-#206 common case —
    a fault no longer auto-finalises) is NOT terminal, so an operator action is
    NOT 410'd: stop_cooling / start_cooling / emergency_stop / acknowledge_fault
    are all accepted, so a fault never strands a physically-running machine."""
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
async def test_recover_on_start_faulted_run_re_enters_operable_faulted(
    store: RoastStore,
) -> None:
    """#206 (fail-closed restart): a persisted FAULTED run with no ``completed_at``
    (the post-#206 common case) re-enters the operable-FAULTED state — NOT
    operator_recovery_required (whose row would permit resume-into-roasting). The
    loop is alive, no heat/fan/session write is issued, and the operator can still
    cool / e-stop / acknowledge. This is distinct from the active-roast →
    recovery path (tested separately)."""
    await store.create_run(
        run_id="run-faulted-crash",
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
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
    recovered = await store.read_run("run-faulted-crash")
    assert recovered is not None
    assert recovered.agent_phase is RoastPhase.FAULTED  # NOT operator_recovery_required
    assert recovered.completed_at_utc is None  # operable, awaiting acknowledgement
    assert service.active_run_id == "run-faulted-crash"
    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.FAULTED
    # No resume-into-roast: heat/fan are not auto-resumed, no MCP write on recovery.
    assert mcp.commands() == []
    snapshot = service.runner.controller_snapshot()
    assert (snapshot.current_heat, snapshot.current_fan) == (0, 0)
    # The operable-faulted state surfaces cooling/e-stop/ack, never resume-to-roast.
    enabled = enabled_operator_actions(RoastPhase.FAULTED)
    assert OperatorAction.STOP_COOLING in enabled
    assert OperatorAction.ACKNOWLEDGE_FAULT in enabled


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
    # The resume tick re-arms the settle window; a turned bean (RoR >= 0) releases
    # it at once, so the pre-first-crack PHASE_CHANGE advisory fires automatically.
    mcp.frames = [_reading(bean=150.0, env=185.0, bean_ror_c_per_min=4.0)]
    await _tick(service, clock)  # drains the resume action
    await _tick(service, clock)  # turned-bean consult

    assert advisor.contexts, "advisor should be consulted after resume"
    ctx = advisor.contexts[-1]
    # The DTR denominator is the restored charge clock — non-zero, ≈120 s, NOT 0.0.
    assert ctx.roast_elapsed_seconds > 0.0
    assert ctx.roast_elapsed_seconds == pytest.approx(120.0, abs=10.0)


@pytest.mark.asyncio
async def test_recover_faulted_then_acknowledge_preserves_fault_reason(
    store: RoastStore,
) -> None:
    """#206 regression: recover_faulted() must latch _captured_fault_reason BEFORE
    _flush_events() drains the FAULT event from the emitter buffer. Without the
    latch, the fault_reason column is None after restart→acknowledge because the
    buffer is empty when _handle_completion fires on the first tick after ack."""
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
    await service.recover_on_start()
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
    mcp.frames = [_reading(196.0, 205.0, t0_detected=True, first_crack_detected=True)]
    await _tick(service, clock)  # → development (FC instant: dev elapsed == 0)
    # One more tick so development time has actually elapsed past the FC instant.
    mcp.frames = [_reading(200.0, 208.0, t0_detected=True, first_crack_detected=True)]
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
    await asyncio.sleep(0.03)
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
    await asyncio.sleep(0.03)
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
