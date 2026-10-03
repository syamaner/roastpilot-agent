"""Closed public projection and bounded hub for cold-characterisation observations.

A retained cold tick carries private run, session, driver, vendor and audio data
plus an identity digest.  Only the closed, versioned eleven-field
:class:`ColdObservationData` envelope projected here may leave the process; it is
rendered once into an SSE frame and no reference to the tick is retained.

Temperatures are Celsius, exact finite floats or ``null``; finiteness is not a
plausibility claim and no range is applied.  ``fan_percent`` (the main fan) is
always ``null``: per-tick observation covers heat, roast fan and cooling only.

:class:`ColdObservationHub` is bounded everywhere: admission, per-subscriber
queues, the replay ring and ``Last-Event-ID`` parsing.  Over a limit it refuses
immediately and never waits.  It reads no client-supplied identity.  Exactly two
frame kinds exist: ``observation`` and ``heartbeat``.  A heartbeat is transport
evidence only, never a freshness, continuity or watchdog signal, and frame IDs are
opaque.  Nothing here logs or formats a tick, an identity or an input value.
"""

import asyncio
import collections
import enum
import json
import math
import re
import secrets
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_lifecycle import is_admissible_utc_instant
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdPhaseKind,
    ColdTickRecord,
    ColdTickRoastFanOutcome,
)

#: The largest exact JSON integer magnitude; a wire-exactness bound, not plausibility.
_MAX_EXACT_INT: typing.Final = 2**53 - 1
_LAST_EVENT_ID_MAX_CHARS: typing.Final = 33
_LAST_EVENT_ID_PATTERN: typing.Final = re.compile(r"[0-9a-f]{16}-[1-9][0-9]{0,15}", re.ASCII)
_EPOCH_PATTERN: typing.Final = re.compile(r"[0-9a-f]{16}", re.ASCII)


class ColdStreamFrameKind(enum.Enum):
    """The two closed cold stream frame kinds."""

    OBSERVATION = "observation"
    HEARTBEAT = "heartbeat"


class ColdStreamPhase(enum.Enum):
    """The public cold phase vocabulary (mapped from the evidence phase by identity)."""

    RECORDING_OFF = "recording_off"
    RECORDING_ON = "recording_on"


class ColdStreamRoastFanOutcome(enum.Enum):
    """The public commanded roast-fan read outcome (mapped by identity)."""

    OBSERVED = "observed"
    NOT_ELIGIBLE = "not_eligible"
    UNSUPPORTED = "unsupported"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"


_PHASES: typing.Final[tuple[tuple[ColdPhaseKind, ColdStreamPhase], ...]] = (
    (ColdPhaseKind.RECORDING_OFF, ColdStreamPhase.RECORDING_OFF),
    (ColdPhaseKind.RECORDING_ON, ColdStreamPhase.RECORDING_ON),
)
_OUTCOMES: typing.Final[tuple[tuple[ColdTickRoastFanOutcome, ColdStreamRoastFanOutcome], ...]] = (
    (ColdTickRoastFanOutcome.OBSERVED, ColdStreamRoastFanOutcome.OBSERVED),
    (ColdTickRoastFanOutcome.NOT_ELIGIBLE, ColdStreamRoastFanOutcome.NOT_ELIGIBLE),
    (ColdTickRoastFanOutcome.UNSUPPORTED, ColdStreamRoastFanOutcome.UNSUPPORTED),
    (ColdTickRoastFanOutcome.UNREADABLE, ColdStreamRoastFanOutcome.UNREADABLE),
    (ColdTickRoastFanOutcome.MALFORMED, ColdStreamRoastFanOutcome.MALFORMED),
)


class ColdObservationData(pydantic.BaseModel):
    """The closed public projection of one retained cold tick (schema version 1).

    ``heat_percent`` and ``cooling_on`` are the commanded state reported by MCP
    (D197), not physical measurements; ``roast_fan_percent`` is commanded and is
    present only when the read outcome is ``observed``.  ``fan_percent`` is always
    ``None``.  ``recorded_at_utc`` is the tick's retained UTC instant.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    schema_version: typing.Literal[1]
    cold_phase: ColdStreamPhase
    recorded_at_utc: str
    device_reported: bool
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_percent: int | None
    fan_percent: None
    roast_fan_outcome: ColdStreamRoastFanOutcome
    roast_fan_percent: int | None
    cooling_on: bool | None


class ColdObservationProjectionError(RuntimeError):
    """Fixed refusal of one projection; carries nothing from its input."""

    def __init__(self) -> None:
        """Create the fixed refusal."""
        super().__init__("Cold observation projection refused.")


def _finite(value: object) -> float | None:
    """An exact finite float, else ``None`` (finiteness is not plausibility)."""
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _exact_int(value: object, low: int, high: int) -> int | None:
    """An exact ``int`` (never ``bool``) within ``[low, high]``, else ``None``."""
    if type(value) is int and low <= value <= high:
        return value
    return None


def _project(tick: ColdTickRecord) -> ColdObservationData | None:
    """Read only the published fields of one exact tick; ``None`` when refused."""
    if type(tick) is not ColdTickRecord:
        return None
    phase = next((public for kind, public in _PHASES if tick.phase is kind), None)
    roast_fan = tick.roast_fan
    outcome = next((public for kind, public in _OUTCOMES if roast_fan.outcome is kind), None)
    utc: object = tick.recorded_at_utc
    if phase is None or outcome is None or not is_admissible_utc_instant(utc):
        return None
    level = (
        _exact_int(roast_fan.roast_fan_level_percent, 0, 100)
        if outcome is ColdStreamRoastFanOutcome.OBSERVED
        else None
    )
    device = tick.device
    bean = env = None
    heat: int | None = None
    cooling: bool | None = None
    if device is not None:
        bean = _finite(device.bean_temp_c)
        env = _finite(device.env_temp_c)
        heat = _exact_int(device.heat_level_percent, -_MAX_EXACT_INT, _MAX_EXACT_INT)
        cooling_value: object = device.cooling_on
        cooling = cooling_value if type(cooling_value) is bool else None
    return ColdObservationData(
        schema_version=1,
        cold_phase=phase,
        recorded_at_utc=utc,
        device_reported=device is not None,
        bean_temp_c=bean,
        env_temp_c=env,
        heat_percent=heat,
        fan_percent=None,
        roast_fan_outcome=outcome,
        roast_fan_percent=level,
        cooling_on=cooling,
    )


def project_cold_observation(tick: ColdTickRecord) -> ColdObservationData:
    """Project one retained tick into the closed public envelope.

    Never reads the run, identity digest, session, audio, raw audio extra, driver,
    connection, vendor data or main-fan level.

    Args:
        tick: One exact retained tick record.

    Returns:
        The closed eleven-field projection.

    Raises:
        ColdObservationProjectionError: If the input is not an exact tick, a closed
            mapping is missing, or its UTC instant is inadmissible (fixed text).
    """
    data: ColdObservationData | None
    try:
        data = _project(tick)
    except Exception:
        data = None
    if data is None:
        raise ColdObservationProjectionError
    return data


def render_observation_frame(epoch: str, sequence: int, data: ColdObservationData) -> str:
    """Render one observation SSE frame with a strict, canonical JSON body.

    Args:
        epoch: The hub's opaque 16-hex-digit epoch.
        sequence: The positive frame sequence within the epoch.
        data: The closed projection.

    Returns:
        ``id: {epoch}-{sequence}\\nevent: observation\\ndata: {json}\\n\\n``.
    """
    body = json.dumps(
        data.model_dump(mode="json"),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    kind = ColdStreamFrameKind.OBSERVATION.value
    return f"id: {epoch}-{sequence}\nevent: {kind}\ndata: {body}\n\n"


#: The exact heartbeat frame: transport evidence only, with no ID.
HEARTBEAT_FRAME: typing.Final = f"event: {ColdStreamFrameKind.HEARTBEAT.value}\ndata: {{}}\n\n"


def parse_last_event_id(raw: object) -> tuple[str, int] | None:
    """Parse one opaque cold ``Last-Event-ID``; anything else is a fresh connection.

    Accepted only as an exact ``str`` of at most 33 characters fully matching
    ``[0-9a-f]{16}-[1-9][0-9]{0,15}`` (ASCII).  The value is never logged or echoed.

    Args:
        raw: The untrusted header or query value.

    Returns:
        ``(epoch, sequence)``, or ``None``.
    """
    if type(raw) is not str or len(raw) > _LAST_EVENT_ID_MAX_CHARS:
        return None
    if _LAST_EVENT_ID_PATTERN.fullmatch(raw) is None:
        return None
    epoch, sequence = raw.split("-")
    return epoch, int(sequence)


class ColdSubscription:
    """One bounded subscriber queue; ``None`` from :meth:`get` ends the stream."""

    def __init__(self, maxsize: int) -> None:
        """Create the queue (one slot is reserved for the end sentinel)."""
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=maxsize)

    async def get(self) -> str | None:
        """Return the next rendered frame, or ``None`` at the end of the stream."""
        return await self.queue.get()


class ColdObservationHub:
    """Bounded fan-out of rendered cold observation frames.

    For a single event loop; not thread-safe.  ``publish`` is synchronous and is
    the runtime's display-only tick observer: a projection error propagates (and
    fails the run).  Admission, queues and the replay ring are bounded; over a
    limit the hub refuses immediately.  A subscriber whose queue already holds
    ``queue_frames`` frames is sent the end sentinel and removed, while frames it
    already holds are still delivered.
    """

    def __init__(
        self,
        *,
        max_subscribers: int = 4,
        queue_frames: int = 64,
        ring_frames: int = 64,
        epoch: str | None = None,
    ) -> None:
        """Create the hub.

        Args:
            max_subscribers: The admission limit (at least 1).
            queue_frames: Frames each subscriber may hold pending.
            ring_frames: Frames retained for resume (``1 <= ring <= queue``).
            epoch: An explicit 16-hex-digit epoch; random when ``None``.

        Raises:
            ValueError: On any refused bound or epoch (fixed text).
        """
        if not (1 <= ring_frames <= queue_frames):
            raise ValueError("Cold observation hub ring must be within 1..queue_frames.")
        if max_subscribers < 1:
            raise ValueError("Cold observation hub needs at least one subscriber slot.")
        if epoch is not None and (
            type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None
        ):
            raise ValueError("Cold observation hub epoch refused.")
        self._max_subscribers = max_subscribers
        self._queue_frames = queue_frames
        self._epoch = secrets.token_hex(8) if epoch is None else epoch
        self._ring: collections.deque[tuple[int, str]] = collections.deque(maxlen=ring_frames)
        self._subscribers: list[ColdSubscription] = []
        self._sequence = 0
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        """The number of admitted subscribers."""
        return len(self._subscribers)

    @property
    def closed(self) -> bool:
        """Whether the hub has been closed."""
        return self._closed

    def admission_available(self) -> bool:
        """Whether a new subscriber would be admitted now."""
        return not self._closed and len(self._subscribers) < self._max_subscribers

    def publish(self, tick: ColdTickRecord) -> None:
        """Project, render, retain and fan out one tick; a no-op after close.

        Raises:
            ColdObservationProjectionError: If the tick cannot be projected.
        """
        if self._closed:
            return
        data = project_cold_observation(tick)
        self._sequence += 1
        frame = render_observation_frame(self._epoch, self._sequence, data)
        self._ring.append((self._sequence, frame))
        for subscription in tuple(self._subscribers):
            if subscription.queue.qsize() >= self._queue_frames:
                subscription.queue.put_nowait(None)
                self._remove(subscription)
            else:
                subscription.queue.put_nowait(frame)

    def _replay(self, last_event_id: object) -> tuple[str, ...]:
        """The frames after a resumable in-epoch ID still in the ring, else none."""
        parsed = parse_last_event_id(last_event_id)
        if parsed is None or not self._ring:
            return ()
        epoch, sequence = parsed
        oldest = self._ring[0][0]
        if epoch != self._epoch or sequence >= self._sequence or sequence < oldest - 1:
            return ()
        return tuple(frame for held, frame in self._ring if held > sequence)

    def subscribe(self, last_event_id: object = None) -> ColdSubscription | None:
        """Admit one subscriber with any resumable replay, or ``None`` immediately.

        Args:
            last_event_id: The untrusted resume ID; malformed means fresh.

        Returns:
            The subscription, or ``None`` when closed or full.
        """
        if not self.admission_available():
            return None
        subscription = ColdSubscription(self._queue_frames + 1)
        for frame in self._replay(last_event_id):
            subscription.queue.put_nowait(frame)
        self._subscribers.append(subscription)
        return subscription

    def _remove(self, subscription: ColdSubscription) -> None:
        """Remove one subscription by identity, if present."""
        self._subscribers = [held for held in self._subscribers if held is not subscription]

    def unsubscribe(self, subscription: ColdSubscription) -> None:
        """Release one subscription; idempotent."""
        self._remove(subscription)

    def close(self) -> None:
        """Close the hub and send the end sentinel to every subscriber."""
        self._closed = True
        subscribers, self._subscribers = self._subscribers, []
        for subscription in subscribers:
            subscription.queue.put_nowait(None)
