"""Application-owned identity, command uncertainty, and HUMAN call admission."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentAttempt,
    EndpointDisposition,
    EndpointOwner,
)


class HumanRunAdmission(Enum):
    """Construction-bound authority for routine HUMAN commands."""

    PREPARED = auto()
    ADMITTED = auto()
    SESSION_BOUND = auto()


class HumanSessionBinding(Enum):
    """Private handoff outcomes; entered-unbound never proves non-entry.

    NO_ISSUED_RECEIPT is receiver-scoped: an unrelated receiver cannot prove
    producer non-entry or release the original accepted attempt for retry.
    """

    BOUND = auto()
    NO_ISSUED_RECEIPT = auto()
    ENTERED_UNBOUND = auto()


class PendingHumanCall(Enum):
    """Operations that retain ownership until their local await terminates."""

    READ = auto()
    CONTAINMENT = auto()


class AdmissionCloseReason(Enum):
    """Application lifecycle transitions that close HUMAN call admission."""

    APPLICATION_TEARDOWN = auto()
    DEVICE_RESPAWN = auto()


class HumanCallRefusal(Enum):
    """Reasons a HUMAN adapter operation was refused before dispatch."""

    ADMISSION_CLOSED = auto()
    CHILD_TEARDOWN_UNCONFIRMED = auto()
    READ_ALREADY_PENDING = auto()


@dataclass(frozen=True)
class RefusedHumanCall:
    """A typed non-dispatch result, distinct from a failed or completed RPC."""

    kind: PendingHumanCall
    refusal: HumanCallRefusal


class HumanCallAdmission(Protocol):
    """The read-only admission capability shared with controllers."""

    @property
    def closed_reason(self) -> AdmissionCloseReason | None:
        """Return the current reason for closure, if any."""
        ...


class HumanCallAdmissionFence:
    """Own application teardown and respawn admission for one adapter."""

    def __init__(self) -> None:
        self._closed_reason: AdmissionCloseReason | None = None

    @property
    def closed_reason(self) -> AdmissionCloseReason | None:
        """Return the current reason for closure, if any."""
        return self._closed_reason

    def close(self, reason: AdmissionCloseReason) -> None:
        """Close immediately, preserving terminal application teardown."""
        if self._closed_reason is not AdmissionCloseReason.APPLICATION_TEARDOWN:
            self._closed_reason = reason

    def reopen_after_respawn(
        self,
        *,
        child_started: bool,
        teardown_unconfirmed: bool,
        lifecycle_blocked: bool,
        shutdown_started: bool,
    ) -> bool:
        """Reopen a respawn closure only after all lifecycle proofs hold."""
        if (
            self._closed_reason is not AdmissionCloseReason.DEVICE_RESPAWN
            or not child_started
            or teardown_unconfirmed
            or lifecycle_blocked
            or shutdown_started
        ):
            return False
        self._closed_reason = None
        return True


@dataclass
class HumanRunState:
    """Run-local command and exact-session ownership retained by the service."""

    run_id: str
    _admission: HumanRunAdmission = HumanRunAdmission.ADMITTED
    child_generation: int | None = None
    session_id: str | None = None
    endpoint: EndpointDisposition = EndpointDisposition.NONE
    endpoint_owner: EndpointOwner | None = None
    command: CommandDisposition = CommandDisposition.SETTLED
    containment: ContainmentAttempt = ContainmentAttempt.AVAILABLE
    durably_closed: bool = False
    _session_handoff_prepared: bool = False

    @property
    def admission(self) -> HumanRunAdmission:
        """Return authority, including the one-way private session binding."""
        return self._admission

    @property
    def unresolved(self) -> bool:
        """Return whether this run must block lifecycle replacement."""
        return (
            not self.durably_closed
            or self.command is CommandDisposition.UNKNOWN
            or self.endpoint
            in (
                EndpointDisposition.ADMITTED,
                EndpointDisposition.IN_FLIGHT,
                EndpointDisposition.UNKNOWN,
                EndpointDisposition.HISTORY_UNKNOWN,
            )
        )


class HumanRunOwnership:
    """Share pending-call exclusivity and run ownership across controllers."""

    def __init__(self) -> None:
        self._run: HumanRunState | None = None
        self._pending: set[PendingHumanCall] = set()

    @property
    def has_pending_call(self) -> bool:
        """Return whether any locally pending call owns the adapter."""
        return bool(self._pending)

    @property
    def lifecycle_blocked(self) -> bool:
        """Refuse replacement while a local call or unresolved run owns it."""
        return self.has_pending_call or (self._run is not None and self._run.unresolved)

    def claim_run(self, run_id: str) -> HumanRunState:
        """Create ownership for a run after the application admits it."""
        return self._claim_run(run_id, HumanRunAdmission.ADMITTED)

    def _claim_prepared_run(self, run_id: str) -> HumanRunState:
        """Claim a sessionless owner without routine command authority."""
        return self._claim_run(run_id, HumanRunAdmission.PREPARED)

    def _claim_run(self, run_id: str, admission: HumanRunAdmission) -> HumanRunState:
        if self.lifecycle_blocked:
            raise RuntimeError("unresolved HUMAN ownership")
        run = HumanRunState(run_id, _admission=admission)
        self._run = run
        return run

    def is_current(self, run: HumanRunState) -> bool:
        """Check ownership by object identity, including same-session ABA."""
        return self._run is run

    def allows_session_start(self, run: HumanRunState | None) -> bool:
        """Allow a fresh session only for its own current, untouched run."""
        if run is None:
            return not self.lifecycle_blocked
        return (
            self._run is run
            and not self._pending
            and not run.durably_closed
            and run.session_id is None
            and run.command is CommandDisposition.SETTLED
            and run.endpoint is EndpointDisposition.NONE
            and run.containment is ContainmentAttempt.AVAILABLE
        )

    def pending(self, kind: PendingHumanCall) -> bool:
        """Return whether this adapter already has a pending call of this kind."""
        return kind in self._pending

    @contextmanager
    def hold(self, kind: PendingHumanCall) -> Generator[None, None, None]:
        """Keep a marker until local termination, independent of remote state."""
        if kind in self._pending:
            raise RuntimeError("HUMAN call is already pending")
        self._pending.add(kind)
        try:
            yield
        finally:
            self._pending.remove(kind)
