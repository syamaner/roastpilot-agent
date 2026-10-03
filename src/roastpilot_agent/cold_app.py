"""Read-only cold-characterisation ASGI app with one cold observation SSE route.

:func:`create_cold_app` builds the ordinary FastAPI app over an API-only
:class:`~roastpilot_agent.api.RoastService` (no roaster, MCP child or advisor) with
a cold lifespan, then adds exactly one route,
``GET``/``HEAD`` :data:`COLD_OBSERVATION_EVENTS_PATH`, and an outermost read-only
guard.  The guard passes only exact ``GET`` and ``HEAD``; every other HTTP method
receives a fixed 409 without the inner app or ``receive()`` ever being called,
WebSocket is closed with 1008 and lifespan passes through.  It is a method guard,
not a ``GET`` allow-list: existing ``GET`` routes stay reachable (a recorded
residual).

The cold lifespan performs no seeding, recovery or service shutdown; it only closes
the hub on exit.  Health reports ``mcp_child: not_configured`` because the
normal-mode service has no child wired; that never proves no cold child exists.
"""

import asyncio
import contextlib
import math
import typing
from collections.abc import AsyncGenerator, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from roastpilot_agent.api import FiniteJSONResponse, RoastService, create_app
from roastpilot_agent.cold_observation_stream import (
    HEARTBEAT_FRAME,
    ColdObservationHub,
    ColdSubscription,
)
from roastpilot_agent.config import AppConfig
from roastpilot_agent.store import RoastStore

COLD_OBSERVATION_EVENTS_PATH: typing.Final = "/api/cold-characterisation/events"
_READ_ONLY_METHODS: typing.Final = frozenset({"GET", "HEAD"})
_READ_ONLY_DETAIL: typing.Final = "Cold characterisation mode is read-only."
_SSE_HEADERS: typing.Final = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class _ColdReadOnlyGuard:
    """Outermost pure-ASGI guard: exact ``GET``/``HEAD`` only; nothing else is read."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "http":
            if scope["method"] in _READ_ONLY_METHODS:
                await self._app(scope, receive, send)
                return
            response = FiniteJSONResponse(status_code=409, content={"detail": _READ_ONLY_DETAIL})
            await response(scope, receive, send)
        elif kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        elif kind == "lifespan":
            await self._app(scope, receive, send)


def _hub(request: Request) -> ColdObservationHub | None:
    """The app's cold hub, or ``None`` when absent or of another type."""
    hub: object = getattr(request.app.state, "cold_observation_hub", None)
    return hub if type(hub) is ColdObservationHub else None


async def _next_frame(
    request: Request, subscription: ColdSubscription, heartbeat: float
) -> str | None:
    """The next frame, a heartbeat after ``heartbeat`` quiet seconds, or ``None`` to end.

    ``None`` means the client disconnected or the subscription received the end
    sentinel (hub closed or the subscriber overflowed).
    """
    if await request.is_disconnected():
        return None
    waiting = subscription.get()
    try:
        return await asyncio.wait_for(waiting, timeout=heartbeat)
    except TimeoutError:
        return HEARTBEAT_FRAME


async def cold_observation_events(request: Request) -> Response:
    """Stream cold observation frames (``GET``) or report the stream headers (``HEAD``).

    ``HEAD`` never subscribes.  ``GET`` subscribes only inside the response body
    generator, so a response that is never iterated holds no slot, and releases its
    slot on completion, cancellation, disconnect or overflow.  ``Last-Event-ID`` is
    read from the header first, then the ``last_event_id`` query parameter; a
    malformed value means a fresh stream.  No client value is logged or echoed.
    """
    hub = _hub(request)
    if hub is None:
        return FiniteJSONResponse(
            status_code=503, content={"detail": "Cold observation stream unavailable."}
        )
    if request.method == "HEAD":
        return Response(status_code=200, media_type="text/event-stream", headers=_SSE_HEADERS)
    if not hub.admission_available():
        return FiniteJSONResponse(
            status_code=503,
            content={"detail": "Cold observation stream subscriber limit reached."},
        )
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is None:
        last_event_id = request.query_params.get("last_event_id")
    heartbeat: float = request.app.state.cold_sse_heartbeat_seconds

    async def frames() -> AsyncIterator[str]:
        subscription = None
        try:
            yield ": connected\n\n"
            subscription = hub.subscribe(last_event_id)
            if subscription is None:
                return
            while True:
                frame = await _next_frame(request, subscription, heartbeat)
                if frame is None:
                    return
                yield frame
        finally:
            if subscription is not None:
                hub.unsubscribe(subscription)

    return StreamingResponse(frames(), media_type="text/event-stream", headers=_SSE_HEADERS)


@contextlib.asynccontextmanager
async def _cold_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """No seeding, recovery or service shutdown; close the hub on exit."""
    try:
        yield
    finally:
        hub: object = getattr(app.state, "cold_observation_hub", None)
        if type(hub) is ColdObservationHub:
            hub.close()


def create_cold_app(
    store: RoastStore,
    config: AppConfig,
    hub: ColdObservationHub,
    *,
    sse_heartbeat_seconds: float = 15.0,
) -> FastAPI:
    """Create the read-only cold-characterisation app.

    Caller obligations: ``store`` is an already-initialised, empty, run-private
    store (this factory never initialises or migrates it); the app is hosted on the
    same event loop as the cold runtime that publishes to ``hub``; any later mount
    goes after the cold route and stays inside the guard.

    Args:
        store: The initialised run-private store.
        config: The application config.
        hub: The cold observation hub (the runtime's display-only tick observer).
        sse_heartbeat_seconds: The finite positive heartbeat interval.

    Returns:
        The guarded app.

    Raises:
        ValueError: If the heartbeat is not finite and positive (fixed text).
    """
    if not (math.isfinite(sse_heartbeat_seconds) and sse_heartbeat_seconds > 0):
        raise ValueError("Cold observation heartbeat must be finite and positive.")
    service = RoastService(
        store,
        config=config,
        roaster=None,
        mcp=None,
        advisor=None,
        sse_heartbeat_seconds=float(sse_heartbeat_seconds),
    )
    app = create_app(service, lifespan=_cold_lifespan, spa_dir=None)
    app.state.cold_observation_hub = hub
    app.state.cold_sse_heartbeat_seconds = float(sse_heartbeat_seconds)
    app.api_route(COLD_OBSERVATION_EVENTS_PATH, methods=["GET", "HEAD"])(cold_observation_events)
    app.add_middleware(_ColdReadOnlyGuard)
    return app
