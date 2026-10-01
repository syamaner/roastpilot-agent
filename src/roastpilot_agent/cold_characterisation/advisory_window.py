"""Fixed advisory-window geometry for cold characterisation (#954, D199/D200).

This module is policy geometry only.  It derives the advisory window
``[scheduled_end - 360 s, scheduled_end - 60 s]`` from a retained scheduled phase
end and names the D199 invocation allowance and the minimum post-completion dwell.
The geometry is not provenance or execution proof: it says nothing about whether a
call was issued, when, or by whom.  It is not a timeout, grace period or watchdog,
and the 60-second end margin is not a stop guarantee.  The per-call bound and the
post-completion dwell stay configured per run and are compared as retained; only
the dwell's 5.0-second floor is named here.  It performs no I/O and reads no clock.
"""

import math
import typing

__all__ = (
    "ADVISORY_INVOCATION_ALLOWANCE_SECONDS",
    "ADVISORY_WINDOW_END_MARGIN_SECONDS",
    "ADVISORY_WINDOW_SECONDS",
    "MIN_POST_COMPLETION_DWELL_SECONDS",
    "advisory_window_bounds",
)

#: D200: the advisory window is 300 seconds long.
ADVISORY_WINDOW_SECONDS: typing.Final = 300.0
#: D200: the window closes 60 seconds before the scheduled phase end; not a stop guarantee.
ADVISORY_WINDOW_END_MARGIN_SECONDS: typing.Final = 60.0
#: D199 OD3: each actual invocation may lag its due instant by at most one second.
ADVISORY_INVOCATION_ALLOWANCE_SECONDS: typing.Final = 1.0
#: The production post-completion dwell default and its floor (parent contract G21).
MIN_POST_COMPLETION_DWELL_SECONDS: typing.Final = 5.0


def advisory_window_bounds(scheduled_end_monotonic: float) -> tuple[float, float]:
    """Return the inclusive advisory window for one retained scheduled phase end.

    Args:
        scheduled_end_monotonic: The retained ``PHASE_ACTIVATED`` scheduled end.

    Returns:
        ``(open, close)``: 360 and 60 seconds before the scheduled end.

    Raises:
        ValueError: If the argument is not an exact finite ``float``.
    """
    if type(scheduled_end_monotonic) is not float or not math.isfinite(scheduled_end_monotonic):
        raise ValueError("scheduled end must be an exact finite float")
    return (
        scheduled_end_monotonic - (ADVISORY_WINDOW_SECONDS + ADVISORY_WINDOW_END_MARGIN_SECONDS),
        scheduled_end_monotonic - ADVISORY_WINDOW_END_MARGIN_SECONDS,
    )
