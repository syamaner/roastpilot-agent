"""Closed cold-duration generations, independent of execution and infrastructure."""

import enum
import math
import typing

HISTORICAL_PHASE_SECONDS: typing.Final = 1800.0
CURRENT_PHASE_SECONDS: typing.Final = 600.0


class ColdDurationGeneration(enum.Enum):
    """The two admitted retained activation relations."""

    HISTORICAL = "historical_30_30"
    D210 = "d210_10_10"


def admit_duration_generation(
    activation: object, scheduled_end: object
) -> ColdDurationGeneration | None:
    """Admit only exact finite float activation-plus-duration relations.

    Args:
        activation: Retained activation instant.
        scheduled_end: Retained scheduled end instant.

    Returns:
        The closed generation, or None for an unknown or malformed relation.
    """
    if type(activation) is not float or type(scheduled_end) is not float:
        return None
    if not math.isfinite(activation) or not math.isfinite(scheduled_end):
        return None
    if activation < 0.0 or scheduled_end < 0.0:
        return None
    historical = scheduled_end == activation + HISTORICAL_PHASE_SECONDS
    current = scheduled_end == activation + CURRENT_PHASE_SECONDS
    # Very large finite instants can collapse both additions to the same float.
    if historical == current:
        return None
    return ColdDurationGeneration.HISTORICAL if historical else ColdDurationGeneration.D210
