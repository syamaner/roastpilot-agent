"""Hardware-free proofs for the root conftest session-finish cleanup guard (#1030).

The root ``tests/conftest.py`` ``pytest_sessionfinish`` hook skips importing
``roastpilot_agent.bean_sourcing`` only when neither import cache holds it.
Every other state must reach the unchanged original lookup, lock, clear and
shutdown path. Fall-through cases are judged against a literal copy of the
original ``from`` import, executed under a first-position meta-path blocker so
no test here ever loads the real parse module or touches its globals.
"""

import sys
import threading
import types
import typing
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import roastpilot_agent

_CHILD = "roastpilot_agent.bean_sourcing"
_PACKAGE = "roastpilot_agent"
_ABSENT_PACKAGE = "_rp1030_absent_pkg"
_WATCHDOG_SECONDS = 30.0
_PLACEHOLDER = types.ModuleType("_rp1030_placeholder")

_Hook = Callable[[pytest.Session, int], None]
_Observation = tuple[type[Exception] | None, str, object]


class _UnexpectedImport(Exception):
    """Raised by the blocker; deliberately not an ``ImportError`` subclass."""


class _ShutdownFailure(Exception):
    """Raised by an executor double to prove shutdown failures propagate."""


class _ImportBlocker:
    """First-position meta-path finder refusing every ``*.bean_sourcing`` load."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: types.ModuleType | None = None,
        /,
    ) -> None:
        """Record and refuse parse-module lookups; defer every other name."""
        del path, target
        if fullname.endswith(".bean_sourcing"):
            self.requested.append(fullname)
            raise _UnexpectedImport(fullname)
        return None


def _literal_original_lookup() -> object:
    from roastpilot_agent import bean_sourcing  # identical to tests/conftest.py:356

    return bean_sourcing


def _observe(fn: Callable[[], object]) -> _Observation:
    try:
        value = fn()
    except Exception as error:
        return (type(error), str(error), None)
    return (None, "", value)


def _module_cache() -> dict[str, object]:
    # The single deliberate cast: sys.modules is typed as holding modules only,
    # and these proofs must build intentionally malformed cache states (a None
    # entry, or a non-module package object) such as B1, B3 and B4.
    return typing.cast("dict[str, object]", sys.modules)


def _install_blocker(mp: pytest.MonkeyPatch) -> _ImportBlocker:
    blocker = _ImportBlocker()
    mp.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    return blocker


def _remove_module(mp: pytest.MonkeyPatch, name: str) -> None:
    # setitem first records the original entry, or its absence, so undo also
    # removes anything a later import inserts under this key.
    mp.setitem(sys.modules, name, _PLACEHOLDER)
    mp.delitem(sys.modules, name)


def _remove_attribute(mp: pytest.MonkeyPatch, target: object, name: str) -> None:
    mp.setattr(target, name, None, raising=False)
    mp.delattr(target, name)


def _fake_package(name: str = _PACKAGE) -> types.ModuleType:
    package = types.ModuleType(name)
    vars(package)["__path__"] = []
    return package


def _install_package(mp: pytest.MonkeyPatch, package: object) -> None:
    mp.setitem(_module_cache(), _PACKAGE, package)


class _RecordingLock:
    """A real lock that logs each context-manager acquire and release."""

    def __init__(self, log: list[object]) -> None:
        self._lock = threading.Lock()
        self._log = log

    def __enter__(self) -> bool:
        self._lock.acquire()
        self._log.append("acquire")
        return True

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self._log.append("release")
        self._lock.release()

    def locked(self) -> bool:
        """Report whether the wrapped lock is currently held."""
        return self._lock.locked()


class _RecordingExecutor:
    """Executor double recording shutdown arguments and observed state."""

    def __init__(
        self,
        module: types.ModuleType,
        lock: _RecordingLock,
        log: list[object],
        failure: Exception | None,
    ) -> None:
        self._module = module
        self._lock = lock
        self._log = log
        self._failure = failure
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def shutdown(self, *args: object, **kwargs: object) -> None:
        """Record the call, the lock state and the module singleton at call time."""
        self.calls.append((args, kwargs))
        singleton = getattr(self._module, "_parse_executor", _PLACEHOLDER)
        self._log.append(("shutdown", self._lock.locked(), singleton))
        if self._failure is not None:
            raise self._failure


@dataclass
class _ParseFake:
    """A fake parse module with its recording lock, executor and logs."""

    module: types.ModuleType
    lock: _RecordingLock
    executor: object
    log: list[object] = field(default_factory=lambda: [])
    factory_calls: list[str] = field(default_factory=lambda: [])

    @property
    def singleton(self) -> object:
        """Return the fake module's current executor singleton."""
        return getattr(self.module, "_parse_executor", _PLACEHOLDER)

    @property
    def calls(self) -> list[tuple[tuple[object, ...], dict[str, object]]]:
        """Return the recording executor's shutdown calls."""
        executor = self.executor
        assert type(executor) is _RecordingExecutor
        return executor.calls


def _parse_fake(*, live: bool = True, failure: Exception | None = None) -> _ParseFake:
    module = types.ModuleType(_CHILD)
    log: list[object] = []
    factory_calls: list[str] = []
    lock = _RecordingLock(log)
    executor: object = _RecordingExecutor(module, lock, log, failure) if live else None

    def get_parse_executor() -> object:
        factory_calls.append("_get_parse_executor")
        return None

    namespace = vars(module)
    namespace["_parse_executor_lock"] = lock
    namespace["_parse_executor"] = executor
    namespace["_get_parse_executor"] = get_parse_executor
    return _ParseFake(module, lock, executor, log, factory_calls)


class _CanonicalStr(str):
    """A ``str`` subclass equal to the canonical package name."""

    __slots__ = ()


class _RecordingStr(str):
    """A ``str`` subclass logging coercion, comparison, hashing and fspath."""

    log: list[str]

    def __new__(cls, value: str, log: list[str]) -> "_RecordingStr":
        """Build the recording string bound to a per-case log."""
        instance = super().__new__(cls, value)
        instance.log = log
        return instance

    def __str__(self) -> str:
        self.log.append("__str__")
        return str.__str__(self)

    def __eq__(self, other: object) -> bool:
        self.log.append("__eq__")
        return str.__eq__(self, other)

    def __hash__(self) -> int:
        self.log.append("__hash__")
        return str.__hash__(self)

    def __fspath__(self) -> str:
        """Return a plain ``str`` copy of the value."""
        self.log.append("__fspath__")
        return str.__str__(self)


@pytest.fixture
def hook(pytestconfig: pytest.Config) -> _Hook:
    """Bind the root conftest's ``pytest_sessionfinish`` without importing it."""
    target = Path(__file__).resolve().parent / "conftest.py"
    matches = [
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if isinstance(plugin, types.ModuleType)
        and plugin.__file__ is not None
        and Path(plugin.__file__).resolve() == target
    ]
    assert len(matches) == 1
    found: _Hook = matches[0].pytest_sessionfinish
    return found


@pytest.fixture
def session(request: pytest.FixtureRequest) -> pytest.Session:
    """Return the running session, passed to the hook as in production."""
    return request.session


# Group A: the skip state.


def test_a1_real_package_with_neither_cache_entry_skips_the_import(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """The real package without the parse module returns before any import."""
    namespace = vars(roastpilot_agent)
    assert type(roastpilot_agent) is types.ModuleType
    assert sys.modules[_PACKAGE] is roastpilot_agent
    assert "__name__" in namespace
    assert type(namespace["__name__"]) is str
    assert namespace["__name__"] == _PACKAGE
    assert "__path__" in namespace
    # Entry types are checked before the list check narrows the raw value.
    assert all(type(entry) is str for entry in namespace["__path__"])
    assert type(namespace["__path__"]) is list
    assert "__getattr__" not in namespace

    with monkeypatch.context() as mp:
        _remove_module(mp, _CHILD)
        _remove_attribute(mp, roastpilot_agent, "bean_sourcing")
        blocker = _install_blocker(mp)

        assert _observe(lambda: hook(session, 0)) == (None, "", None)
        assert blocker.requested == []
        assert _CHILD not in sys.modules
        assert "bean_sourcing" not in vars(roastpilot_agent)


def test_a2_fake_exact_canonical_package_skips_and_is_left_unchanged(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """A fresh exact canonical package skips without mutating its namespace."""
    package = _fake_package()
    path = vars(package)["__path__"]
    before = dict(vars(package))
    with monkeypatch.context() as mp:
        _remove_module(mp, _CHILD)
        _install_package(mp, package)
        blocker = _install_blocker(mp)

        assert _observe(lambda: hook(session, 0)) == (None, "", None)
        assert blocker.requested == []
        assert _CHILD not in sys.modules
        assert dict(vars(package)) == before
        assert vars(package)["__path__"] is path
        assert path == []


# Group B: every unknown state falls through to the literal original lookup.


def _b1_child_none(mp: pytest.MonkeyPatch) -> list[str]:
    _install_package(mp, _fake_package())
    mp.setitem(_module_cache(), _CHILD, None)
    return []


def _b2_package_key_absent(mp: pytest.MonkeyPatch) -> list[str]:
    _remove_module(mp, _CHILD)
    _remove_module(mp, _PACKAGE)
    return []


def _b3_package_none(mp: pytest.MonkeyPatch) -> list[str]:
    _remove_module(mp, _CHILD)
    _install_package(mp, None)
    return []


def _b4_package_not_a_module(mp: pytest.MonkeyPatch) -> list[str]:
    _remove_module(mp, _CHILD)
    _install_package(mp, types.SimpleNamespace(__name__=_PACKAGE, __path__=[]))
    return []


def _b5_module_subclass(mp: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []

    class _CountingModule(types.ModuleType):
        def __getattribute__(self, name: str) -> object:
            log.append(name)
            return super().__getattribute__(name)

    package = _CountingModule(_PACKAGE)
    vars(package)["__path__"] = []
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return log


def _b6_raising_module_getattr(mp: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []

    def module_getattr(name: str) -> object:
        log.append(name)
        raise AttributeError(name)

    package = _fake_package()
    vars(package)["__getattr__"] = module_getattr
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return log


def _b7_none_module_getattr(mp: pytest.MonkeyPatch) -> list[str]:
    package = _fake_package()
    vars(package)["__getattr__"] = None
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return []


def _b8_non_canonical_name(mp: pytest.MonkeyPatch) -> list[str]:
    _remove_module(mp, _CHILD)
    _remove_module(mp, _ABSENT_PACKAGE)
    _install_package(mp, _fake_package(_ABSENT_PACKAGE))
    return []


def _b9_name_deleted(mp: pytest.MonkeyPatch) -> list[str]:
    package = _fake_package()
    del vars(package)["__name__"]
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return []


def _b10_str_subclass_name(mp: pytest.MonkeyPatch) -> list[str]:
    package = _fake_package()
    vars(package)["__name__"] = _CanonicalStr(_PACKAGE)
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return []


def _b11_path_none(mp: pytest.MonkeyPatch) -> list[str]:
    package = _fake_package()
    vars(package)["__path__"] = None
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return []


def _b12_path_missing(mp: pytest.MonkeyPatch) -> list[str]:
    _remove_module(mp, _CHILD)
    _install_package(mp, types.ModuleType(_PACKAGE))
    return []


def _b13_path_entry_none(mp: pytest.MonkeyPatch) -> list[str]:
    package = _fake_package()
    vars(package)["__path__"] = [None]
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return []


def _b14_path_entry_str_subclass(mp: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []
    package = _fake_package()
    vars(package)["__path__"] = [_RecordingStr("x", log)]
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return log


@dataclass(frozen=True)
class _FallCase:
    """One fall-through state with its pinned literal-original exception type."""

    setup: Callable[[pytest.MonkeyPatch], list[str]]
    expected: type[Exception]
    child_preset_none: bool = False


_FALL_CASES = {
    "B1": _FallCase(_b1_child_none, ModuleNotFoundError, child_preset_none=True),
    "B2": _FallCase(_b2_package_key_absent, _UnexpectedImport),
    "B3": _FallCase(_b3_package_none, ModuleNotFoundError),
    # Observed, not the planner prediction: the import machinery reads the
    # non-module parent's missing __spec__ before any finder runs.
    "B4": _FallCase(_b4_package_not_a_module, AttributeError),
    "B5": _FallCase(_b5_module_subclass, _UnexpectedImport),
    "B6": _FallCase(_b6_raising_module_getattr, _UnexpectedImport),
    "B7": _FallCase(_b7_none_module_getattr, TypeError),
    "B8": _FallCase(_b8_non_canonical_name, ModuleNotFoundError),
    "B9": _FallCase(_b9_name_deleted, AttributeError),
    "B10": _FallCase(_b10_str_subclass_name, _UnexpectedImport),
    "B11": _FallCase(_b11_path_none, _UnexpectedImport),
    "B12": _FallCase(_b12_path_missing, ImportError),
    "B13": _FallCase(_b13_path_entry_none, _UnexpectedImport),
    "B14": _FallCase(_b14_path_entry_str_subclass, _UnexpectedImport),
}


def _observe_in_state(
    monkeypatch: pytest.MonkeyPatch, case: _FallCase, fn: Callable[[], object]
) -> tuple[_Observation, list[str], list[str]]:
    with monkeypatch.context() as mp:
        blocker = _install_blocker(mp)
        log = case.setup(mp)
        log.clear()
        observation = _observe(fn)
        accesses = list(log)
        if case.child_preset_none:
            assert _CHILD in sys.modules
            assert sys.modules[_CHILD] is None
        else:
            assert _CHILD not in sys.modules
        return observation, list(blocker.requested), accesses


@pytest.mark.parametrize("case_id", list(_FALL_CASES))
def test_fall_through_state_matches_the_literal_original_lookup(
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
    hook: _Hook,
    session: pytest.Session,
) -> None:
    """Each unknown state reaches the original import with identical effects."""
    case = _FALL_CASES[case_id]
    reference, reference_requested, reference_accesses = _observe_in_state(
        monkeypatch, case, _literal_original_lookup
    )
    observed, observed_requested, observed_accesses = _observe_in_state(
        monkeypatch, case, lambda: hook(session, 0)
    )

    assert reference[0] is case.expected
    assert (observed[0], observed[1]) == (reference[0], reference[1])
    assert observed_requested == reference_requested
    assert observed_accesses == reference_accesses


def _f_attribute_none(mp: pytest.MonkeyPatch) -> None:
    package = _fake_package()
    vars(package)["bean_sourcing"] = None
    _remove_module(mp, _CHILD)
    _install_package(mp, package)


def test_f_present_but_none_attribute_reaches_the_original_error(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """A ``None`` package attribute is looked up and fails at the lock read."""
    with monkeypatch.context() as mp:
        blocker = _install_blocker(mp)
        _f_attribute_none(mp)
        assert _observe(_literal_original_lookup) == (None, "", None)
        assert blocker.requested == []
    with monkeypatch.context() as mp:
        blocker = _install_blocker(mp)
        _f_attribute_none(mp)
        with pytest.raises(AttributeError, match="_parse_executor_lock"):
            hook(session, 0)
        assert blocker.requested == []


# Group C: every loaded form is found by the original lookup and cleaned.


@dataclass
class _LoadedState:
    """A loaded-form state: the module the lookup returns, plus bystanders."""

    target: _ParseFake
    bystanders: list[_ParseFake]
    log: list[str]


def _c1_cache_only(mp: pytest.MonkeyPatch) -> _LoadedState:
    fake = _parse_fake()
    _install_package(mp, _fake_package())
    mp.setitem(sys.modules, _CHILD, fake.module)
    return _LoadedState(fake, [], [])


def _c2_attribute_only(mp: pytest.MonkeyPatch) -> _LoadedState:
    fake = _parse_fake()
    package = _fake_package()
    vars(package)["bean_sourcing"] = fake.module
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return _LoadedState(fake, [], [])


def _c3_real_package_both(mp: pytest.MonkeyPatch) -> _LoadedState:
    fake = _parse_fake()
    _install_package(mp, roastpilot_agent)
    mp.setitem(sys.modules, _CHILD, fake.module)
    mp.setattr(roastpilot_agent, "bean_sourcing", fake.module, raising=False)
    return _LoadedState(fake, [], [])


def _c4_disagreeing_caches(mp: pytest.MonkeyPatch) -> _LoadedState:
    attribute_fake = _parse_fake()
    cache_fake = _parse_fake()
    package = _fake_package()
    vars(package)["bean_sourcing"] = attribute_fake.module
    _install_package(mp, package)
    mp.setitem(sys.modules, _CHILD, cache_fake.module)
    return _LoadedState(attribute_fake, [cache_fake], [])


def _c5_stateful_module_getattr(mp: pytest.MonkeyPatch) -> _LoadedState:
    fake = _parse_fake()
    log: list[str] = []

    def module_getattr(name: str) -> object:
        log.append(name)
        if name == "bean_sourcing":
            return fake.module
        raise AttributeError(name)

    package = _fake_package()
    vars(package)["__getattr__"] = module_getattr
    _remove_module(mp, _CHILD)
    _install_package(mp, package)
    return _LoadedState(fake, [], log)


_LOADED_CASES: dict[str, Callable[[pytest.MonkeyPatch], _LoadedState]] = {
    "C1": _c1_cache_only,
    "C2": _c2_attribute_only,
    "C3": _c3_real_package_both,
    "C4": _c4_disagreeing_caches,
    "C5": _c5_stateful_module_getattr,
}


@pytest.mark.parametrize("case_id", list(_LOADED_CASES))
def test_loaded_form_is_found_by_the_original_lookup_and_cleaned_once(
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
    hook: _Hook,
    session: pytest.Session,
) -> None:
    """The hook cleans exactly the module the literal original lookup returns."""
    setup = _LOADED_CASES[case_id]
    with monkeypatch.context() as mp:
        blocker = _install_blocker(mp)
        state = setup(mp)
        state.log.clear()
        reference = _observe(_literal_original_lookup)
        reference_accesses = list(state.log)
        assert reference[0] is None
        assert reference[2] is state.target.module
        assert blocker.requested == []

    with monkeypatch.context() as mp:
        blocker = _install_blocker(mp)
        state = setup(mp)
        state.log.clear()

        assert _observe(lambda: hook(session, 0)) == (None, "", None)
        assert state.target.calls == [((), {"wait": True, "cancel_futures": True})]
        assert state.target.singleton is None
        for bystander in state.bystanders:
            assert bystander.calls == []
            assert bystander.singleton is bystander.executor
        assert state.log == reference_accesses
        assert blocker.requested == []


# Group D: the preserved cleanup contract on a cache-only fake parse module.


def _install_cache_only(mp: pytest.MonkeyPatch, fake: _ParseFake) -> _ImportBlocker:
    blocker = _install_blocker(mp)
    _install_package(mp, _fake_package())
    mp.setitem(sys.modules, _CHILD, fake.module)
    return blocker


def test_d1_empty_singleton_only_takes_and_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """An empty singleton is read under the lock and never created."""
    fake = _parse_fake(live=False)
    with monkeypatch.context() as mp:
        _install_cache_only(mp, fake)
        assert _observe(lambda: hook(session, 0)) == (None, "", None)
    assert fake.log == ["acquire", "release"]
    assert fake.singleton is None
    assert fake.factory_calls == []


def test_d2_shutdown_runs_after_the_lock_is_released_and_cleared(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """Capture and clear happen under the lock; shutdown happens outside it."""
    fake = _parse_fake()
    with monkeypatch.context() as mp:
        _install_cache_only(mp, fake)
        assert _observe(lambda: hook(session, 0)) == (None, "", None)
    assert fake.log == ["acquire", "release", ("shutdown", False, None)]


def test_d3_shutdown_waits_and_cancels_pending_futures(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """Shutdown is called with exactly ``wait=True, cancel_futures=True``."""
    fake = _parse_fake()
    with monkeypatch.context() as mp:
        _install_cache_only(mp, fake)
        assert _observe(lambda: hook(session, 0)) == (None, "", None)
    assert len(fake.calls) == 1
    args, kwargs = fake.calls[0]
    assert args == ()
    assert kwargs == {"wait": True, "cancel_futures": True}


def test_d4_repeated_cleanup_shuts_down_once(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """A second cleanup finds the cleared singleton and only takes the lock."""
    fake = _parse_fake()
    with monkeypatch.context() as mp:
        _install_cache_only(mp, fake)
        assert _observe(lambda: hook(session, 0)) == (None, "", None)
        assert _observe(lambda: hook(session, 0)) == (None, "", None)
    assert len(fake.calls) == 1
    assert fake.log == [
        "acquire",
        "release",
        ("shutdown", False, None),
        "acquire",
        "release",
    ]


def test_d5_shutdown_failure_propagates_after_clearing(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """A shutdown failure is not swallowed and leaves no held lock."""
    fake = _parse_fake(failure=_ShutdownFailure("rp1030 shutdown failure"))
    with monkeypatch.context() as mp:
        _install_cache_only(mp, fake)
        with pytest.raises(_ShutdownFailure):
            hook(session, 0)
    assert fake.singleton is None
    assert not fake.lock.locked()


def _blocked_work(started: threading.Event, release: threading.Event) -> str:
    started.set()
    release.wait()
    return "done"


def test_d6_real_executor_cleanup_cancels_queued_work_and_joins_the_worker(
    monkeypatch: pytest.MonkeyPatch, hook: _Hook, session: pytest.Session
) -> None:
    """A real executor is cancelled and joined; cleanup waits for running work."""
    started = threading.Event()
    release = threading.Event()
    cancel_ack = threading.Event()
    cleanup_done = threading.Event()
    errors: list[Exception] = []
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rp1030-cleanup-proof")
    module = types.ModuleType(_CHILD)
    namespace = vars(module)
    namespace["_parse_executor_lock"] = threading.Lock()
    namespace["_parse_executor"] = executor
    thread: threading.Thread | None = None

    def run_cleanup() -> None:
        try:
            hook(session, 0)
        except Exception as error:
            errors.append(error)
        finally:
            cleanup_done.set()

    def acknowledge_cancel(_: Future[str]) -> None:
        cancel_ack.set()

    with monkeypatch.context() as mp:
        _install_blocker(mp)
        _install_package(mp, _fake_package())
        mp.setitem(sys.modules, _CHILD, module)
        try:
            running = executor.submit(_blocked_work, started, release)
            queued = executor.submit(_blocked_work, started, release)
            queued.add_done_callback(acknowledge_cancel)
            assert started.wait(_WATCHDOG_SECONDS)

            thread = threading.Thread(target=run_cleanup, name="rp1030-cleanup", daemon=True)
            thread.start()
            assert cancel_ack.wait(_WATCHDOG_SECONDS)
            assert queued.cancelled()
            assert not running.done()
            assert not cleanup_done.is_set()

            release.set()
            assert cleanup_done.wait(_WATCHDOG_SECONDS)
            thread.join(_WATCHDOG_SECONDS)
            assert not thread.is_alive()
            assert errors == []
            assert running.result(timeout=_WATCHDOG_SECONDS) == "done"
            assert namespace["_parse_executor"] is None
            with pytest.raises(RuntimeError):
                executor.submit(_blocked_work, started, release)
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)
            if thread is not None:
                thread.join(_WATCHDOG_SECONDS)
                if thread.is_alive():
                    raise AssertionError("rp1030 cleanup thread still alive after bounded join")
