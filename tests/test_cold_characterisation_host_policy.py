"""Pure host-policy tests and live-reader parity for the A2 retained-host rule (#954)."""

import ast
import math
import typing
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import host, host_policy
from roastpilot_agent.cold_characterisation.host import ColdHostBoundError, ColdHostBoundFailure
from tests.test_cold_characterisation_host import (
    RecordingRunner,
    _completed,  # pyright: ignore[reportPrivateUsage]
    _reader,  # pyright: ignore[reportPrivateUsage]
)

SOURCE = Path(host_policy.__file__).read_text(encoding="utf-8")
GUARDED_BITS = (0x1, 0x2, 0x4, 0x8, 0x10000, 0x20000, 0x40000, 0x80000)


def _failure(action: typing.Callable[[], object]) -> ColdHostBoundFailure | None:
    """Run one reader action; return its closed failure member, or ``None`` if admitted."""
    try:
        action()
    except ColdHostBoundError as error:
        return error.failure
    return None


# ----------------------------------------------------------- constants and fence


def test_constants_are_the_ac15_values_and_host_re_exports_them() -> None:
    """Every constant has its AC15 value and the reader binds the same objects."""
    assert host_policy.HOST_MAX_TEMP_C == 80.0 and type(host_policy.HOST_MAX_TEMP_C) is float
    assert host_policy.HOST_MIN_MEM_AVAILABLE_BYTES == 512 * 2**20
    assert host_policy.HOST_MIN_FREE_BYTES_BEFORE == 2 * 2**30
    assert host_policy.HOST_MIN_FREE_BYTES_DURING == 1 * 2**30
    assert host_policy.HOST_THROTTLE_FAILURE_MASK == 0x000F000F
    assert host.HOST_MAX_TEMP_C is host_policy.HOST_MAX_TEMP_C
    assert host.HOST_MIN_MEM_AVAILABLE_BYTES is host_policy.HOST_MIN_MEM_AVAILABLE_BYTES
    assert host.HOST_MIN_FREE_BYTES_BEFORE is host_policy.HOST_MIN_FREE_BYTES_BEFORE
    assert host.HOST_MIN_FREE_BYTES_DURING is host_policy.HOST_MIN_FREE_BYTES_DURING
    mask = host._THROTTLE_FAILURE_MASK  # pyright: ignore[reportPrivateUsage]
    assert mask is host_policy.HOST_THROTTLE_FAILURE_MASK


def test_host_policy_imports_only_math_re_and_typing() -> None:
    """The pure module has no I/O, provider, pydantic or project import."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(f"from:{node.module}")
    assert imported == {"math", "re", "typing"}


# ------------------------------------------------------------------- predicates


@pytest.mark.parametrize(
    ("value", "admitted"),
    [
        (math.nextafter(80.0, -math.inf), True),
        (-40.0, True),
        (80.0, False),
        (80.5, False),
        (math.nan, False),
        (-math.inf, False),
        (math.inf, False),
        (True, False),
        (45, False),
    ],
)
def test_soc_temp_below_limit(value: typing.Any, admitted: bool) -> None:
    """Finite exact floats below 80.0 only; no lower bound; no bool or int."""
    assert host_policy.soc_temp_below_limit(value) is admitted


@pytest.mark.parametrize(
    ("word", "clear"),
    [
        (0, True),
        (0xFFF0FFF0, True),
        (0x0FFF0FF0, False),  # digit five is 0xF: guarded bits 16-19 are set
        *[(bit, False) for bit in GUARDED_BITS],
        (0x000F000F, False),
        (True, False),
    ],
)
def test_throttle_word_is_clear(word: typing.Any, clear: bool) -> None:
    """Every guarded current and sticky bit refuses; unmasked bits are admitted."""
    assert host_policy.throttle_word_is_clear(word) is clear


_MEM = host_policy.HOST_MIN_MEM_AVAILABLE_BYTES
_RUN = host_policy.HOST_MIN_FREE_BYTES_DURING
_ANY_FLOAT: typing.Any = 1.0


def test_integer_predicates_are_exact_type_inclusive_floors() -> None:
    """Exact floors admit, floor minus one refuses, and non-int inputs refuse."""
    assert host_policy.mem_available_is_admitted(_MEM) is True
    assert host_policy.mem_available_is_admitted(_MEM - 1) is False
    assert host_policy.free_bytes_meets_floor(_RUN, _RUN) is True
    assert host_policy.free_bytes_meets_floor(_RUN - 1, _RUN) is False
    assert host_policy.mem_available_is_admitted(True) is False
    assert host_policy.mem_available_is_admitted(_ANY_FLOAT * _MEM) is False
    assert host_policy.free_bytes_meets_floor(True, 0) is False
    assert host_policy.free_bytes_meets_floor(_ANY_FLOAT * _RUN, _RUN) is False
    assert host_policy.free_bytes_meets_floor(_RUN, _ANY_FLOAT) is False


class _TextSubclass(str):
    """A ``str`` subclass the retained grammar refuses."""


@pytest.mark.parametrize(
    ("text", "word"),
    [("0x0", 0), ("0x00000000", 0), ("0x0fff0ff0", 0x0FFF0FF0), ("0xfff0fff0", 0xFFF0FFF0)],
)
def test_parser_admits_the_closed_retained_spelling(text: str, word: int) -> None:
    """Lowercase ``0x`` plus one to eight digits, leading zeros admitted."""
    assert host_policy.parse_retained_throttle_hex(text) == word


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0x",
        "0X0",
        "0xA",
        "0x000000000",
        "-0x1",
        "+0x1",
        " 0x0",
        "0x0\n",
        "0x١",
        _TextSubclass("0x0"),
        typing.cast(str, 0),
    ],
)
def test_parser_refuses_every_other_spelling(text: str) -> None:
    """Case, length, sign, whitespace, non-ASCII digits and subclasses all refuse."""
    assert host_policy.parse_retained_throttle_hex(text) is None


# ------------------------------------------------------- live reader parity


@pytest.mark.parametrize("millidegrees", ["79999", "80000"])
def test_thermal_parity_with_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, millidegrees: str
) -> None:
    """79.999 C is admitted and 80.000 C refused by the reader and the predicate alike."""
    reader = _reader(tmp_path, monkeypatch, thermal=f"{millidegrees}\n")
    value = int(millidegrees) / 1000.0
    expected = (
        None if host_policy.soc_temp_below_limit(value) else ColdHostBoundFailure.THERMAL_EXCEEDED
    )
    assert _failure(reader.read_thermal_c) is expected
    assert expected is (None if millidegrees == "79999" else ColdHostBoundFailure.THERMAL_EXCEEDED)


@pytest.mark.parametrize("word", [*GUARDED_BITS, 0x10])
def test_throttle_parity_with_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    """Each guarded bit refuses and the unmasked 0x10 is admitted, in both paths."""
    runner = RecordingRunner(_completed(stdout=f"throttled={word:#x}\n".encode()))
    reader = _reader(tmp_path, monkeypatch, runner=runner)
    expected = (
        None if host_policy.throttle_word_is_clear(word) else ColdHostBoundFailure.THROTTLE_BITS_SET
    )
    assert _failure(reader.read_throttled_word) is expected
    assert (expected is None) is (word == 0x10)


@pytest.mark.parametrize("delta_kib", [-1, 0, 1])
def test_memory_parity_with_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta_kib: int
) -> None:
    """The reader sees integral KiB: floor and the nearest KiB below and above."""
    kib = _MEM // 1024 + delta_kib
    reader = _reader(tmp_path, monkeypatch, meminfo=f"MemAvailable: {kib} kB\n")
    admitted = host_policy.mem_available_is_admitted(kib * 1024)
    expected = None if admitted else ColdHostBoundFailure.MEMINFO_BELOW_BOUND
    assert _failure(reader.read_mem_available_bytes) is expected
    assert admitted is (delta_kib >= 0)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_run_disk_parity_with_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    """The during-run floor, through both ``check_run_bounds`` and ``sample``."""
    free = _RUN + delta
    reader = _reader(tmp_path, monkeypatch, free_bytes=free)
    admitted = host_policy.free_bytes_meets_floor(free, _RUN)
    expected = None if admitted else ColdHostBoundFailure.DISK_BELOW_RUN_BOUND
    assert _failure(lambda: reader.check_run_bounds(tmp_path)) is expected
    assert _failure(lambda: reader.sample(tmp_path)) is expected
    assert admitted is (delta >= 0)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_start_disk_parity_with_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    """The start floor applies only through ``check_start_bounds``."""
    start = host_policy.HOST_MIN_FREE_BYTES_BEFORE
    reader = _reader(tmp_path, monkeypatch, free_bytes=start + delta)
    admitted = host_policy.free_bytes_meets_floor(start + delta, start)
    expected = None if admitted else ColdHostBoundFailure.DISK_BELOW_START_BOUND
    assert _failure(lambda: reader.check_start_bounds(tmp_path)) is expected
    assert admitted is (delta >= 0)


def test_uppercase_raw_unmasked_word_is_retained_lowercase_and_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw uppercase output is lowercased; the retained spelling parses and is clear."""
    runner = RecordingRunner(_completed(stdout=b"throttled=0x000000A0\n"))
    reader = _reader(tmp_path, monkeypatch, runner=runner)
    retained = reader.sample(tmp_path).throttled_word_hex
    assert retained == "0x000000a0"
    word = host_policy.parse_retained_throttle_hex(retained)
    assert word == 0xA0 and host_policy.throttle_word_is_clear(word)


def test_uppercase_raw_masked_word_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw uppercase output with a guarded bit still raises ``THROTTLE_BITS_SET``."""
    runner = RecordingRunner(_completed(stdout=b"throttled=0x0000000A\n"))
    reader = _reader(tmp_path, monkeypatch, runner=runner)
    assert _failure(reader.read_throttled_word) is ColdHostBoundFailure.THROTTLE_BITS_SET
