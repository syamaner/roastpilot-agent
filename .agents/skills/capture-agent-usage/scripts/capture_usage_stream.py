"""Bounded binary JSONL ingestion shared by local harness usage parsers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO

MAX_EVENT_BYTES = 65_536
"""Largest accepted JSONL event, limiting one allocation to 64 KiB."""
MAX_EVENT_COUNT = 10_000
"""Largest accepted event count, far above normal task streams."""
MAX_STREAM_BYTES = 1_048_576
"""Largest accepted total stream size, limiting hostile or endless output."""


class BoundedStreamError(ValueError):
    """Raised when a binary JSONL stream exceeds the closed ingestion grammar."""


def bounded_jsonl_lines(stream: BinaryIO) -> Iterator[str]:
    """Yield complete UTF-8 JSONL lines under fixed byte and count limits.

    Args:
        stream: A binary file-like stream, including a fixed subprocess stdout pipe.

    Raises:
        BoundedStreamError: If a line, stream, or encoding violates the fixed limits.
    """
    event_count = 0
    total_bytes = 0
    while True:
        raw_line = stream.readline(MAX_EVENT_BYTES + 1)
        if raw_line == b"":
            return
        if len(raw_line) > MAX_EVENT_BYTES:
            raise BoundedStreamError("usage stream event exceeds size limit")
        if not raw_line.endswith(b"\n"):
            raise BoundedStreamError("usage stream contains a partial event")
        event_count += 1
        if event_count > MAX_EVENT_COUNT:
            raise BoundedStreamError("usage stream exceeds event count limit")
        total_bytes += len(raw_line)
        if total_bytes > MAX_STREAM_BYTES:
            raise BoundedStreamError("usage stream exceeds total byte limit")
        try:
            yield raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BoundedStreamError("usage stream contains invalid UTF-8") from exc
