"""Tests for the shared read-only SQLite snapshot/URI helpers (#726 item 3)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
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


def test_connect_read_only_calls_sqlite3_connect_with_percent_encoded_uri_and_uri_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic, SQLite-build-independent proof of the exact call made.

    The real end-to-end tests above (``test_connect_read_only_opens_the_
    literal_file_for_special_characters`` etc.) prove behaviour against a
    live SQLite build with URI filenames actually compiled in
    (``SQLITE_USE_URI``), which is not guaranteed on every host. This test
    instead mocks :func:`sqlite3.connect` and asserts the exact positional
    URI and ``uri=True`` keyword ``connect_read_only`` passes it, so it fails
    deterministically — independent of the host's SQLite build — if
    ``uri=True`` is ever dropped, inverted, or the raw (non-percent-encoded)
    path is passed instead of :func:`store_snapshot.read_only_sqlite_uri`'s
    result.
    """
    path = tmp_path / "store.sqlite3"
    path.touch()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    sentinel_connection = object()

    def fake_connect(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return sentinel_connection

    monkeypatch.setattr(store_snapshot.sqlite3, "connect", fake_connect)

    result = store_snapshot.connect_read_only(path)

    assert result is sentinel_connection
    assert len(calls) == 1
    (positional_args, keyword_args) = calls[0]
    assert positional_args == (store_snapshot.read_only_sqlite_uri(path),)
    assert keyword_args == {"uri": True}


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


# --- connect_read_only: TOCTOU race between the existence check and the open --


def test_connect_read_only_translates_open_failure_when_path_removed_mid_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file removed/rotated between the initial ``exists()`` check and the
    SQLite open translates to the documented ``FileNotFoundError``, instead of
    leaking a raw ``sqlite3.OperationalError`` that does not name the real
    cause. The removal happens INSIDE the faked ``sqlite3.connect`` call, so
    ``db_path`` genuinely exists at the initial check and is genuinely absent
    only once the post-failure check runs — exercising the real race window,
    not just its two static end-states."""
    path = tmp_path / "store.sqlite3"
    _seed_db(path)

    def racing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        path.unlink()
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(store_snapshot.sqlite3, "connect", racing_connect)

    with pytest.raises(FileNotFoundError, match="no store at"):
        store_snapshot.connect_read_only(path)
    assert not path.exists()


def test_connect_read_only_reraises_operational_error_unchanged_when_path_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open failure that is NOT a missing file (corruption, permissions,
    locking, ...) re-raises the original ``sqlite3.OperationalError``
    unchanged — it must never be flattened into a missing-file claim while
    the path is still present."""
    path = tmp_path / "store.sqlite3"
    _seed_db(path)

    def failing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_snapshot.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store_snapshot.connect_read_only(path)
    assert path.exists()


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


# --- snapshot_store_to_temp: destination-aliases-source rejection -------------


def test_snapshot_store_to_temp_rejects_same_resolved_path_alias(tmp_path: Path) -> None:
    """``tmp_dir``/``snapshot_name`` resolving to the literal source path must
    fail closed instead of opening the operator's live database as its own
    backup target."""
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    before = _sha256(store_path)

    with pytest.raises(ValueError, match="aliases the source store"):
        store_snapshot.snapshot_store_to_temp(store_path, tmp_path, snapshot_name=store_path.name)

    assert _sha256(store_path) == before


def test_snapshot_store_to_temp_rejects_symlink_alias(tmp_path: Path) -> None:
    """The target-symlink guard rejects a pre-existing source symlink before
    the resolved-path alias check could inspect it."""
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    before = _sha256(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    link_name = "linked.sqlite3"
    link_path = dest_dir / link_name
    try:
        link_path.symlink_to(store_path)
    except OSError:
        pytest.skip("symlink creation is not supported on this filesystem/platform")

    with pytest.raises(ValueError, match="aliases the source store"):
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir, snapshot_name=link_name)

    assert _sha256(store_path) == before


def test_snapshot_store_to_temp_rejects_intermediate_symlink_directory_alias_before_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regular target entry reached through a symlinked parent is rejected
    by resolved-path equality before either SQLite connection opens."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store_path = store_dir / "store.sqlite3"
    _seed_db(store_path)
    before = _sha256(store_path)
    linked_dir = tmp_path / "linked-store"
    try:
        linked_dir.symlink_to(store_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not supported on this filesystem/platform")
    snapshot_path = linked_dir / store_path.name
    assert not snapshot_path.is_symlink()

    def fail_source_connection(_path: Path) -> sqlite3.Connection:
        raise AssertionError("source connection must not open for an aliasing target")

    def fail_sqlite_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("SQLite connection must not open for an aliasing target")

    monkeypatch.setattr(store_snapshot, "connect_read_only", fail_source_connection)
    monkeypatch.setattr(store_snapshot.sqlite3, "connect", fail_sqlite_connection)

    with pytest.raises(ValueError, match="aliases the source store"):
        store_snapshot.snapshot_store_to_temp(store_path, linked_dir, snapshot_name=store_path.name)

    assert _sha256(store_path) == before


def test_aliases_same_file_rejects_uninspectable_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a missing entry is treated as safe; inspection failures fail closed."""
    store_path = tmp_path / "store.sqlite3"
    snapshot_path = tmp_path / "snapshot.sqlite3"
    _seed_db(store_path)
    snapshot_path.touch()
    source_before = _sha256(store_path)

    def unresolved(path: Path, *, strict: bool = False) -> Path:
        return path

    original_stat = Path.stat

    def denied_target_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == snapshot_path:
            raise PermissionError("target metadata denied")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(store_snapshot.Path, "resolve", unresolved)
    monkeypatch.setattr(store_snapshot.Path, "stat", denied_target_stat)

    with pytest.raises(ValueError, match="cannot safely inspect source"):
        store_snapshot._aliases_same_file(store_path, snapshot_path)

    assert _sha256(store_path) == source_before


def test_reject_existing_snapshot_symlink_rejects_uninspectable_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target metadata error cannot be mistaken for an absent safe target."""
    store_path = tmp_path / "store.sqlite3"
    snapshot_path = tmp_path / "snapshot.sqlite3"
    _seed_db(store_path)
    source_before = _sha256(store_path)

    def denied_lstat(_path: Path) -> os.stat_result:
        raise PermissionError("target metadata denied")

    monkeypatch.setattr(store_snapshot.Path, "lstat", denied_lstat)

    with pytest.raises(ValueError, match="cannot safely inspect snapshot target"):
        store_snapshot._reject_existing_snapshot_symlink(snapshot_path)

    assert _sha256(store_path) == source_before


def test_write_snapshot_atomically_cleans_up_after_failed_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed publication preserves the old target and removes private bytes."""
    store_path = tmp_path / "store.sqlite3"
    snapshot_path = tmp_path / "snapshot.sqlite3"
    _seed_db(store_path)
    snapshot_path.write_bytes(b"previous snapshot")
    source_before = _sha256(store_path)
    target_before = snapshot_path.read_bytes()

    def failed_replace(_source: Path, _target: Path) -> None:
        raise OSError("publication failed")

    monkeypatch.setattr(store_snapshot.os, "replace", failed_replace)

    with pytest.raises(OSError, match="publication failed"):
        store_snapshot._write_snapshot_atomically(snapshot_path, b"new snapshot")

    assert snapshot_path.read_bytes() == target_before
    assert _sha256(store_path) == source_before
    assert list(tmp_path.glob(f".{snapshot_path.name}.*.tmp")) == []


def test_snapshot_store_to_temp_rejects_unrelated_target_symlink_before_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target symlink cannot redirect snapshot writes to an unrelated file."""
    store_path = tmp_path / "store.sqlite3"
    unrelated_path = tmp_path / "unrelated.sqlite3"
    _seed_db(store_path)
    _seed_db(unrelated_path)
    source_before = _sha256(store_path)
    unrelated_before = _sha256(unrelated_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    target_path = dest_dir / store_snapshot.DEFAULT_SNAPSHOT_NAME
    try:
        target_path.symlink_to(unrelated_path)
    except OSError:
        pytest.skip("symlink creation is not supported on this filesystem/platform")

    def fail_source_connection(_path: Path) -> sqlite3.Connection:
        raise AssertionError("source connection must not open for a symlink target")

    def fail_sqlite_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("SQLite connection must not open for a symlink target")

    monkeypatch.setattr(store_snapshot, "connect_read_only", fail_source_connection)
    monkeypatch.setattr(store_snapshot.sqlite3, "connect", fail_sqlite_connection)

    with pytest.raises(ValueError, match="through a symlink"):
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir)

    assert _sha256(store_path) == source_before
    assert _sha256(unrelated_path) == unrelated_before


def test_snapshot_store_to_temp_replaces_a_symlink_swapped_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-validation target swap cannot redirect writes through its symlink."""
    store_path = tmp_path / "store.sqlite3"
    unrelated_path = tmp_path / "unrelated.sqlite3"
    _seed_db(store_path)
    _seed_db(unrelated_path)
    source_before = _sha256(store_path)
    unrelated_before = _sha256(unrelated_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    snapshot_path = dest_dir / store_snapshot.DEFAULT_SNAPSHOT_NAME
    real_connect_read_only = store_snapshot.connect_read_only

    def swap_target_after_validation(path: Path) -> sqlite3.Connection:
        connection = real_connect_read_only(path)
        try:
            snapshot_path.symlink_to(unrelated_path)
        except OSError:
            connection.close()
            pytest.skip("symlink creation is not supported on this filesystem/platform")
        return connection

    monkeypatch.setattr(store_snapshot, "connect_read_only", swap_target_after_validation)

    result = store_snapshot.snapshot_store_to_temp(store_path, dest_dir)

    assert result == snapshot_path
    assert not result.is_symlink()
    assert _sha256(store_path) == source_before
    assert _sha256(unrelated_path) == unrelated_before
    connection = sqlite3.connect(str(result))
    try:
        assert connection.execute("SELECT name FROM widgets").fetchall() == [("seed",)]
    finally:
        connection.close()


def test_snapshot_store_to_temp_rejects_hard_link_alias(tmp_path: Path) -> None:
    """A pre-existing hard link at the computed target path sharing the
    source's inode must be rejected — resolved-path equality alone would miss
    this, since hard links are not resolved by ``Path.resolve``."""
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    before = _sha256(store_path)
    dest_dir = tmp_path / "snap"
    dest_dir.mkdir()
    link_name = "hardlinked.sqlite3"
    link_path = dest_dir / link_name
    try:
        os.link(store_path, link_path)
    except OSError:
        pytest.skip("hard link creation is not supported on this filesystem/platform")

    with pytest.raises(ValueError, match="aliases the source store"):
        store_snapshot.snapshot_store_to_temp(store_path, dest_dir, snapshot_name=link_name)

    assert _sha256(store_path) == before


def test_snapshot_store_to_temp_alias_rejection_precedes_any_connection_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the alias check runs, and fails closed, BEFORE either the
    read-only source connection or the writable target connection is opened
    — not merely that the eventual ``ValueError`` happens to be raised.
    Patching both ``connect_read_only`` and ``sqlite3.connect`` to explode
    means this test fails loudly if either is ever reached ahead of the alias
    check, which also proves no source mutation is possible (the source is
    never even opened)."""
    store_path = tmp_path / "store.sqlite3"
    _seed_db(store_path)
    before = _sha256(store_path)

    def fail_connect_read_only(db_path: Path) -> sqlite3.Connection:
        raise AssertionError("connect_read_only must not be called for an aliasing target")

    def fail_sqlite_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("sqlite3.connect must not be called for an aliasing target")

    monkeypatch.setattr(store_snapshot, "connect_read_only", fail_connect_read_only)
    monkeypatch.setattr(store_snapshot.sqlite3, "connect", fail_sqlite_connect)

    with pytest.raises(ValueError, match="aliases the source store"):
        store_snapshot.snapshot_store_to_temp(store_path, tmp_path, snapshot_name=store_path.name)

    assert _sha256(store_path) == before


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


# --- import separation: never reach the roaster/control path ------------------


def test_store_snapshot_module_does_not_directly_import_controller_safety_or_mcp_client() -> None:
    """Static check on this module's own import statements.

    ``store_snapshot`` is a shared helper for offline, read-only scripts
    (bake-off harnesses, fixture exporters, corpus scorers); it must never
    pull in the live roaster/control path.
    """
    import ast

    tree = ast.parse(Path(store_snapshot.__file__).read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
    forbidden = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.mcp_client",
    }
    assert imported_modules.isdisjoint(forbidden)


def test_store_snapshot_never_transitively_imports_controller_safety_or_mcp_client() -> None:
    """Authoritative transitive-import check: the roaster/control path must be
    unreachable from this shared offline-script helper.

    Runs in a FRESH subprocess — this pytest process may already have
    imported ``controller``/``safety``/``mcp_client`` via other test modules
    collected in the same session, which would make an in-process
    ``sys.modules`` check a false pass. The subprocess only ever puts
    ``scripts/`` on ``sys.path`` and imports ``store_snapshot``, then
    inspects what that pulled in — so this fails meaningfully (a non-empty,
    named list of forbidden modules) if a future change to
    ``store_snapshot.py`` or anything it imports ever reaches the control
    path, directly or transitively.
    """
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(scripts_dir)!r})\n"
        "import store_snapshot\n"
        "loaded = {m for m in sys.modules if m.startswith('roastpilot_agent.')}\n"
        "forbidden = {\n"
        "    'roastpilot_agent.controller',\n"
        "    'roastpilot_agent.safety',\n"
        "    'roastpilot_agent.mcp_client',\n"
        "}\n"
        "hit = sorted(loaded & forbidden)\n"
        "print(','.join(hit))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"store_snapshot transitively imported forbidden modules: {result.stdout.strip()}\n"
        f"stderr: {result.stderr}"
    )
