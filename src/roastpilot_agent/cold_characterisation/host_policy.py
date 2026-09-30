"""Pure cold-characterisation host-bound policy shared by the reader and checker.

This module owns the AC15 host bounds (D183, D194) and the predicates that apply
them.  It performs no I/O and imports only the standard library, so the live Linux
reader and the retained-evidence conformance checker apply one identical policy.
Every predicate is total: it returns ``False`` (or ``None``) instead of raising.
"""

import math
import re
import typing

HOST_MAX_TEMP_C: typing.Final = 80.0
HOST_MIN_MEM_AVAILABLE_BYTES: typing.Final = 512 * 2**20
HOST_MIN_FREE_BYTES_BEFORE: typing.Final = 2 * 2**30
HOST_MIN_FREE_BYTES_DURING: typing.Final = 1 * 2**30
HOST_THROTTLE_FAILURE_MASK: typing.Final = 0x000F000F

_MAX_RETAINED_THROTTLE_CHARACTERS: typing.Final = 10
_RETAINED_THROTTLE_PATTERN: typing.Final = re.compile(r"\A0x[0-9a-f]{1,8}\Z")


def soc_temp_below_limit(value: float) -> bool:
    """Whether a system-on-chip temperature is admitted.

    The finite check is a precondition matching the evidence schema's existing
    finite rule; it is not a lower bound, and negative finite values are admitted.

    Args:
        value: Temperature in Celsius.

    Returns:
        ``True`` only for an exact finite ``float`` below ``HOST_MAX_TEMP_C``.
    """
    return type(value) is float and math.isfinite(value) and value < HOST_MAX_TEMP_C


def throttle_word_is_clear(word: int) -> bool:
    """Whether no current or sticky guarded throttle bit is set.

    The word is assumed non-negative and at most 32 bits, as produced by the live
    reader or by ``parse_retained_throttle_hex``; no separate policy is added for
    negative words, which neither producer can represent.  Unmasked bits are admitted.

    Args:
        word: The throttle word.

    Returns:
        ``True`` only for an exact ``int`` with no ``HOST_THROTTLE_FAILURE_MASK`` bit.
    """
    return type(word) is int and (word & HOST_THROTTLE_FAILURE_MASK) == 0


def mem_available_is_admitted(value: int) -> bool:
    """Whether available memory meets the AC15 floor.

    Args:
        value: Available memory in bytes.

    Returns:
        ``True`` only for an exact ``int`` of at least ``HOST_MIN_MEM_AVAILABLE_BYTES``.
    """
    return type(value) is int and value >= HOST_MIN_MEM_AVAILABLE_BYTES


def free_bytes_meets_floor(value: int, floor: int) -> bool:
    """Whether free disk space meets a supplied floor.

    Args:
        value: Free unprivileged bytes.
        floor: The start or during-run floor in bytes.

    Returns:
        ``True`` only when both are exact ``int`` and ``value >= floor``.
    """
    return type(value) is int and type(floor) is int and value >= floor


def parse_retained_throttle_hex(text: str) -> int | None:
    """Parse the closed retained throttle spelling.

    The retained grammar is ``0x`` plus one to eight lowercase hexadecimal digits,
    leading zeros admitted.  It is deliberately stricter than the raw command
    output, which the live reader accepts in either case and retains lowercased.

    Args:
        text: The retained ``throttled_word_hex`` value.

    Returns:
        The word, or ``None`` for any other input, including ``str`` subclasses.
    """
    if type(text) is not str or len(text) > _MAX_RETAINED_THROTTLE_CHARACTERS:
        return None
    if _RETAINED_THROTTLE_PATTERN.fullmatch(text) is None:
        return None
    return int(text[2:], 16)
