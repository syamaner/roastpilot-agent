"""Exact, bounded projection between synthetic evidence clocks."""

from fractions import Fraction
from typing import cast

from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AlignmentAnchor,
    ClockName,
    Instant,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    MAX_COORDINATE,
    CheckReason,
    ClockScale,
)


class _ClockEdge:
    """An anchored affine mapping in the rate's declared source coordinates."""

    def __init__(self, anchor: AlignmentAnchor, scale: ClockScale | None, rate: int | None) -> None:
        source, target = anchor.source_clock, anchor.target_clock
        source_value, target_value = anchor.source_value, anchor.target_value
        if scale is not None and source is not scale.source_clock:
            source, target = target, source
            source_value, target_value = target_value, source_value
        self.source = source
        self.target = target
        self.source_value = Fraction(source_value)
        self.target_value = Fraction(target_value)
        if scale is not None:
            self.scale = Fraction(scale.target_per_source)
        else:
            sample_rate = cast(int, rate)  # Sample edges exist only with a bound rate.
            self.scale = (
                Fraction(1, sample_rate)
                if source is ClockName.RECORDING_SAMPLES
                else Fraction(sample_rate)
            )
        self.validity = None if scale is None else (scale.valid_from, scale.valid_to)

    def project(self, source: ClockName, point: Fraction) -> tuple[ClockName, Fraction] | None:
        if source is self.source:
            source_point = point
            destination = self.target
            projection = self.target_value + (point - self.source_value) * self.scale
        else:
            source_point = self.source_value + (point - self.target_value) / self.scale
            destination = self.source
            projection = source_point
        if self.validity is not None:
            low, high = self.validity
            if not (low <= self.source_value <= high and low <= source_point <= high):
                return None
        return destination, projection


class ClockProjection:
    """Resolve a fixed target through all usable simple clock paths."""

    def __init__(
        self, anchors: tuple[AlignmentAnchor, ...], scales: tuple[ClockScale, ...], rate: int | None
    ) -> None:
        self._rate = rate
        self.edges: dict[ClockName, list[_ClockEdge]] = {clock: [] for clock in ClockName}
        scales_by_pair = {
            frozenset((scale.source_clock, scale.target_clock)): scale for scale in scales
        }
        for anchor in anchors:
            pair = frozenset((anchor.source_clock, anchor.target_clock))
            scale = scales_by_pair.get(pair)
            if scale is None and (ClockName.RECORDING_SAMPLES not in pair or self.rate is None):
                continue
            edge = _ClockEdge(anchor, scale, self.rate)
            self.edges[edge.source].append(edge)
            self.edges[edge.target].append(edge)

    @property
    def rate(self) -> int | None:
        """Return the supplied sample-clock rate."""
        return self._rate

    def point(self, instant: Instant, target: ClockName) -> Fraction | CheckReason:
        """Project an instant exactly or return a typed clock failure.

        Args:
            instant: Original instant in its admitted source clock.
            target: Clock in which the point must be compared.

        Returns:
            A coordinate supported by all usable paths, or the applicable clock reason.
        """
        if instant.value > MAX_COORDINATE:
            return CheckReason.INSTANT_OUT_OF_DOMAIN
        if instant.clock is target:
            return Fraction(instant.value)

        values: set[Fraction] = set()
        paths = [(instant.clock, Fraction(instant.value), frozenset((instant.clock,)), False)]
        while paths:
            clock, point, visited, out_of_domain = paths.pop()
            for edge in self.edges[clock]:
                neighbor = edge.target if clock is edge.source else edge.source
                if neighbor in visited:
                    continue
                step = edge.project(clock, point)
                if step is None:
                    continue
                destination, projected = step
                outside = out_of_domain or not (
                    0 <= projected <= MAX_COORDINATE
                    and edge.source_value <= MAX_COORDINATE
                    and edge.target_value <= MAX_COORDINATE
                )
                if destination is target:
                    if outside:
                        return CheckReason.INSTANT_OUT_OF_DOMAIN
                    values.add(projected)
                else:
                    paths.append((destination, projected, visited | {destination}, outside))
        if not values:
            return CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
        if len(values) > 1:
            return CheckReason.CLOCK_PATH_CONFLICT
        return values.pop()
