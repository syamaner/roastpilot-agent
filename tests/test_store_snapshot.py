"""Tests for the shared read-only SQLite snapshot/URI helpers (#726 item 3)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bakeoff_reference_567  # noqa: E402
import rpd_corpus_score  # noqa: E402
import store_snapshot  # noqa: E402


def _seed_db(path: Path) -> None:
    """Create a tiny real SQLite DB with one table + one row at ``path``."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO widgets (id, name) VALUES (1, 'seed')")
        connection.commit()
    finally:
        connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- read_only_sqlite_uri: exact URI grammar -----------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "plain.sqlite3",
        "with?question.sqlite3",
        "with#hash.sqlite3",
        "with%percent.sqlite3",
        "with space.sqlite3",
        "with-unicode-café.sqlite3",
    ],
)
def test_read_only_sqlite_uri_percent_encodes_reserved_characters(
    tmp_path: Path, filename: str
) -> None:
    """The URI is exactly ``as_uri()`` + ``?mode=ro`` — RFC 3986 percent-encoding,
    never a naive f-string interpolation of the raw path."""
    path = tmp_path / filename
    uri = store_snapshot.read_only_sqlite_uri(path)
    assert uri == f"{path.resolve().as_uri()}?mode=ro"
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")


def test_read_only_sqlite_uri_resolves_a_relative_path(tmp_path: Path) -> None:
    """A relative path is resolved to absolute before URI construction (SQLite
    URI filenames must be absolute)."""
    (tmp_path / "rel.sqlite3").touch()
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        uri = store_snapshot.read_only_sqlite_uri(Path("rel.sqlite3"))
    finally:
        os.chdir(cwd)
    assert uri == f"{(tmp_path / 'rel.sqlite3').resolve().as_uri()}?mode=ro"


@pytest.mark.parametrize(
    "filename",
    [
        "with?question.sqlite3",
        "with#hash.sqlite3",
        "with%percent.sqlite3",
        "with space.sqlite3",
        "with-unicode-café.sqlite3",
    ],
)
def test_connect_read_only_opens_the_literal_file_for_special_characters(
    tmp_path: Path, filename: str
) -> None:
    """The read-only connection actually opens the INTENDED file (not a
    truncated/mis-parsed path) for every URI-significant character."""
    path = tmp_path / filename
    _seed_db(path)
    connection = store_snapshot.connect_read_only(path)
    try:
        rows = connection.execute("SELECT name FROM widgets").fetchall()
    finally:
        connection.close()
    assert rows == [("seed",)]


# --- connect_read_only: missing store, default row factory, write failure -----


def test_connect_read_only_missing_store_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no store at"):
        store_snapshot.connect_read_only(tmp_path / "does-not-exist.sqlite3")


def test_connect_read_only_uses_default_row_factory(tmp_path: Path) -> None:
    """Callers that want ``sqlite3.Row`` must set it themselves."""
    path = tmp_path / "store.sqlite3"
    _seed_db(path)
    connection = store_snapshot.connect_read_only(path)
    try:
        assert connection.row_factory is None
    finally:
        connection.close()


def test_connect_read_only_insert_fails(tmp_path: Path) -> None:
    """A write through a read-only connection fails closed."""
    path = tmp_path / "store.sqlite3"
    _seed_db(path)
    connection = store_snapshot.connect_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            connection.execute("INSERT INTO widgets (id, name) VALUES (2, 'nope')")
    finally:
        connection.close()
    # The source file must be untouched by the failed write attempt.
    verify = sqlite3.connect(str(path))
    try:
        rows = verify.execute("SELECT name FROM widgets").fetchall()
    finally:
        verify.close()
    assert rows == [("seed",)]


def test_connect_read_only_write_pragma_fails(tmp_path: Path) -> None:
    """A write-requiring PRAGMA through a read-only connection fails closed."""
    path = tmp_path / "store.sqlite3"
    _seed_db(path)
    connection = store_snapshot.connect_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            connection.execute("PRAGMA user_version = 5")
    finally:
        connection.close()


def test_connect_read_only_corrupt_db_query_fails_without_modifying_bytes(
    tmp_path: Path,
) -> None:
    """A corrupt source must not be modified by the failed read attempt."""
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a real sqlite database at all")
    before = _sha256(path)
    connection = store_snapshot.connect_read_only(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("SELECT * FROM widgets")
    finally:
        connection.close()
    assert _sha256(path) == before


# --- snapshot_store_to_temp: seeded reads, WAL inclusion, naming --------------


def test_snapshot_store_to_temp_default_name(tmp_path: Path) -> None:
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()

    snapshot_path = store_snapshot.snapshot_store_to_temp(store_path, dest_dir)

    assert snapshot_path == dest_dir / store_snapshot.DEFAULT_SNAPSHOT_NAME
    connection = sqlite3.connect(str(snapshot_path))
    try:
        rows = connection.execute("SELECT name FROM widgets").fetchall()
    finally:
        connection.close()
    assert rows == [("seed",)]


def test_snapshot_store_to_temp_custom_name(tmp_path: Path) -> None:
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()

    snapshot_path = store_snapshot.snapshot_store_to_temp(
        store_path, dest_dir, snapshot_name="custom_copy.sqlite3"
    )

    assert snapshot_path == dest_dir / "custom_copy.sqlite3"
    assert snapshot_path.exists()


def test_snapshot_store_to_temp_missing_store_raises_and_creates_nothing(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        store_snapshot.snapshot_store_to_temp(tmp_path / "nope.sqlite3", dest_dir)
    assert list(dest_dir.iterdir()) == []


def test_snapshot_store_to_temp_missing_temp_directory_writes_nowhere(
    tmp_path: Path,
) -> None:
    """A ``tmp_dir`` that does not exist cannot be silently created underneath;
    the attempted write fails and nothing is left on disk."""
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    missing_dir = tmp_path / "does-not-exist"
    with pytest.raises(sqlite3.OperationalError):
        store_snapshot.snapshot_store_to_temp(store_path, missing_dir)
    assert not missing_dir.exists()


@pytest.mark.parametrize(
    "bad_name",
    ["", ".", "..", "sub/dir.sqlite3", "sub\\dir.sqlite3", "../escape.sqlite3"],
)
def test_snapshot_store_to_temp_invalid_name_raises_and_writes_nothing(
    tmp_path: Path, bad_name: str
) -> None:
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    with pytest.raises(ValueError, match="snapshot_name"):
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir, snapshot_name=bad_name)
    assert list(dest_dir.iterdir()) == []


def test_snapshot_store_to_temp_invalid_absolute_path_name_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """An absolute-path ``snapshot_name`` is rejected too — deliberately built
    from ``tmp_path`` (never a literal system path like ``/etc/passwd``) so a
    mutation that removes the guard cannot make this test attempt to touch a
    real system file."""
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    escape_target = str(tmp_path / "outside-dest-dir.sqlite3")
    with pytest.raises(ValueError, match="snapshot_name"):
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir, snapshot_name=escape_target)
    assert list(dest_dir.iterdir()) == []
    assert not Path(escape_target).exists()


def test_snapshot_store_to_temp_source_bytes_unchanged(tmp_path: Path) -> None:
    """The source DB/WAL/SHM bytes are unchanged by taking a snapshot.

    The writer connection is kept open (mirrors the live agent, whose store is
    open for the whole process lifetime) so the main DB and ``-wal`` sidecar
    both already exist, with real committed data in the WAL, before the
    snapshot is taken. Only the durable, data-carrying files (the main DB and
    the ``-wal`` log) are checked byte-for-byte: the ``-shm`` file is SQLite's
    own ephemeral shared-memory reader-coordination index, which every WAL
    reader (even a strictly ``mode=ro`` one) legitimately updates as part of
    attaching — that is SQLite's own concurrent-reader bookkeeping, not a
    mutation of the operator's roast data, so it is deliberately excluded
    from the byte-identity assertion.
    """
    store_path = tmp_path / "store.sqlite3"
    connection = sqlite3.connect(str(store_path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO widgets (id, name) VALUES (1, 'seed')")
    connection.commit()
    try:
        data_paths = {
            suffix: store_path.with_name(store_path.name + suffix) for suffix in ("", "-wal")
        }
        before = {suffix: _sha256(p) for suffix, p in data_paths.items() if p.exists()}
        assert before, "expected the main DB (and its WAL sidecar) to exist"

        dest_dir = tmp_path / "snap"
        dest_dir.mkdir()
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir)

        after = {suffix: _sha256(p) for suffix, p in data_paths.items() if p.exists()}
        assert before == after
    finally:
        connection.close()


def test_snapshot_store_to_temp_is_wal_inclusive(tmp_path: Path) -> None:
    """A row committed but not yet checkpointed out of the WAL sidecar is
    still present in the snapshot (the online-backup API reads through WAL)."""
    store_path = tmp_path / "store.sqlite3"
    # Keep this connection open (no checkpoint) across the snapshot call, so the
    # committed row genuinely lives only in the -wal sidecar at snapshot time.
    connection = sqlite3.connect(str(store_path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO widgets (id, name) VALUES (1, 'wal-only')")
    connection.commit()
    try:
        dest_dir = tmp_path / "snap"
        dest_dir.mkdir()
        snapshot_path = store_snapshot.snapshot_store_to_temp(store_path, dest_dir)
        verify = sqlite3.connect(str(snapshot_path))
        try:
            rows = verify.execute("SELECT name FROM widgets").fetchall()
        finally:
            verify.close()
        assert rows == [("wal-only",)]
    finally:
        connection.close()


def test_snapshot_store_to_temp_corrupt_source_not_modified(tmp_path: Path) -> None:
    """A corrupt source DB must not be modified by a failed snapshot attempt."""
    store_path = tmp_path / "corrupt.sqlite3"
    store_path.write_bytes(b"not a real sqlite database at all")
    before = _sha256(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    with pytest.raises(sqlite3.DatabaseError):
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir)
    assert _sha256(store_path) == before


# --- adoption: RPD + bakeoff module bindings are the shared helper ------------


def test_rpd_corpus_score_binds_the_shared_snapshot_helper() -> None:
    assert rpd_corpus_score.snapshot_store_to_temp is store_snapshot.snapshot_store_to_temp


def test_bakeoff_reference_567_binds_the_shared_snapshot_helper() -> None:
    assert bakeoff_reference_567.snapshot_store_to_temp is store_snapshot.snapshot_store_to_temp
