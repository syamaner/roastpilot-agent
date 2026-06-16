"""Access-log verbosity tests (issue #267): the quiet filter + CLI>env>config.

Hardware-free and server-free. The :class:`logging.Filter` is unit-tested by
building uvicorn ``access`` log records directly (the 5-tuple
``(client_addr, method, full_path, http_version, status_code)`` in
``record.args``) and asserting ``filter()`` keeps/drops the right ones. The
resolution helpers are tested for the CLI > ``ROASTPILOT_*`` env > ``AppConfig``
precedence that mirrors the existing ``--db`` resolution.
"""
# This module deliberately unit-tests cli.py's private logging helpers (the
# filter, the path matcher, the resolvers), so private-member access is expected
# here; suppress the strict rule for the whole file rather than tag each import.
# pyright: reportPrivateUsage=false

import logging
from collections.abc import Iterator

import pytest

from roastpilot_agent.api import (
    DEFAULT_HTTP_ACCESS_LOG_QUIET_PATHS as API_QUIET_PATHS,
)
from roastpilot_agent.api import (
    EVENTS_PATH,
    HEALTH_PATH,
    TELEMETRY_PATH,
)
from roastpilot_agent.cli import (
    _UVICORN_ACCESS_LOGGER,
    _access_path_matches,
    _apply_access_log_mode,
    _QuietAccessLogFilter,
    _resolve_access_log_mode,
    _resolve_log_level,
)
from roastpilot_agent.config import (
    DEFAULT_HTTP_ACCESS_LOG_QUIET_PATHS as CONFIG_QUIET_PATHS,
)
from roastpilot_agent.config import (
    AppConfig,
    LoggingConfig,
)


def _access_record(path: str, status: int) -> logging.LogRecord:
    """Build a uvicorn ``access`` log record for ``path`` returning ``status``.

    Args:
        path: The request path (may carry a ``?query`` suffix).
        status: The HTTP status code.

    Returns:
        A ``uvicorn.access`` log record shaped like uvicorn emits.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", status),
        exc_info=None,
    )


# --- drift guard: config defaults must mirror the api.py route constants -----


def test_config_quiet_paths_mirror_api_route_constants() -> None:
    """The config default quiet-path set must equal the api.py route source.

    config.py cannot import api.py (import cycle), so the default list is
    duplicated; this drift test fails if a future route rename moves api.py's
    constants without updating config.py, which would silently un-quiet a path.
    """
    assert tuple(CONFIG_QUIET_PATHS) == tuple(API_QUIET_PATHS)
    assert tuple(API_QUIET_PATHS) == (EVENTS_PATH, TELEMETRY_PATH, HEALTH_PATH)


# --- _access_path_matches: run-id template matching --------------------------


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        # telemetry / events with a real run id are caught (any id)
        ("/api/roasts/abc-123/telemetry", TELEMETRY_PATH, True),
        ("/api/roasts/00000000-0000-0000-0000-000000000000/telemetry", TELEMETRY_PATH, True),
        ("/api/roasts/abc-123/events", EVENTS_PATH, True),
        # query string is stripped before matching
        ("/api/roasts/abc-123/telemetry?downsample=4", TELEMETRY_PATH, True),
        # exact (non-template) path
        ("/api/health", HEALTH_PATH, True),
        ("/api/health?x=1", HEALTH_PATH, True),
        # a deeper sub-path under the run id must NOT match (no '/' in the id)
        ("/api/roasts/abc/telemetry/extra", TELEMETRY_PATH, False),
        # empty run id does not match
        ("/api/roasts//telemetry", TELEMETRY_PATH, False),
        # different suffix / different route
        ("/api/roasts/abc-123/timeline", TELEMETRY_PATH, False),
        ("/api/roasts/abc-123", TELEMETRY_PATH, False),
        # health pattern does not over-match
        ("/api/healthz", HEALTH_PATH, False),
    ],
)
def test_access_path_matches(path: str, pattern: str, expected: bool) -> None:
    """``_access_path_matches`` honours the ``{run_id}`` template semantics."""
    assert _access_path_matches(path, pattern) is expected


# --- _QuietAccessLogFilter: keep/drop logic ----------------------------------


@pytest.fixture
def quiet_filter() -> _QuietAccessLogFilter:
    """A quiet filter seeded with the default quiet-path set."""
    return _QuietAccessLogFilter(list(API_QUIET_PATHS))


@pytest.mark.parametrize(
    "path",
    [
        "/api/roasts/run-1/events",
        "/api/roasts/run-1/telemetry",
        "/api/roasts/run-1/telemetry?downsample=8",
        "/api/health",
    ],
)
@pytest.mark.parametrize("status", [200, 204, 304])
def test_filter_drops_successful_chatty_paths(
    quiet_filter: _QuietAccessLogFilter, path: str, status: int
) -> None:
    """A 2xx/3xx on an SSE/telemetry/health path is dropped (filtered out)."""
    assert quiet_filter.filter(_access_record(path, status)) is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/roasts",
        "/api/roasts/run-1",
        "/api/roasts/run-1/timeline",
        "/api/roasts/run-1/log",
        "/",
    ],
)
def test_filter_keeps_successful_other_paths(
    quiet_filter: _QuietAccessLogFilter, path: str
) -> None:
    """A 200 to a non-quiet path is kept (logged)."""
    assert quiet_filter.filter(_access_record(path, 200)) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/roasts/run-1/events",
        "/api/roasts/run-1/telemetry",
        "/api/health",
        "/api/roasts/run-1",
        "/",
    ],
)
@pytest.mark.parametrize("status", [400, 404, 422, 500, 503])
def test_filter_keeps_errors_on_any_path(
    quiet_filter: _QuietAccessLogFilter, path: str, status: int
) -> None:
    """A 4xx/5xx on ANY path (including a quiet one) is always kept."""
    assert quiet_filter.filter(_access_record(path, status)) is True


def test_filter_keeps_unclassifiable_records(
    quiet_filter: _QuietAccessLogFilter,
) -> None:
    """A record that is not a recognizable access line fails OPEN (kept)."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="some non-access message",
        args=None,
        exc_info=None,
    )
    assert quiet_filter.filter(record) is True


def test_filter_keeps_record_with_short_args(
    quiet_filter: _QuietAccessLogFilter,
) -> None:
    """A short/odd args tuple is not classifiable and is kept (fail open)."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s %s",
        args=("a", "b"),
        exc_info=None,
    )
    assert quiet_filter.filter(record) is True


def test_filter_keeps_record_with_wrong_arg_types(
    quiet_filter: _QuietAccessLogFilter,
) -> None:
    """A 5-tuple whose path/status are not str/int is unclassifiable (kept).

    Guards the type-narrowing branch: a record of the right arity but the wrong
    element types (a future uvicorn format change) must fail open, not crash or
    silently drop.
    """
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s %s %s %s %s",
        args=("addr", "GET", 12345, "1.1", "200"),  # path int, status str
        exc_info=None,
    )
    assert quiet_filter.filter(record) is True


# --- _resolve_access_log_mode: CLI > env > config ----------------------------


def test_mode_cli_beats_env_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI value wins over both the env var and the config default."""
    monkeypatch.setenv("ROASTPILOT_HTTP_ACCESS_LOG", "off")
    assert _resolve_access_log_mode("full", "quiet") == "full"


def test_mode_env_beats_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no CLI value, the env var wins over the config default."""
    monkeypatch.setenv("ROASTPILOT_HTTP_ACCESS_LOG", "off")
    assert _resolve_access_log_mode(None, "quiet") == "off"


def test_mode_env_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is normalized (trimmed + lowercased)."""
    monkeypatch.setenv("ROASTPILOT_HTTP_ACCESS_LOG", "  FULL  ")
    assert _resolve_access_log_mode(None, "quiet") == "full"


def test_mode_config_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither CLI nor env, the config default applies."""
    monkeypatch.delenv("ROASTPILOT_HTTP_ACCESS_LOG", raising=False)
    assert _resolve_access_log_mode(None, "quiet") == "quiet"


def test_mode_invalid_env_falls_back_to_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An invalid env value is ignored (with a warning) and config wins."""
    monkeypatch.setenv("ROASTPILOT_HTTP_ACCESS_LOG", "loud")
    with caplog.at_level(logging.WARNING, logger="roastpilot_agent.cli"):
        assert _resolve_access_log_mode(None, "full") == "full"
    assert any("ROASTPILOT_HTTP_ACCESS_LOG" in r.message for r in caplog.records)


# --- _resolve_log_level: CLI > env > config ----------------------------------


def test_log_level_cli_beats_env_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI value wins over both the env var and the config default."""
    monkeypatch.setenv("ROASTPILOT_LOG_LEVEL", "warning")
    assert _resolve_log_level("debug", "info") == "debug"


def test_log_level_env_beats_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no CLI value, the env var wins over the config default."""
    monkeypatch.setenv("ROASTPILOT_LOG_LEVEL", "WARNING")
    assert _resolve_log_level(None, "info") == "warning"


def test_log_level_config_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither CLI nor env, the config default applies."""
    monkeypatch.delenv("ROASTPILOT_LOG_LEVEL", raising=False)
    assert _resolve_log_level(None, "info") == "info"


# --- _apply_access_log_mode: filter install + access_log flag -----------------


@pytest.fixture(autouse=True)
def clear_access_logger_filters() -> Iterator[None]:
    """Strip any quiet filter from ``uvicorn.access`` before and after each test."""

    def _strip() -> None:
        log = logging.getLogger(_UVICORN_ACCESS_LOGGER)
        for f in [f for f in log.filters if isinstance(f, _QuietAccessLogFilter)]:
            log.removeFilter(f)

    _strip()
    yield
    _strip()


def _quiet_filters() -> list[logging.Filter]:
    log = logging.getLogger(_UVICORN_ACCESS_LOGGER)
    return [f for f in log.filters if isinstance(f, _QuietAccessLogFilter)]


def test_apply_quiet_installs_filter_and_keeps_access_on() -> None:
    """``quiet`` installs exactly one filter and returns ``access_log=True``."""
    access_log = _apply_access_log_mode("quiet", list(API_QUIET_PATHS))
    assert access_log is True
    assert len(_quiet_filters()) == 1


def test_apply_quiet_is_idempotent() -> None:
    """Re-applying ``quiet`` does not stack duplicate filters."""
    _apply_access_log_mode("quiet", list(API_QUIET_PATHS))
    _apply_access_log_mode("quiet", list(API_QUIET_PATHS))
    assert len(_quiet_filters()) == 1


def test_apply_full_removes_filter_and_keeps_access_on() -> None:
    """``full`` clears any quiet filter and returns ``access_log=True``."""
    _apply_access_log_mode("quiet", list(API_QUIET_PATHS))
    access_log = _apply_access_log_mode("full", list(API_QUIET_PATHS))
    assert access_log is True
    assert _quiet_filters() == []


def test_apply_off_disables_access_log_and_clears_filter() -> None:
    """``off`` returns ``access_log=False`` and installs no filter."""
    _apply_access_log_mode("quiet", list(API_QUIET_PATHS))
    access_log = _apply_access_log_mode("off", list(API_QUIET_PATHS))
    assert access_log is False
    assert _quiet_filters() == []


# --- LoggingConfig + AppConfig wiring ----------------------------------------


def test_logging_config_defaults() -> None:
    """The config default is ``quiet`` with the three chatty paths."""
    cfg = LoggingConfig()
    assert cfg.http_access_log_mode == "quiet"
    assert cfg.log_level == "info"
    assert tuple(cfg.http_access_log_quiet_paths) == tuple(API_QUIET_PATHS)


def test_appconfig_composes_logging_section() -> None:
    """``AppConfig`` exposes the ``logging`` section with the quiet default."""
    assert AppConfig().logging.http_access_log_mode == "quiet"


def test_appconfig_logging_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nested ``ROASTPILOT_LOGGING__*`` env override loads into the section."""
    monkeypatch.setenv("ROASTPILOT_LOGGING__HTTP_ACCESS_LOG_MODE", "full")
    assert AppConfig().logging.http_access_log_mode == "full"
