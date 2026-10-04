"""Hardware-free tests for the read-only cold ASGI app and its SSE route (#954 U2).

Requests are raw ASGI calls with counting ``receive`` callables, so a blocked
request is proved never to read its body.  Host serial/audio enumeration is
replaced by fail-if-called sentinels for every test; no provider, child process,
serial port or microphone is touched.
"""

# pyright: reportPrivateUsage=false

import ast
import asyncio
import json
import math
import typing
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from starlette.types import Message

from roastpilot_agent import api, cold_app
from roastpilot_agent import cold_observation_stream as stream
from roastpilot_agent.config import AppConfig
from roastpilot_agent.store import RoastStore
from tests.test_cold_characterisation_conformance import _import_base
from tests.test_cold_observation_stream import EPOCH, frame_data, frame_id, make_tick

PATH = cold_app.COLD_OBSERVATION_EVENTS_PATH


@pytest.fixture(autouse=True)
def no_host_enumeration_or_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host enumeration fails if reached; product environment names are stripped."""

    def forbidden() -> typing.NoReturn:
        raise AssertionError("host enumeration reached")

    monkeypatch.setattr(api, "_enumerate_serial", forbidden)
    monkeypatch.setattr(api, "_enumerate_audio_inputs", forbidden)
    import os

    for name in list(os.environ):
        if name.startswith(("ROASTPILOT_", "COFFEE_")) or name == "OPENROUTER_API_KEY":
            monkeypatch.delenv(name)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> typing.AsyncIterator[RoastStore]:
    roast_store = RoastStore(db_path=tmp_path / "roastpilot-test.sqlite3")
    await roast_store.initialize()
    try:
        yield roast_store
    finally:
        await roast_store.close()


def make_app(store: RoastStore, hub: stream.ColdObservationHub, heartbeat: float = 15.0) -> FastAPI:
    return cold_app.create_cold_app(store, AppConfig(), hub, sse_heartbeat_seconds=heartbeat)


class Counting:
    """A ``receive`` that counts calls; it serves ``messages`` then waits for ``release``."""

    def __init__(self, messages: list[Message] | None = None) -> None:
        self.messages = list(messages or [])
        self.calls = 0
        self.release = asyncio.Event()

    async def __call__(self) -> Message:
        self.calls += 1
        if self.messages:
            return self.messages.pop(0)
        await self.release.wait()
        return {"type": "http.disconnect"}


class Sent:
    """A ``send`` collecting messages and signalling each body chunk."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.changed = asyncio.Event()

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)
        self.changed.set()

    @property
    def status(self) -> int:
        return typing.cast(int, self.messages[0]["status"])

    @property
    def headers(self) -> dict[str, str]:
        return {key.decode(): value.decode() for key, value in self.messages[0].get("headers", [])}

    @property
    def chunks(self) -> list[str]:
        return [
            typing.cast(bytes, m.get("body", b"")).decode()
            for m in self.messages
            if m["type"] == "http.response.body" and m.get("body")
        ]

    @property
    def body(self) -> bytes:
        return b"".join(
            typing.cast(bytes, m.get("body", b""))
            for m in self.messages
            if m["type"] == "http.response.body"
        )

    async def until(self, predicate: typing.Callable[[], bool]) -> None:
        while not predicate():
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), 5)


def http_scope(
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    query: bytes = b"",
) -> Message:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }


async def request(
    app: typing.Any,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[Sent, Counting]:
    """One complete non-streaming request; the body is served when read."""
    receive = Counting([{"type": "http.request", "body": body, "more_body": False}])
    receive.release.set()
    sent = Sent()
    await app(http_scope(method, path, headers=headers), receive, sent)
    return sent, receive


def json_body(sent: Sent) -> typing.Any:
    return json.loads(sent.body)


@pytest.fixture
def limiter_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Counts every call reaching any existing body limiter (the guard's inner app)."""
    calls: list[str] = []
    real = api._RouteBodyLimitMiddleware.__call__

    async def counting(
        self: typing.Any, scope: typing.Any, receive: typing.Any, send: typing.Any
    ) -> None:
        calls.append(scope["type"])
        await real(self, scope, receive, send)

    monkeypatch.setattr(api._RouteBodyLimitMiddleware, "__call__", counting)
    return calls


# ------------------------------------------------------------------ T-A1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "get"])
async def test_t_a1_every_non_read_method_is_a_fixed_409(
    store: RoastStore, method: str, limiter_calls: list[str]
) -> None:
    """T-A1: only exact GET/HEAD pass; every other method (and ``get``) gets the fixed 409."""
    app = make_app(store, stream.ColdObservationHub())
    for path in (PATH, api.CONFIG_PATH, "/api/roasts", "/nowhere"):
        sent, receive = await request(app, method, path, body=b"{}")
        assert sent.status == 409
        assert json_body(sent) == {"detail": "Cold characterisation mode is read-only."}
        assert receive.calls == 0
    assert limiter_calls == []


# ------------------------------------------------------------------ T-A2


@pytest.mark.asyncio
async def test_t_a2_blocked_writes_never_reach_the_limiter_or_the_body(
    store: RoastStore, limiter_calls: list[str]
) -> None:
    """T-A2: an oversized bounded POST is 409 (not 413) with zero receives and limiter calls."""
    app = make_app(store, stream.ColdObservationHub())
    oversized = b"x" * (api._HARDWARE_CLEAR_MAX_BODY_BYTES + 1)
    sent, receive = await request(
        app,
        "POST",
        api._HARDWARE_CLEAR_PATH,
        body=oversized,
        headers=[(b"content-length", str(len(oversized)).encode())],
    )
    assert sent.status == 409
    assert receive.calls == 0 and limiter_calls == []
    sent, receive = await request(app, "POST", "/api/roasts", body=b"{}")
    assert sent.status == 409 and receive.calls == 0
    assert await store.list_runs() == []
    sent, receive = await request(app, "POST", "/api/unknown-write", body=b"{}")
    assert sent.status == 409 and receive.calls == 0
    assert limiter_calls == []


@pytest.mark.asyncio
async def test_t_a2_control_reads_do_reach_the_inner_stack(
    store: RoastStore, limiter_calls: list[str]
) -> None:
    """Control: an allowed GET passes the guard into the inner middleware stack."""
    app = make_app(store, stream.ColdObservationHub())
    sent, _receive = await request(app, "GET", api.HEALTH_PATH)
    assert sent.status == 200
    assert limiter_calls == ["http", "http", "http"]


# ------------------------------------------------------------------ T-A3


@pytest.mark.asyncio
async def test_t_a3_websocket_is_closed_1008_before_the_inner_app(
    store: RoastStore, limiter_calls: list[str]
) -> None:
    """T-A3: a WebSocket scope is closed with 1008; nothing inside the guard is reached."""
    app = make_app(store, stream.ColdObservationHub())
    receive = Counting([{"type": "websocket.connect"}])
    sent = Sent()
    scope = {**http_scope("GET", PATH), "type": "websocket"}
    await app(scope, receive, sent)
    assert sent.messages == [{"type": "websocket.close", "code": 1008}]
    assert receive.calls == 0 and limiter_calls == []


@pytest.mark.asyncio
async def test_unknown_scope_types_are_ignored_and_lifespan_passes(store: RoastStore) -> None:
    """Any other scope type returns untouched; lifespan reaches the inner app; order holds."""
    reached: list[str] = []

    async def inner(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        reached.append(scope["type"])

    guard = cold_app._ColdReadOnlyGuard(inner)
    receive, sent = Counting(), Sent()
    await guard({"type": "other"}, receive, sent)
    assert sent.messages == [] and receive.calls == 0 and reached == []
    await guard({"type": "lifespan"}, receive, sent)
    assert reached == ["lifespan"]
    app = make_app(store, stream.ColdObservationHub())
    assert app.user_middleware[0].cls is cold_app._ColdReadOnlyGuard


# ------------------------------------------------------------------ T-A4


async def run_lifespan(app: FastAPI) -> list[Message]:
    receive = Counting([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent = Sent()
    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, sent)
    return sent.messages


@pytest.mark.asyncio
async def test_t_a4_cold_lifespan_seeds_recovers_and_shuts_down_nothing(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-A4: no seeding, recovery or shutdown; the hub closes at exit; tables stay empty."""
    calls: list[str] = []

    def spy(name: str) -> typing.Callable[..., typing.Any]:
        async def record(self: typing.Any) -> None:
            calls.append(name)

        return record

    for name in ("seed_bean_profiles", "recover_on_start", "shutdown"):
        monkeypatch.setattr(api.RoastService, name, spy(name))
    hub = stream.ColdObservationHub()
    subscription = hub.subscribe(None)
    assert subscription is not None
    app = make_app(store, hub)
    messages = await run_lifespan(app)
    assert [m["type"] for m in messages] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert calls == []
    assert hub.closed and subscription.queue.get_nowait() is None
    assert await store.list_bean_profiles() == []
    assert await store.list_runs() == []


@pytest.mark.asyncio
async def test_t_a4_lifespan_tolerates_a_replaced_hub(store: RoastStore) -> None:
    """The lifespan closes only an exact hub; anything else is left alone."""
    hub = stream.ColdObservationHub()
    app = make_app(store, hub)
    app.state.cold_observation_hub = object()
    messages = await run_lifespan(app)
    assert messages[-1]["type"] == "lifespan.shutdown.complete"
    assert not hub.closed


# ------------------------------------------------------- T-A5 / T-A6


def route_set(app: FastAPI) -> set[tuple[str, frozenset[str]]]:
    return {
        (
            typing.cast(str, getattr(route, "path", "")),
            frozenset(getattr(route, "methods", None) or ()),
        )
        for route in app.routes
    }


@pytest.mark.asyncio
async def test_t_a5_the_normal_app_has_no_cold_route(store: RoastStore, tmp_path: Path) -> None:
    """T-A5 (cross-mode regression oracle): normal app with the SPA keeps a JSON 404."""
    spa = tmp_path / "spa"
    spa.mkdir()
    (spa / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    normal = api.create_app(api.RoastService(store), spa_dir=spa)
    sent, _receive = await request(normal, "GET", PATH)
    assert sent.status == 404
    assert sent.headers["content-type"].startswith("application/json")
    assert PATH not in normal.openapi()["paths"]


@pytest.mark.asyncio
async def test_t_a6_exactly_one_route_is_added(store: RoastStore) -> None:
    """T-A6: cold routes are the normal factory's plus exactly ``(PATH, {GET, HEAD})``."""
    normal = api.create_app(api.RoastService(store), spa_dir=None)
    cold = make_app(store, stream.ColdObservationHub())
    assert route_set(cold) == route_set(normal) | {(PATH, frozenset({"GET", "HEAD"}))}
    assert len(cold.routes) == len(normal.routes) + 1


# ------------------------------------------------------------------ T-A7


@pytest.mark.asyncio
async def test_t_a7_head_reports_the_stream_without_subscribing(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-A7: a real ASGI HEAD: 200, event-stream, empty body, zero subscribe calls."""
    hub = stream.ColdObservationHub()
    subscribes: list[object] = []
    real = hub.subscribe

    def spy(last_event_id: object = None) -> stream.ColdSubscription | None:
        subscribes.append(last_event_id)
        return real(last_event_id)

    monkeypatch.setattr(hub, "subscribe", spy)
    app = make_app(store, hub)
    receive = Counting([{"type": "http.request", "body": b"", "more_body": False}])
    receive.release.set()
    sent = Sent()
    await asyncio.wait_for(app(http_scope("HEAD", PATH), receive, sent), 5)
    assert subscribes == []
    assert hub.subscriber_count == 0
    assert sent.status == 200
    assert sent.headers["content-type"].startswith("text/event-stream")
    assert sent.headers["cache-control"] == "no-cache"
    assert sent.body == b""


# ------------------------------------------------------- streaming GET


class Stream:
    """One in-flight streaming GET driven over raw ASGI."""

    def __init__(
        self,
        app: FastAPI,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
        query: bytes = b"",
    ) -> None:
        self.receive = Counting([{"type": "http.request", "body": b"", "more_body": False}])
        self.sent = Sent()
        self.task = asyncio.ensure_future(
            app(http_scope("GET", PATH, headers=headers, query=query), self.receive, self.sent)
        )

    async def chunks_until(self, count: int) -> list[str]:
        await self.sent.until(lambda: len(self.sent.chunks) >= count)
        return self.sent.chunks

    async def disconnect(self) -> None:
        self.receive.release.set()
        await asyncio.wait_for(self.task, 5)


async def until(predicate: typing.Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_t_a8_get_streams_connected_then_frames_and_disconnect_releases(
    store: RoastStore,
) -> None:
    """T-A8: ``: connected``, then a published frame; a disconnect releases the slot."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    app = make_app(store, hub)
    live = Stream(app)
    assert (await live.chunks_until(1))[0] == ": connected\n\n"
    await until(lambda: hub.subscriber_count == 1)
    assert live.sent.status == 200
    assert live.sent.headers["content-type"].startswith("text/event-stream")
    hub.publish(make_tick())
    chunks = await live.chunks_until(2)
    assert frame_id(chunks[1]) == f"{EPOCH}-1"
    await live.disconnect()
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_t_a8_heartbeat_and_hub_close_end_the_stream(store: RoastStore) -> None:
    """A quiet stream sends the exact heartbeat; closing the hub ends it and frees the slot."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    app = make_app(store, hub, heartbeat=0.01)
    live = Stream(app)
    chunks = await live.chunks_until(2)
    assert chunks[1] == stream.HEARTBEAT_FRAME
    hub.close()
    await asyncio.wait_for(live.task, 5)
    assert hub.subscriber_count == 0
    assert live.sent.messages[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }


def bare_request(app: FastAPI, query: bytes = b"") -> Request:
    scope = {**http_scope("GET", PATH, query=query), "app": app}

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_t_a8_an_un_iterated_response_holds_no_slot(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-A8: a response built but never iterated never calls ``subscribe``."""
    hub = stream.ColdObservationHub()
    subscribes: list[object] = []
    real = hub.subscribe

    def spy(last_event_id: object = None) -> stream.ColdSubscription | None:
        subscribes.append(last_event_id)
        return real(last_event_id)

    monkeypatch.setattr(hub, "subscribe", spy)
    app = make_app(store, hub)
    response = await cold_app.cold_observation_events(bare_request(app))
    assert response.status_code == 200
    assert subscribes == [] and hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_t_a8_admission_lost_after_the_handler_ends_the_body(store: RoastStore) -> None:
    """If the hub fills between handler and iteration, the body ends after ``connected``."""
    hub = stream.ColdObservationHub(max_subscribers=1)
    app = make_app(store, hub)
    response = await cold_app.cold_observation_events(bare_request(app))
    held = hub.subscribe(None)
    assert held is not None
    body = typing.cast(typing.Any, response).body_iterator
    assert [chunk async for chunk in body] == [": connected\n\n"]
    assert hub.subscriber_count == 1


@pytest.mark.asyncio
async def test_t_a8_a_disconnected_client_is_released_by_the_loop(store: RoastStore) -> None:
    """A client already disconnected ends the loop and releases its slot in ``finally``."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    app = make_app(store, hub)
    response = await cold_app.cold_observation_events(bare_request(app))
    body = typing.cast(typing.Any, response).body_iterator
    assert await body.__anext__() == ": connected\n\n"
    hub.publish(make_tick())
    assert [chunk async for chunk in body] == []
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_t_a8_hub_close_ends_a_connected_body(store: RoastStore) -> None:
    """A connected body ends on the hub's end sentinel and releases its slot."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    app = make_app(store, hub)
    never = asyncio.Event()

    async def receive() -> Message:
        await never.wait()
        return {"type": "http.disconnect"}  # pragma: no cover - never released.

    scope = {**http_scope("GET", PATH), "app": app}
    response = await cold_app.cold_observation_events(Request(scope, receive))
    body = typing.cast(typing.Any, response).body_iterator
    assert await body.__anext__() == ": connected\n\n"
    pending = asyncio.ensure_future(body.__anext__())
    await until(lambda: hub.subscriber_count == 1)
    hub.publish(make_tick())
    assert frame_id(await asyncio.wait_for(pending, 5)) == f"{EPOCH}-1"
    pending = asyncio.ensure_future(body.__anext__())
    await until(lambda: hub.subscriber_count == 1 and not pending.done())
    hub.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 5)
    assert hub.subscriber_count == 0


class _Disconnect:
    """A request stand-in whose disconnect state is fixed."""

    def __init__(self, disconnected: bool) -> None:
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.mark.asyncio
async def test_next_frame_frame_heartbeat_end_and_disconnect() -> None:
    """The wait helper returns a frame, the heartbeat, ``None`` at the end or on disconnect."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(None)
    assert subscription is not None
    connected = typing.cast(Request, _Disconnect(False))
    hub.publish(make_tick())
    assert frame_id(await cold_app._next_frame(connected, subscription, 5.0)) == f"{EPOCH}-1"
    assert await cold_app._next_frame(connected, subscription, 0.01) == stream.HEARTBEAT_FRAME
    hub.publish(make_tick())
    gone = typing.cast(Request, _Disconnect(True))
    assert await cold_app._next_frame(gone, subscription, 5.0) is None
    hub.close()
    assert frame_id(await cold_app._next_frame(connected, subscription, 5.0)) == f"{EPOCH}-2"
    assert await cold_app._next_frame(connected, subscription, 5.0) is None


# ------------------------------------------------------- T-A9 / T-A10


@pytest.mark.asyncio
async def test_t_a9_full_hub_is_a_fixed_503(store: RoastStore) -> None:
    """T-A9: at the limit the route answers the fixed 503; the count is unchanged."""
    hub = stream.ColdObservationHub(max_subscribers=1)
    held = hub.subscribe(None)
    assert held is not None
    app = make_app(store, hub)
    sent, _receive = await request(app, "GET", PATH)
    assert sent.status == 503
    assert json_body(sent) == {"detail": "Cold observation stream subscriber limit reached."}
    assert hub.subscriber_count == 1


@pytest.mark.asyncio
async def test_missing_hub_is_a_fixed_503(store: RoastStore) -> None:
    """A missing or foreign hub on the app state answers a fixed 503."""
    app = make_app(store, stream.ColdObservationHub())
    for value in (None, object()):
        app.state.cold_observation_hub = value
        sent, _receive = await request(app, "GET", PATH)
        assert sent.status == 503
        assert json_body(sent) == {"detail": "Cold observation stream unavailable."}


@pytest.mark.asyncio
async def test_t_a10_header_precedes_query_and_query_is_the_fallback(store: RoastStore) -> None:
    """T-A10: the header wins over the query; the query is used when no header is sent."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    for index in range(3):
        hub.publish(make_tick(tick=index))
    app = make_app(store, hub)
    both = Stream(
        app,
        headers=[(b"last-event-id", f"{EPOCH}-2".encode())],
        query=f"last_event_id={EPOCH}-1".encode(),
    )
    chunks = await both.chunks_until(2)
    await both.disconnect()
    assert [frame_id(chunk) for chunk in chunks[1:]] == [f"{EPOCH}-3"]
    fallback = Stream(app, query=f"last_event_id={EPOCH}-1".encode())
    chunks = await fallback.chunks_until(3)
    await fallback.disconnect()
    assert [frame_id(chunk) for chunk in chunks[1:]] == [f"{EPOCH}-2", f"{EPOCH}-3"]
    assert hub.subscriber_count == 0


# ------------------------------------------------------- T-A11 / T-A12


@pytest.mark.asyncio
async def test_t_a11_health_reports_not_configured(store: RoastStore) -> None:
    """T-A11: ``mcp_child`` is ``not_configured`` and no run is active."""
    app = make_app(store, stream.ColdObservationHub())
    sent, _receive = await request(app, "GET", api.HEALTH_PATH)
    assert sent.status == 200
    health = json_body(sent)
    assert health["mcp_child"] == "not_configured"
    assert health["active_run_id"] is None


@pytest.mark.asyncio
async def test_t_a12_devices_get_passes_the_guard(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-A12: the existing devices GET passes the guard (the recorded U4 residual)."""
    fixed = api.DeviceOption(value="fixed-port", label="fixed", note="synthetic")

    def serial() -> tuple[list[api.DeviceOption], str | None]:
        return [fixed], None

    def audio_inputs() -> tuple[list[api.DeviceOption], str | None]:
        return [], None

    monkeypatch.setattr(api, "_enumerate_serial", serial)
    monkeypatch.setattr(api, "_enumerate_audio_inputs", audio_inputs)
    app = make_app(store, stream.ColdObservationHub())
    sent, _receive = await request(app, "GET", api.DEVICES_PATH)
    assert sent.status == 200


@pytest.mark.parametrize("heartbeat", [0.0, -1.0, math.inf, math.nan])
def test_create_cold_app_refuses_a_bad_heartbeat(heartbeat: float) -> None:
    """A non-finite or non-positive heartbeat is refused with fixed text."""
    with pytest.raises(ValueError, match="^Cold observation heartbeat must be finite"):
        cold_app.create_cold_app(
            typing.cast(RoastStore, None),
            AppConfig(),
            stream.ColdObservationHub(),
            sse_heartbeat_seconds=heartbeat,
        )


# ------------------------------------------------------------------ T-A13


@pytest.mark.asyncio
async def test_t_a13_end_to_end_one_frame_per_retained_tick(tmp_path: Path) -> None:
    """T-A13: a run publishing to the hub yields one frame per retained tick, matching."""
    from tests.test_cold_characterisation_two_phase import Outcome
    from tests.test_cold_characterisation_two_phase_observer import (
        TICKS_PER_PHASE,
        ObservedWorld,
    )

    hub = stream.ColdObservationHub(queue_frames=16, ring_frames=16)
    subscription = hub.subscribe(None)
    assert subscription is not None
    world = ObservedWorld(tmp_path, hub.publish)
    result = await world.run()
    outcome = result.outcome
    assert outcome is Outcome.ADVISORY_CONFORMANT
    frames: list[str | None] = []
    while not subscription.queue.empty():
        frames.append(subscription.queue.get_nowait())
    ticks = world.records("tick")
    assert len(frames) == len(ticks) == 2 * TICKS_PER_PHASE == len(hub._ring)
    for frame, tick in zip(frames, ticks, strict=True):
        data = frame_data(frame)
        assert data["bean_temp_c"] == tick["device"]["bean_temp_c"]
        assert data["env_temp_c"] == tick["device"]["env_temp_c"]
        assert data["recorded_at_utc"] == tick["recorded_at_utc"]
        assert data["cold_phase"] == tick["phase"]
        assert data["fan_percent"] is None


# ------------------------------------------------------------------ T-I1

_FORBIDDEN: typing.Final = (
    "roastpilot_agent.cold_app",
    "roastpilot_agent.cold_observation_stream",
    "roastpilot_agent.api",
    "fastapi",
    "starlette",
)


def _forbidden(module: str | None) -> bool:
    return module is not None and any(
        module == name or module.startswith(f"{name}.") for name in _FORBIDDEN
    )


def forbidden_imports(package_root: Path, package_name: str = "roastpilot_agent") -> list[str]:
    """Files under ``cold_characterisation`` importing the app, stream, api or web stack."""
    offenders: list[str] = []
    cold = package_root / "cold_characterisation"
    for path in sorted(cold.rglob("*.py")):
        relative = path.relative_to(package_root).with_suffix("")
        # The importing module's package: the file's directory (``__init__`` included).
        package = ".".join([package_name, *relative.parts][:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(_forbidden(alias.name) for alias in node.names):
                    offenders.append(path.name)
            elif isinstance(node, ast.ImportFrom):
                base = _import_base(package, node, package_name)
                candidates = [base] + [
                    f"{base}.{alias.name}" for alias in node.names if base is not None
                ]
                if any(_forbidden(candidate) for candidate in candidates):
                    offenders.append(path.name)
    return offenders


def test_t_i1_the_cold_package_never_imports_the_app_stream_api_or_web_stack() -> None:
    """T-I1: no cold-package module imports cold_app, the stream, api, fastapi or starlette."""
    package_root = Path(cold_app.__file__).resolve().parent
    assert (package_root / "cold_characterisation" / "two_phase.py").is_file()
    assert forbidden_imports(package_root) == []


@pytest.mark.parametrize(
    "source",
    [
        "import roastpilot_agent.cold_app\n",
        "from roastpilot_agent.cold_observation_stream import ColdObservationHub\n",
        "from roastpilot_agent import api\n",
        "from ..cold_app import create_cold_app\n",
        "from .. import api\n",
        "from .. import cold_observation_stream as s\n",
        "import fastapi\n",
        "from starlette.types import Scope\n",
        "def f():\n    import starlette.responses\n",
    ],
)
def test_t_i1_negative_controls_are_detected(tmp_path: Path, source: str) -> None:
    """T-I1 controls: every absolute and relative forbidden import form is detected."""
    package_root = tmp_path / "roastpilot_agent"
    cold = package_root / "cold_characterisation"
    cold.mkdir(parents=True)
    (cold / "__init__.py").write_text("", encoding="utf-8")
    (cold / "clean.py").write_text("from . import evidence_schema\nimport json\n", encoding="utf-8")
    (cold / "bad.py").write_text(source, encoding="utf-8")
    assert forbidden_imports(package_root) == ["bad.py"]
