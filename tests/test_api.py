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

import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from roastpilot_agent import __version__
from roastpilot_agent.advisor import AdvisorContext, RoastDecision
from roastpilot_agent.api import EventBroadcaster, RoastService, create_app, stream_events
from roastpilot_agent.config import AppConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    OperatorAction,
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
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict
from roastpilot_agent.store import RoastStore


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
    assert queue.get_nowait().data == {"phase": "preheating"}


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
    assert frame["data"] == {"phase": "preheating"}
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
