"""FastAPI application: REST + SSE + static SPA mount (component plan §6).

E7 builds the full REST + SSE surface the SPA renders from — one backend
authority: the SPA never calls MCP, it renders from these routes, the typed
SSE event stream, and snapshots. The deterministic controller and the MCP
child that *drive* a live roast are wired into this surface by the E9 vertical
slice; E7 establishes the API contract, the operator action queue, and the
SSE event vocabulary those depend on.

E7-S1 (this module's first slice) covers the REST routes and their typed
response models: health, roast lifecycle start, history/detail reads, the
downsampled telemetry series, the decision-trace timeline, the export-log
manifest + downloads, and operator rating. The operator action queue (S2)
and the SSE stream (S3) extend :class:`RoastService` in place.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from roastpilot_agent import __version__
from roastpilot_agent.config import AppConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    HealthResponse,
    LogManifest,
    MCPChildStatus,
    OperatorAction,
    OperatorActionRequest,
    OperatorActionResult,
    OperatorRatingRequest,
    RoastCommand,
    RoastDetail,
    RoastHistory,
    RoastPhase,
    RoastProfile,
    RoastTimeline,
    TelemetrySeries,
)
from roastpilot_agent.safety import SafetyPolicy, SafetyVerdict
from roastpilot_agent.store import RoastStore

#: Operator actions that resolve to an MCP write command, mapped to the
#: command×phase matrix entry used for the queue's phase-validity pre-check
#: (E7-S2). The control actions — ``pause_advisory`` / ``resume_advisory`` /
#: ``acknowledge_recovery`` — issue no MCP write and so have no matrix entry;
#: they are accepted at the queue and validated by the controller on drain.
_ACTION_COMMAND: dict[OperatorAction, RoastCommand] = {
    OperatorAction.MARK_BEANS_ADDED: RoastCommand.MARK_BEANS_ADDED,
    OperatorAction.MARK_FIRST_CRACK: RoastCommand.MARK_FIRST_CRACK,
    OperatorAction.DROP_BEANS: RoastCommand.DROP_BEANS,
    OperatorAction.START_COOLING: RoastCommand.START_COOLING,
    OperatorAction.STOP_COOLING: RoastCommand.STOP_COOLING,
    OperatorAction.EMERGENCY_STOP: RoastCommand.EMERGENCY_STOP,
}


class QueuedOperatorAction(BaseModel):
    """One operator action placed on the controller queue (E7-S2).

    The transport item the controller drains each tick (E9): it carries the
    target run, the typed action, and the operator-supplied payload. The
    queue never bypasses safety — the controller re-runs the full policy
    (rate limits, bounds, phase, drop eligibility) before any MCP write; the
    queue's phase pre-check only gives the operator immediate feedback."""

    run_id: str
    action: OperatorAction
    payload: dict[str, Any] | None = None


#: A downloadable export artifact name (plan §6 log manifest), validated
#: against the known artifact set in :meth:`RoastService.log_artifact_path`.
LogArtifactName = str


class RoastRunConflictError(Exception):
    """A request conflicts with the current run state (maps to HTTP 409):
    starting a roast while one is active, or rating an in-progress run."""


class RoastRunNotFoundError(Exception):
    """No run (or no requested artifact) matches the id (maps to HTTP 404)."""


class RoastService:
    """Backend authority behind the REST + SSE surface (component plan §6).

    Owns the persistence store, the active-run pointer, and — once wired —
    the MCP child handle used for the health route's liveness field. The
    controller tick loop and live MCP session that advance a roast are
    attached by the E9 vertical slice; the methods here are the seams it
    drives and the read projections the SPA renders from.
    """

    def __init__(
        self,
        store: RoastStore,
        *,
        config: AppConfig | None = None,
        mcp: MCPServerProcess | None = None,
    ) -> None:
        self._store = store
        self._config = config or AppConfig()
        self._mcp = mcp
        self.active_run_id: str | None = None
        # Serializes the active_run check + create_run insert: without a UNIQUE
        # "at most one open run" schema constraint, two concurrent POST /roasts
        # could both read no-active-run and both insert. One operator + a 1 s
        # tick makes this rare, but the at-most-one-active invariant is held
        # here, not left to chance.
        self._start_lock = asyncio.Lock()
        # The phase-validity pre-check shares the run's configured safety
        # limits, so the queue's verdict matches the controller's on drain.
        self._safety = SafetyPolicy(self._config.safety)
        #: The controller action queue (plan §6: action → operator_actions row
        #: → controller queue → safety → MCP). The API enqueues phase-valid
        #: actions; the controller tick loop drains and executes them (E9).
        self.operator_queue: asyncio.Queue[QueuedOperatorAction] = asyncio.Queue()

    def mcp_child_status(self) -> MCPChildStatus:
        """Liveness of the coffee-roaster-mcp child for the health route.

        ``not_configured`` is the E7 API-only mode (no child wired yet);
        E9 attaches a real :class:`MCPServerProcess` and this reflects its
        ``running`` flag.
        """
        if self._mcp is None:
            return MCPChildStatus.NOT_CONFIGURED
        return MCPChildStatus.RUNNING if self._mcp.running else MCPChildStatus.STOPPED

    async def health(self) -> HealthResponse:
        """Liveness + MCP child status + active run id (plan §6).

        Reports the active run from persisted state without mutating the
        in-memory ``active_run_id`` pointer — a GET must not have a write
        side-effect, and once E9 wires the controller loop that pointer is the
        loop's to own, not a health poll's.
        """
        active = await self._store.active_run()
        return HealthResponse(
            version=__version__,
            mcp_child=self.mcp_child_status(),
            active_run_id=None if active is None else active.run_id,
        )

    async def start_roast(self, profile: RoastProfile) -> RoastDetail:
        """Start a roast: persist the run record, claim it as active (plan §6).

        Returns 409 (``RoastRunConflictError``) when a run is already active —
        the API-level guard the controller's idle-only ``start_run`` transition
        mirrors as the inner guard. The persisted run begins in ``starting``;
        the E9 vertical slice drives the MCP session start and the tick loop
        that advances it. The active-run check reads persisted state, so the
        guard holds across an agent restart.
        """
        async with self._start_lock:
            active = await self._store.active_run()
            if active is not None:
                raise RoastRunConflictError(
                    f"a roast is already active (run {active.run_id}, phase "
                    f"{active.agent_phase.value}); end it before starting another"
                )
            run_id = uuid.uuid4().hex
            await self._store.create_run(
                run_id=run_id,
                profile=profile,
                config=self._config,
                agent_phase=RoastPhase.STARTING,
            )
            self.active_run_id = run_id
        detail = await self._store.read_run(run_id)
        if detail is None:  # pragma: no cover — read immediately after create
            raise RuntimeError(f"read_run returned None for just-created run {run_id}")
        return detail

    async def history(self) -> RoastHistory:
        """The roast history list, newest first (plan §6)."""
        return RoastHistory(runs=await self._store.list_runs())

    async def detail(self, run_id: str) -> RoastDetail:
        """Run detail, or 404 (plan §6)."""
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        return detail

    async def telemetry(self, run_id: str, *, downsample: int) -> TelemetrySeries:
        """The downsampled telemetry series for a run, or 404 (plan §6)."""
        if await self._store.read_run(run_id) is None:
            raise RoastRunNotFoundError(run_id)
        points = await self._store.read_telemetry_points(run_id, downsample=downsample)
        return TelemetrySeries(
            run_id=run_id,
            downsample=downsample,
            point_count=len(points),
            points=points,
        )

    async def timeline(self, run_id: str) -> RoastTimeline:
        """The decision-trace timeline for a run, or 404 (plan §6)."""
        if await self._store.read_run(run_id) is None:
            raise RoastRunNotFoundError(run_id)
        return await self._store.read_timeline(run_id)

    async def log_manifest(self, run_id: str) -> LogManifest:
        """The export-log manifest for a run.

        404 when the run is unknown *or* has no export manifest yet (the
        export runs at roast completion via the MCP client; an in-progress or
        never-exported run carries none).
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.export_manifest is None:
            raise RoastRunNotFoundError(f"run {run_id} has no export manifest")
        return detail.export_manifest

    async def log_artifact_path(self, run_id: str, artifact: LogArtifactName) -> Path:
        """Resolve a downloadable export artifact to its on-disk path, or 404.

        404 covers every missing case: unknown run, no manifest, the export
        not marked ``ready``, or the file absent on disk — the API never
        streams a half-written or stale export.
        """
        manifest = await self.log_manifest(run_id)
        paths: dict[str, str] = {
            "jsonl": manifest.jsonl_path,
            "csv": manifest.csv_path,
            "summary": manifest.summary_path,
        }
        if artifact not in paths:
            raise RoastRunNotFoundError(f"unknown log artifact {artifact!r}")
        if not manifest.ready:
            raise RoastRunNotFoundError(f"run {run_id} export is not ready")
        path = Path(paths[artifact])
        if not path.is_file():
            raise RoastRunNotFoundError(f"run {run_id} {artifact} file is not available")
        return path

    async def rate(self, run_id: str, rating: OperatorRatingRequest) -> RoastDetail:
        """Record the operator self-rating, or 404/409 (plan §6).

        404 when the run is unknown; 409 when it is still in progress — a
        rating is one of the explicit immutability exceptions the store allows
        only on completed runs, so the API surfaces the in-progress case as a
        conflict rather than letting the store's RuntimeError escape as a 500.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is None:
            raise RoastRunConflictError(
                f"run {run_id} is still in progress; rate it after completion"
            )
        await self._store.set_operator_rating(run_id, rating=rating.stars, notes=rating.notes)
        rated = await self._store.read_run(run_id)
        if rated is None:  # pragma: no cover — immutable once completed
            raise RuntimeError(f"read_run returned None for rated run {run_id}")
        return rated

    async def submit_operator_action(
        self, run_id: str, request: OperatorActionRequest
    ) -> OperatorActionResult:
        """Queue an operator action through safety policy (plan §6).

        The pipeline is: validate the action against the run's current phase
        via the existing command×phase matrix, record an ``operator_actions``
        row with the outcome, and — when accepted — place the action on the
        controller queue. The controller drains the queue each tick and runs
        the *full* safety policy again before any MCP write (E9): this method
        never writes hardware and never bypasses or reimplements safety. The
        phase pre-check exists only so the operator gets immediate feedback
        (e.g. a drop requested during preheating is rejected now, not silently
        held). 404s an unknown run id.

        Control actions with no MCP write (``pause_advisory`` /
        ``resume_advisory`` / ``acknowledge_recovery``) skip the matrix check
        and are accepted for the controller to interpret on drain.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)

        command = _ACTION_COMMAND.get(request.action)
        if command is None:
            result: Literal["accepted", "rejected", "failed"] = "accepted"
            reason = f"{request.action.value} accepted: queued for the controller"
        else:
            evaluation = self._safety.evaluate_command_phase(
                command=command, phase=detail.agent_phase
            )
            if evaluation.verdict is SafetyVerdict.ALLOW:
                result = "accepted"
                reason = f"{request.action.value} accepted in phase {detail.agent_phase.value}"
            else:
                result = "rejected"
                reason = evaluation.reason

        await self._store.record_operator_action(
            action=request.action.value,
            result=result,
            run_id=run_id,
            payload=request.payload,
        )
        queued = result == "accepted"
        if queued:
            self.operator_queue.put_nowait(
                QueuedOperatorAction(run_id=run_id, action=request.action, payload=request.payload)
            )
        return OperatorActionResult(
            action=request.action, result=result, reason=reason, queued=queued
        )


def _get_service(request: Request) -> RoastService:
    """Dependency: the app's :class:`RoastService`, or 503 if unconfigured.

    ``create_app()`` with no service (the scaffold smoke app) leaves
    ``app.state`` bare; every store-backed route then fails closed with a
    clear 503 rather than an ``AttributeError`` 500.
    """
    service = getattr(request.app.state, "service", None)
    if not isinstance(service, RoastService):
        raise HTTPException(status_code=503, detail="roast service not configured")
    return service


ServiceDep = Annotated[RoastService, Depends(_get_service)]


async def health(request: Request) -> HealthResponse:
    """``GET /api/health`` — works with or without a configured service.

    Without a service (scaffold app) it still reports liveness and version so
    the probe never depends on a store being wired.
    """
    service = getattr(request.app.state, "service", None)
    if isinstance(service, RoastService):
        return await service.health()
    return HealthResponse(
        version=__version__,
        mcp_child=MCPChildStatus.NOT_CONFIGURED,
        active_run_id=None,
    )


async def start_roast(profile: RoastProfile, service: ServiceDep) -> RoastDetail:
    """``POST /api/roasts`` — start a roast (409 if one is active)."""
    try:
        return await service.start_roast(profile)
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def list_roasts(service: ServiceDep) -> RoastHistory:
    """``GET /api/roasts`` — roast history list."""
    return await service.history()


async def get_roast(run_id: str, service: ServiceDep) -> RoastDetail:
    """``GET /api/roasts/{run_id}`` — run detail."""
    try:
        return await service.detail(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def get_telemetry(
    run_id: str,
    service: ServiceDep,
    downsample: Annotated[int, Query(ge=1)] = 1,
) -> TelemetrySeries:
    """``GET /api/roasts/{run_id}/telemetry`` — downsampled snapshot series."""
    try:
        return await service.telemetry(run_id, downsample=downsample)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def get_timeline(run_id: str, service: ServiceDep) -> RoastTimeline:
    """``GET /api/roasts/{run_id}/timeline`` — the decision trace."""
    try:
        return await service.timeline(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def get_log_manifest(run_id: str, service: ServiceDep) -> LogManifest:
    """``GET /api/roasts/{run_id}/log`` — the export-log manifest."""
    try:
        return await service.log_manifest(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def download_log(
    run_id: str,
    artifact: str,
    service: ServiceDep,
) -> FileResponse:
    """``GET /api/roasts/{run_id}/log/{artifact}`` — download an export file.

    ``artifact`` is validated in :meth:`RoastService.log_artifact_path` (the
    single artifact-name check), which 404s an unknown name like any other
    missing artifact.
    """
    try:
        path = await service.log_artifact_path(run_id, artifact)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


async def rate_roast(
    run_id: str,
    rating: OperatorRatingRequest,
    service: ServiceDep,
) -> RoastDetail:
    """``POST /api/roasts/{run_id}/rating`` — operator self-rating."""
    try:
        return await service.rate(run_id, rating)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def submit_operator_action(
    run_id: str,
    action: OperatorActionRequest,
    service: ServiceDep,
) -> OperatorActionResult:
    """``POST /api/roasts/{run_id}/operator-actions`` — queue an operator action.

    Always 200 with the typed :class:`OperatorActionResult`: the request was
    recorded and its policy outcome (``accepted`` / ``rejected``) is in the
    body, so the SPA renders one shape. 404 only when the run is unknown; an
    unknown action value is a 422 from request validation.
    """
    try:
        return await service.submit_operator_action(run_id, action)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def create_app(service: RoastService | None = None) -> FastAPI:
    """Create the FastAPI application.

    ``service`` is the backend authority (store + active-run state + MCP
    handle); when omitted, only ``/api/health`` is functional and every
    store-backed route returns 503 — the shape the E1 scaffold smoke test
    relies on. The E9 vertical slice constructs a fully-wired service and
    passes it here.
    """
    app = FastAPI(title="roastpilot-agent", version=__version__)
    app.state.service = service
    app.get("/api/health")(health)
    app.post("/api/roasts", status_code=201)(start_roast)
    app.get("/api/roasts")(list_roasts)
    app.get("/api/roasts/{run_id}")(get_roast)
    app.get("/api/roasts/{run_id}/telemetry")(get_telemetry)
    app.get("/api/roasts/{run_id}/timeline")(get_timeline)
    app.get("/api/roasts/{run_id}/log")(get_log_manifest)
    app.get("/api/roasts/{run_id}/log/{artifact}")(download_log)
    app.post("/api/roasts/{run_id}/rating")(rate_roast)
    app.post("/api/roasts/{run_id}/operator-actions")(submit_operator_action)
    return app
