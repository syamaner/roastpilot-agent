"""Shared read-only SQLite snapshot/URI helpers (#726 item 3).

Consolidates the read-only ``sqlite3`` URI construction and the "copy the
operator's live store to a private temp file" pattern that several offline
scripts (``rpd_corpus_score.py``, ``bakeoff_reference_567.py``,
``store_to_fixture.py``, ``plant_model_arx_study.py``) each re-implemented
slightly differently. Three of those copies (``bakeoff_reference_567.py``,
``store_to_fixture.py``, ``plant_model_arx_study.py``) built the read-only URI
with a naive ``f"file:{path}?mode=ro"`` — SQLite's URI filenames follow RFC
3986, so an unescaped ``?``/``#`` (or a relative path) in ``path`` is
misparsed, silently truncating or corrupting the path SQLite actually opens.
The fourth (``rpd_corpus_score.py``) already had the correct pattern. This
module adopts that already-correct pattern (``path.resolve().as_uri()``,
which percent-encodes per RFC 3986) as the single source of truth.

Every function here is strictly read-only against the caller-supplied
``db_path`` / ``store_path``: nothing in this module ever opens the live
operator store for writing, and :func:`snapshot_store_to_temp` is the only
function that performs a write, and only to a caller-owned temp-directory
target it constructs itself.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Final

#: The default snapshot filename used when a caller does not need a custom one.
DEFAULT_SNAPSHOT_NAME: Final[str] = "store-snapshot.sqlite3"


def _aliases_same_file(store_path: Path, snapshot_path: Path) -> bool:
    """Return ``True`` if ``snapshot_path`` would write through to ``store_path``.

    Two distinct filesystem paths can name the same underlying file. This
    checks both mechanisms a caller-supplied ``tmp_dir``/``snapshot_name``
    combination could use to alias the source store:

    * Resolved-path equality — covers the literal same path *and* the case
      where ``snapshot_path`` already exists as a symlink (directly or via
      an intermediate symlinked directory) that ultimately resolves to
      ``store_path``.
    * Matching ``(st_dev, st_ino)`` — covers a pre-existing hard link at
      ``snapshot_path`` sharing an inode with ``store_path``; hard links are
      never resolved by :meth:`~pathlib.Path.resolve`, so this check is
      required in addition to (not instead of) the resolved-path check.

    Args:
        store_path: The caller-supplied source database.
        snapshot_path: The computed target path the backup would open for
            writing.

    Returns:
        ``True`` if writing to ``snapshot_path`` would mutate ``store_path``.
    """
    try:
        if store_path.resolve() == snapshot_path.resolve():
            return True
        source_stat = store_path.stat()
        target_stat = snapshot_path.stat()
    except FileNotFoundError:
        # Either path does not exist (yet) as a real filesystem entry, so it
        # cannot be a hard link to the other — the resolved-path check above
        # already covers every case reachable without both paths existing.
        return False
    except OSError as error:
        raise ValueError(
            f"cannot safely inspect source {store_path} or snapshot target {snapshot_path}"
        ) from error
    return (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino)


def _reject_existing_snapshot_symlink(snapshot_path: Path) -> None:
    """Reject a pre-existing target symlink before opening either database."""
    try:
        target_stat = snapshot_path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"cannot safely inspect snapshot target {snapshot_path}") from error
    if not stat.S_ISLNK(target_stat.st_mode):
        return
    raise ValueError(
        f"snapshot target {snapshot_path} aliases the source store or another file "
        "through a symlink; "
        "refusing to open it"
    )


def _write_snapshot_atomically(snapshot_path: Path, snapshot_bytes: bytes) -> None:
    """Publish snapshot bytes without opening the caller-visible path for writing."""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=snapshot_path.parent,
            prefix=f".{snapshot_path.name}.",
            suffix=".tmp",
        )
    except FileNotFoundError as error:
        raise sqlite3.OperationalError("unable to open database file") from error
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(snapshot_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, snapshot_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_only_sqlite_uri(path: Path) -> str:
    """Build a percent-encoded, read-only ``sqlite3`` URI for ``path``.

    A raw ``f"file:{path}?mode=ro"`` (the naive form) mis-parses a path
    containing ``?`` or ``#``: SQLite's URI filenames follow RFC 3986, so an
    unescaped ``?``/``#`` in the path is read as the query-string/fragment
    delimiter, silently truncating or corrupting the path SQLite actually
    opens — a store path containing either character could open the wrong
    file (or fail) instead of opening the intended file read-only.
    :meth:`~pathlib.Path.as_uri` percent-encodes the path per RFC 3986 before
    the ``mode=ro`` query string is appended, so both characters (and any
    other reserved/non-ASCII byte) round-trip correctly. Requires an absolute
    path, hence the ``resolve()``.

    Args:
        path: The SQLite file path to open read-only.

    Returns:
        A ``file:...?mode=ro`` URI safe to pass to ``sqlite3.connect(uri=True)``.
    """
    return f"{path.resolve().as_uri()}?mode=ro"


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only, never mutating the operator's data.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        A read-only ``sqlite3.Connection`` (the default row factory — callers
        that want ``sqlite3.Row`` set it themselves).

    Raises:
        FileNotFoundError: If ``db_path`` does not exist at the initial check,
            or — closing a TOCTOU window — if ``db_path`` is removed or
            rotated out from under the caller between that check and the
            SQLite open itself. That race is detected narrowly: only when the
            ``sqlite3.connect`` call raises ``sqlite3.OperationalError`` *and*
            a post-failure existence check proves ``db_path`` is now absent.
            Every other open failure (corruption, permissions, locking, a
            malformed database, or any other cause left while ``db_path``
            still exists) re-raises the original ``sqlite3.OperationalError``
            unchanged — it is never reinterpreted as a missing file.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"no store at {db_path}")
    try:
        return sqlite3.connect(read_only_sqlite_uri(db_path), uri=True)
    except sqlite3.OperationalError:
        if not db_path.exists():
            raise FileNotFoundError(f"no store at {db_path}") from None
        raise


def snapshot_store_to_temp(
    store_path: Path, tmp_dir: Path, *, snapshot_name: str = DEFAULT_SNAPSHOT_NAME
) -> Path:
    """Copy ``store_path`` to a private temp file; callers open ONLY the copy.

    Uses SQLite's own online backup API (:meth:`sqlite3.Connection.backup`)
    against a strictly read-only (``mode=ro``) source connection (see
    :func:`connect_read_only`), so the snapshot is a fully consistent
    point-in-time copy (including anything still only in the source's WAL)
    without ever acquiring a write lock on the operator's file. The live
    agent's own store is opened read-write and has migrations applied — the
    normal, safe thing for the live agent to do to ITS OWN store — so this
    isolation is what keeps the operator's live database untouched even
    though a caller of this function only ever needs read access.

    Args:
        store_path: The real store to copy. Never opened read-write.
        tmp_dir: A scratch directory the caller owns and will clean up. Must
            already exist.
        snapshot_name: The target filename within ``tmp_dir``. Must be a
            single non-empty path component (no ``/`` or ``\\``, not ``.`` or
            ``..``) so it cannot escape ``tmp_dir``.

    Returns:
        The path to the private snapshot copy (``tmp_dir / snapshot_name``).

    Raises:
        FileNotFoundError: If ``store_path`` does not exist.
        sqlite3.OperationalError: If ``tmp_dir`` does not already exist, so a
            private temporary snapshot cannot be created there.
        ValueError: If ``snapshot_name`` is empty, ``.``, ``..``, or contains
            a path separator (i.e. is not a single plain filename); or if the
            resulting ``tmp_dir / snapshot_name`` target aliases ``store_path``
            — the same resolved path, a symlink to the source, or a hard link
            to the source. This alias check runs, and fails closed, before
            either database is opened: opening the writable target in that
            situation would silently turn the operator's live database into
            its own backup target (and could hang indefinitely against the
            read-only source connection's lock), which violates this
            function's read-only-source guarantee.
    """
    is_invalid = (
        not snapshot_name
        or snapshot_name in {".", ".."}
        or "/" in snapshot_name
        or "\\" in snapshot_name
        or len(Path(snapshot_name).parts) != 1
    )
    if is_invalid:
        raise ValueError(f"snapshot_name must be a single plain filename, got {snapshot_name!r}")
    snapshot_path = tmp_dir / snapshot_name
    _reject_existing_snapshot_symlink(snapshot_path)
    if _aliases_same_file(store_path, snapshot_path):
        raise ValueError(
            f"snapshot target {snapshot_path} aliases the source store {store_path}; "
            "refusing to open the operator's live database as its own backup target"
        )
    source = connect_read_only(store_path)
    try:
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
            snapshot_bytes = target.serialize()
        finally:
            target.close()
    finally:
        source.close()
    _write_snapshot_atomically(snapshot_path, snapshot_bytes)
    return snapshot_path
