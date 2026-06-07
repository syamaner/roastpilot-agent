"""E7 API tests (component plan §6, §8 ``test_api.py``).

E7-S1 covers the REST routes and their typed response models: health, roast
lifecycle start (incl. the 409 active-run guard), history/detail reads, the
downsampled telemetry series, the decision-trace timeline, the export-log
manifest + downloads, and operator rating. The operator action queue (S2)
and the SSE stream (S3) extend this suite. All hardware-free: an in-memory
controller is never started — routes read the temp SQLite store directly.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from roastpilot_agent import __version__
from roastpilot_agent.advisor import AdvisorContext, RoastDecision
from roastpilot_agent.api import RoastService, create_app
from roastpilot_agent.config import AppConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
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
async def test_log_manifest_returned_and_artifact_downloads(
    client: AsyncClient, store: RoastStore, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs-run-l"
    log_dir.mkdir()
    await _complete_with_manifest(store, "run-l", log_dir)

    manifest = await client.get("/api/roasts/run-l/log")
    assert manifest.status_code == 200
    assert manifest.json()["ready"] is True
    assert manifest.json()["jsonl_path"].endswith("roast.jsonl")

    download = await client.get("/api/roasts/run-l/log/jsonl")
    assert download.status_code == 200
    assert download.text == "contents of roast.jsonl"


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
