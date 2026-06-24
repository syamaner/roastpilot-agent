"""Shared roast-degree classification for the roast-data pipeline (#300, D44).

A single pure function the fixture converters (``alog_to_fixture`` and
``store_to_fixture``) both import, so the over-done line is defined once rather
than re-encoded per converter. The thresholds match the operator's known-good
medium line and the offline ``.alog`` analysis (``scripts/alog_classify.py``
``_OVER_DONE_C`` = 197 °C; ``docs/research/hottop-alog-classification-2026-06-20.md``):

- drop ≤ 195.0 °C  → ``core_medium`` (the known-good core mediums);
- 195.0 < drop ≤ 197.0 °C → ``soft_medium`` (the soft 196–197 °C band);
- drop > 197.0 °C → ``over`` (the operative over-done line).

Temperatures are the Hottop **display** bean probe in °C — the same scale the
operator's ceilings are expressed in. This is a coarse degree label off the drop
temperature alone; it does not attempt the second-crack / k-means medium-vs-dark
split that the offline analysis (``alog_classify.classify_degrees``) does, which
needs the full RoR profile. It is the outcome label the bake-off fixtures carry.
"""

from __future__ import annotations

from typing import Literal

#: The known-good-medium core/soft boundary (display bean °C). Drops at or below
#: this are the operator's core mediums.
CORE_MEDIUM_MAX_C = 195.0
#: The operative over-done line (display bean °C), matching
#: ``alog_classify._OVER_DONE_C`` (set 20 Jun 2026). Drops above this are over.
OVER_DONE_C = 197.0

#: The degree label vocabulary, in increasing-drop order.
RoastDegree = Literal["core_medium", "soft_medium", "over"]


def classify_degree(drop_temp_c: float) -> RoastDegree:
    """Classify a roast's degree from its drop bean temperature.

    Args:
        drop_temp_c: The display bean temperature (°C) at the drop.

    Returns:
        ``"core_medium"`` for drop ≤ 195.0 °C, ``"soft_medium"`` for
        195.0 < drop ≤ 197.0 °C, ``"over"`` for drop > 197.0 °C.
    """
    if drop_temp_c <= CORE_MEDIUM_MAX_C:
        return "core_medium"
    if drop_temp_c <= OVER_DONE_C:
        return "soft_medium"
    return "over"
