"""Executable regression tests for the #773 worktree gate recipe fail-closed fixes.

The recipe lives in exactly one place — the "Per-worktree gate environment
(venv, pyright, pytest)" section of `docs/agent-team-worktrees.md` — and this
module extracts it under a closed grammar (§3.0 of the ratified contract) and
then actually EXECUTES the extracted bytes, never asserts on the presence of a
phrase (the one deliberate exception is the single canonical-remedy exact-match
anchor). This is a continuation of the pattern already ratified in
`tests/test_agent_worktree_controls.py`, which reads this same runbook under
test.

D154 discipline: every negative arm below fails against the pre-#773 recipe
shape (proven by the ``test_mutation_*`` functions), so a guard cannot pass
vacuously. No test in this module may ``pytest.skip`` — an environment where a
check cannot run is a ``pytest.fail``, because a skipped containment test is
precisely the false PASS the underlying issue (#773) exists to remove.

The runbook is cited here by section name only, never by line number (the
`agent-team-worktrees.md:<line>` citation ban in
`tests/test_agent_worktree_controls.py` does not scan `tests/`, so this module
follows the rule voluntarily).
"""

from __future__ import annotations

import ast
import functools
import os
import re
import shlex
import site
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_RUNBOOK = _REPO / "docs" / "agent-team-worktrees.md"

_SECTION_HEADING = (
    "Per-worktree gate environment (venv, pyright, pytest) — added Aug 2026 (#738, #733)"
)
_HEADING_PATTERN = re.compile(rf"^## {re.escape(_SECTION_HEADING)}[ \t]*$", re.MULTILINE)
_NEXT_HEADING_PATTERN = re.compile(r"^## ", re.MULTILINE)
_BASH_BLOCK_PATTERN = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
_RECIPE_LINE_PREFIX = "cd <abs worktree> && "
_PROBE_LINE_PATTERN = re.compile(r"^cd <abs worktree> && \.venv/bin/python -c '(?P<source>.*)'$")
_PYTHON_C_SUFFIX_PATTERN = re.compile(r"\.venv/bin/python -c '(?P<source>.*)'$")
_FORBIDDEN_LITERALS = ("--clear", "rm -rf .venv/")
_STOP_ON_FAILURE = "set -e"

#: T14's single exact-match anchor (D154: "byte-exact, not keyword or
#: proximity"). Copied byte-for-byte from the "Guarded `.venv` rebuild remedy"
#: paragraph; an intentional prose edit updates this constant in the same
#: commit.
CANONICAL_REMEDY_SENTENCE = (
    "If `.venv` is a real directory, remove it\n"
    "with `rm -rf .venv` (no trailing slash) and restart the recipe from the\n"
    "absence guard above; if `.venv` is a symlink — live or dangling — stop and\n"
    "report rather than removing or clearing anything through it, because it may be\n"
    "another checkout's borrowed venv."
)

#: T14's second exact-match anchor. The absence guard and the following venv
#: creation command are deliberately not presented as an atomic provisioning
#: primitive; concurrent writers remain a shared surface.
ABSENCE_GUARD_SERIALIZATION_SENTENCE = (
    "This two-line guard/create recipe assumes exactly one provisioning writer for\n"
    "this worktree path. Concurrent provisioning of the same `.venv` path must be\n"
    "serialized under the shared-surface rule: a real directory created in the gap\n"
    "can be silently reused by CPython venv. The recipe is not atomic."
)

#: T14's third exact-match anchor. Adjacent scrubbed commands remain separate
#: Bash calls in the documented workflow, so their environment boundary is not
#: an atomic transaction.
ADJACENT_SCRUB_SERIALIZATION_SENTENCE = (
    "When the adjacent verification and pytest lines run as separate Bash calls,\n"
    "they are not atomic: the single-writer/serialization discipline must prevent\n"
    "ambient environment mutation between them. A copied whole block remains\n"
    "protected by its opening `set -e`."
)

#: Poisoned values used by the AC3 tests. Every name is drawn from a real
#: `ROASTPILOT_*` setting (`config.py`) or a real credential/path variable the
#: gate recipe must isolate; every value is a throwaway sentinel that must
#: never appear in captured stdout/stderr (G10).
_POISON_VALUES: dict[str, str] = {
    "ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE": '{"development": "openrouter/rp773-poison-model"}',
    "ROASTPILOT_SAFETY__PRE_T0_OVERRUN_SEVERITY": "fault",
    "ROASTPILOT_DB": "/tmp/rp773-poison-store.sqlite3",
    "OPENROUTER_API_KEY": "sk-rp773-poison-credential",
    "PYTHONPATH": "/tmp/rp773-poison-pythonpath",
}
_CASE_VARIANT_POISON_VALUES: dict[str, str] = {
    "roastpilot_advisor__model_slug_by_phase": '{"development": "openrouter/rp773-lower"}',
    "RoAsTpIlOt_AdViSoR__MoDeL_SlUg_By_PhAsE": '{"development": "openrouter/rp773-mixed"}',
}
_ALL_POISON_VALUES = _POISON_VALUES | _CASE_VARIANT_POISON_VALUES


@dataclass(frozen=True)
class RecipeLines:
    """The `#773`-fixed worktree gate recipe, extracted under a closed grammar.

    Attributes:
        all_lines: Every non-blank, non-comment command line in the fenced
            block, in file order.
        absence_guard: The G4 pre-creation absence-guard line.
        venv_create: The `python3.11 -m venv .venv` line.
        grep_line: The `grep -Fx` `pyvenv.cfg` containment check.
        pip_list: The informational `pip list` line.
        pip_upgrade: The `pip install --upgrade pip` line.
        pip_install: The editable install line.
        ruff_check: The `ruff check` gate line.
        ruff_format: The `ruff format --check` gate line.
        pyright_check: The `pyright` gate line.
        verification: The G8 post-strip verification line.
        pytest_gate: The pytest gate line.
        prefix_probe_source: The prefix-containment probe's Python source.
        first_party_probe_source: The first-party-containment probe's source.
        third_party_probe_source: The third-party-provenance probe's source.
    """

    all_lines: tuple[str, ...]
    absence_guard: str
    venv_create: str
    grep_line: str
    pip_list: str
    pip_upgrade: str
    pip_install: str
    ruff_check: str
    ruff_format: str
    pyright_check: str
    verification: str
    pytest_gate: str
    prefix_probe_source: str
    first_party_probe_source: str
    third_party_probe_source: str


def _extract_section(runbook_text: str) -> str:
    """Return the text strictly between the named heading and the next one.

    Args:
        runbook_text: The full runbook file contents.

    Returns:
        The section body (excluding the heading line itself).

    Raises:
        AssertionError: If the heading does not appear exactly once.
    """
    matches = list(_HEADING_PATTERN.finditer(runbook_text))
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {_SECTION_HEADING!r} heading, found {len(matches)}"
        )
    start = matches[0].end()
    next_heading = _NEXT_HEADING_PATTERN.search(runbook_text, start)
    end = next_heading.start() if next_heading is not None else len(runbook_text)
    return runbook_text[start:end]


def _extract_bash_block(section_text: str) -> str:
    """Return the sole fenced ```bash block's inner text.

    Args:
        section_text: The section body from :func:`_extract_section`.

    Returns:
        The fenced block's contents, excluding the fence lines.

    Raises:
        AssertionError: If there is not exactly one fenced `bash` block.
    """
    blocks = _BASH_BLOCK_PATTERN.findall(section_text)
    if len(blocks) != 1:
        raise AssertionError(f"expected exactly one fenced bash block, found {len(blocks)}")
    return blocks[0]


def _extract_command_lines(block_text: str) -> list[str]:
    """Return every non-blank, non-comment recipe line, closed-grammar checked.

    Args:
        block_text: The fenced block's inner text.

    Returns:
        Every conforming command line, in order.

    Raises:
        AssertionError: If the initial stop-on-failure preamble is absent, or
            a command does not start with the required `cd <abs worktree> && ` prefix.
    """
    lines: list[str] = []
    saw_stop_on_failure = False
    for raw_line in block_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == _STOP_ON_FAILURE:
            if saw_stop_on_failure or lines:
                raise AssertionError("stop-on-failure preamble must appear exactly once, first")
            saw_stop_on_failure = True
            continue
        if not raw_line.startswith(_RECIPE_LINE_PREFIX):
            raise AssertionError(f"recipe line does not match the closed grammar: {raw_line!r}")
        lines.append(raw_line)
    if not saw_stop_on_failure:
        raise AssertionError("recipe is missing its stop-on-failure preamble")
    return lines


def _unique_line(lines: Sequence[str], predicate: Callable[[str], bool], label: str) -> str:
    """Return the single line in `lines` matching `predicate`.

    Args:
        lines: The candidate command lines.
        predicate: A membership test for the wanted line.
        label: A human-readable name for error messages.

    Returns:
        The one matching line.

    Raises:
        AssertionError: If zero or more than one line matches.
    """
    matches = [line for line in lines if predicate(line)]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {label} line in the extracted recipe, found "
            f"{len(matches)}: {matches!r}"
        )
    return matches[0]


def _extract_probe_sources(lines: Sequence[str]) -> tuple[str, str, str]:
    """Return the three probes' Python sources, classified by content marker.

    Args:
        lines: The full extracted command-line list.

    Returns:
        `(prefix_source, first_party_source, third_party_source)`.

    Raises:
        AssertionError: If there are not exactly three probe lines, or a
            marker does not identify exactly one of them.
    """
    sources: list[str] = []
    for line in lines:
        match = _PROBE_LINE_PATTERN.match(line)
        if match is not None:
            sources.append(match.group("source"))
    if len(sources) != 3:
        raise AssertionError(
            f"expected exactly three probe lines, found {len(sources)}: {sources!r}"
        )

    def _one(keyword: str, excludes: tuple[str, ...]) -> str:
        matches = [s for s in sources if keyword in s and all(bad not in s for bad in excludes)]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one probe source containing {keyword!r} and "
                f"excluding {excludes!r}; found {len(matches)}: {matches!r}"
            )
        return matches[0]

    prefix_source = _one("sys.prefix", ("numpy", "roastpilot_agent"))
    first_party_source = _one("roastpilot_agent", ())
    third_party_source = _one("import numpy", ())
    return prefix_source, first_party_source, third_party_source


def _locate_verification_and_pytest(lines: Sequence[str]) -> tuple[str, str]:
    """Return `(verification_line, pytest_line)`, checking G8's adjacency.

    Args:
        lines: The full extracted command-line list.

    Returns:
        The verification line and the pytest gate line.

    Raises:
        AssertionError: If either line is not uniquely identifiable, or the
            verification line does not sit immediately before the pytest line.
    """
    pytest_gate = _unique_line(lines, lambda line: "-m pytest" in line, "pytest gate")
    verification = _unique_line(
        lines,
        lambda line: "leaked" in line and "-m pytest" not in line,
        "namespace-strip verification",
    )
    if lines.index(verification) != lines.index(pytest_gate) - 1:
        raise AssertionError(
            "the verification line must sit immediately before the pytest line (G8)"
        )
    return verification, pytest_gate


@functools.lru_cache(maxsize=1)
def _load_recipe() -> RecipeLines:
    """Parse the `#773` gate recipe out of the runbook under a closed grammar.

    Every extraction step raises rather than degrades to a skip, per the
    contract's "no test may `pytest.skip`" rule.

    Returns:
        The extracted, classified recipe lines.

    Raises:
        AssertionError: If the runbook's structure does not match the closed
            grammar this module depends on.
    """
    runbook_text = _RUNBOOK.read_text(encoding="utf-8")
    section_text = _extract_section(runbook_text)
    block_text = _extract_bash_block(section_text)
    lines = _extract_command_lines(block_text)

    absence_guard = _unique_line(lines, lambda line: "os.lstat" in line, "absence guard")
    venv_create = _unique_line(
        lines,
        lambda line: line == "cd <abs worktree> && python3.11 -m venv .venv",
        "venv creation",
    )
    if lines.index(absence_guard) >= lines.index(venv_create):
        raise AssertionError("the absence guard must precede venv creation (G4)")

    grep_line = _unique_line(lines, lambda line: "grep -Fx" in line, "pyvenv.cfg containment grep")
    pip_list = _unique_line(lines, lambda line: line.endswith("pip list"), "pip list")
    pip_upgrade = _unique_line(
        lines, lambda line: "pip install --upgrade pip" in line, "pip upgrade"
    )
    pip_install = _unique_line(
        lines, lambda line: "pip install -e . --group dev" in line, "editable install"
    )
    ruff_check = _unique_line(lines, lambda line: line.endswith("ruff check ."), "ruff check")
    ruff_format = _unique_line(lines, lambda line: "ruff format --check ." in line, "ruff format")
    pyright_check = _unique_line(lines, lambda line: line.endswith("-m pyright"), "pyright")
    verification, pytest_gate = _locate_verification_and_pytest(lines)

    prefix_source, first_party_source, third_party_source = _extract_probe_sources(lines)

    return RecipeLines(
        all_lines=tuple(lines),
        absence_guard=absence_guard,
        venv_create=venv_create,
        grep_line=grep_line,
        pip_list=pip_list,
        pip_upgrade=pip_upgrade,
        pip_install=pip_install,
        ruff_check=ruff_check,
        ruff_format=ruff_format,
        pyright_check=pyright_check,
        verification=verification,
        pytest_gate=pytest_gate,
        prefix_probe_source=prefix_source,
        first_party_probe_source=first_party_source,
        third_party_probe_source=third_party_source,
    )


def _assert_no_forbidden_literals(lines: Iterable[str]) -> None:
    """Raise if any line contains `--clear` or `rm -rf .venv/` (G5).

    Args:
        lines: The command lines to scan.

    Raises:
        AssertionError: If a forbidden literal is present.
    """
    offenders: list[str] = []
    for line in lines:
        for literal in _FORBIDDEN_LITERALS:
            if literal in line:
                offenders.append(line)
                break
    if offenders:
        raise AssertionError(f"forbidden literal found in recipe command lines: {offenders!r}")


def _env_prefix(pytest_gate_line: str) -> str:
    """Return the substring between `cd <abs worktree> && ` and ` .venv/bin/python `.

    Args:
        pytest_gate_line: The full pytest gate command line.

    Returns:
        The environment-stripping prefix.

    Raises:
        AssertionError: If the markers are not both present.
    """
    suffix_marker = " .venv/bin/python "
    try:
        start = pytest_gate_line.index(_RECIPE_LINE_PREFIX) + len(_RECIPE_LINE_PREFIX)
        end = pytest_gate_line.index(suffix_marker, start)
    except ValueError as exc:
        raise AssertionError(f"could not locate the env prefix in {pytest_gate_line!r}") from exc
    return pytest_gate_line[start:end]


def _python_c_source(line: str) -> str:
    """Return the single-quoted source from a trailing `.venv/bin/python -c '...'`.

    Args:
        line: A command line ending in a `.venv/bin/python -c '<source>'` clause.

    Returns:
        The extracted source.

    Raises:
        AssertionError: If no such trailing clause is present.
    """
    match = _PYTHON_C_SUFFIX_PATTERN.search(line)
    if match is None:
        raise AssertionError(f"no trailing .venv/bin/python -c '...' clause in {line!r}")
    return match.group("source")


def _run_source(
    source: str, worktree: Path | None = None, *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run extracted Python `source` with the current interpreter.

    Args:
        source: The probe's Python source, possibly containing the literal
            `<abs worktree>` placeholder.
        worktree: If given, substituted for every `<abs worktree>` occurrence.
        env: If given, the exact child environment; defaults to a copy of the
            current process environment.

    Returns:
        The completed subprocess result.
    """
    rendered = source.replace("<abs worktree>", str(worktree)) if worktree is not None else source
    run_env = dict(os.environ) if env is None else dict(env)
    return subprocess.run(
        [sys.executable, "-c", rendered], capture_output=True, text=True, env=run_env
    )


def _run_absence_guard(recipe: RecipeLines, worktree: Path) -> subprocess.CompletedProcess[str]:
    """Run the extracted absence-guard line, substituted for `worktree`.

    Args:
        recipe: The extracted recipe.
        worktree: The directory to substitute for `<abs worktree>`.

    Returns:
        The completed subprocess result.
    """
    rendered = recipe.absence_guard.replace("<abs worktree>", str(worktree))
    return subprocess.run(["bash", "-c", rendered], capture_output=True, text=True)


def _render_fail_fast_block(block_text: str, worktree: Path, marker: Path) -> str:
    """Render the whole block with later commands replaced by safe markers.

    Args:
        block_text: The extracted fenced Bash block.
        worktree: The temporary worktree substituted into the absence guard.
        marker: File that each safely substituted later command would append.

    Returns:
        A control-flow-equivalent script that preserves the real preamble and
        early absence guard without provisioning a virtual environment.
    """
    rendered: list[str] = []
    for raw_line in block_text.splitlines():
        if "os.lstat" in raw_line:
            rendered.append(raw_line.replace("<abs worktree>", str(worktree)))
        elif raw_line.startswith(_RECIPE_LINE_PREFIX):
            rendered.append(f"printf 'later command ran\\n' >> {shlex.quote(str(marker))}")
        else:
            rendered.append(raw_line)
    return "\n".join(rendered)


def _inspector_command(prefix: str, source: str) -> str:
    """Build `<prefix> <sys.executable> -c <source>` as one shell command.

    Args:
        prefix: The environment-stripping prefix (e.g. from :func:`_env_prefix`).
        source: The inspector's Python source.

    Returns:
        A single shell command string, safe for `bash -c`.
    """
    return f"{prefix} {shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _poisoned_env() -> dict[str, str]:
    """Return a copy of the current environment with every poison value set.

    Returns:
        The poisoned environment mapping.
    """
    env = dict(os.environ)
    env.update(_ALL_POISON_VALUES)
    return env


def _parse_leaked(stdout: str) -> list[str]:
    """Parse the inspector's printed Python list literal of leaked names.

    Args:
        stdout: The inspector subprocess's captured stdout.

    Returns:
        The leaked key names.

    Raises:
        AssertionError: If the last non-blank stdout line is not a list
            literal.
    """
    non_blank = [line for line in stdout.splitlines() if line.strip()]
    if not non_blank:
        raise AssertionError(f"inspector produced no stdout: {stdout!r}")
    value: object = ast.literal_eval(non_blank[-1])
    if not isinstance(value, list):
        raise AssertionError(f"expected a list literal, got {non_blank[-1]!r}")
    parsed_items: list[str] = []
    for item in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str):
            raise AssertionError(f"expected a list of str, got an element {item!r}")
        parsed_items.append(item)
    return parsed_items


def _assert_no_poisoned_values(output: str) -> None:
    """Raise when captured output discloses any synthetic poison value.

    Args:
        output: Combined captured stdout and stderr from a gate subprocess.

    Raises:
        AssertionError: If any synthetic poisoned value appears in `output`.
    """
    for name, value in _ALL_POISON_VALUES.items():
        assert value not in output, f"{name}'s poisoned VALUE leaked into gate output"


def _outer_site_packages() -> str:
    """Return this interpreter's first site-packages directory (for T5).

    Returns:
        The site-packages path.
    """
    candidates = site.getsitepackages()
    if not candidates:
        pytest.fail("could not determine this interpreter's site-packages for T5")
    return candidates[0]


# --------------------------------------------------------------------------
# Structural sanity (ties the closed-grammar parse to Class A's "14 matches")
# --------------------------------------------------------------------------


@pytest.mark.docs
def test_recipe_extraction_is_well_formed() -> None:
    """The closed-grammar parse succeeds and matches the Class A line count."""
    recipe = _load_recipe()
    block = _extract_bash_block(_extract_section(_RUNBOOK.read_text(encoding="utf-8")))
    assert next(line for line in block.splitlines() if line.strip()) == _STOP_ON_FAILURE
    assert len(recipe.all_lines) == 14
    assert recipe.all_lines[0] == recipe.absence_guard
    assert recipe.all_lines[1] == recipe.venv_create
    assert recipe.all_lines[-1] == recipe.pytest_gate
    assert recipe.all_lines[-2] == recipe.verification


@pytest.mark.docs
def test_t0_complete_recipe_stops_before_later_commands_when_guard_fails(tmp_path: Path) -> None:
    """T0: a copied complete recipe exits non-zero before any later command runs."""
    (tmp_path / ".venv").mkdir()
    block = _extract_bash_block(_extract_section(_RUNBOOK.read_text(encoding="utf-8")))
    marker = tmp_path / "later-command-ran"
    script = _render_fail_fast_block(block, tmp_path, marker)

    guarded = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert guarded.returncode != 0
    assert not marker.exists()

    mutated = script.replace(f"{_STOP_ON_FAILURE}\n", "", 1)
    bypassed = subprocess.run(["bash", "-c", mutated], capture_output=True, text=True)
    assert bypassed.returncode == 0
    assert marker.exists()


# --------------------------------------------------------------------------
# AC1 — probe exit status (T1-T7)
# --------------------------------------------------------------------------


@pytest.mark.docs
def test_t1_prefix_probe_exits_zero_when_contained() -> None:
    """T1: the prefix probe exits 0 and prints path+True when contained."""
    recipe = _load_recipe()
    result = _run_source(recipe.prefix_probe_source, Path(sys.prefix).resolve())
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 2
    assert lines[1] == "True"


@pytest.mark.docs
def test_t2_prefix_probe_exits_nonzero_when_borrowed(tmp_path: Path) -> None:
    """T2: the prefix probe exits non-zero against an unrelated directory."""
    recipe = _load_recipe()
    result = _run_source(recipe.prefix_probe_source, tmp_path)
    assert result.returncode != 0
    assert "False" in result.stdout


@pytest.mark.docs
def test_t3_first_party_probe_contained_and_borrowed(tmp_path: Path) -> None:
    """T3: the first-party probe exits 0 for the real repo, non-zero otherwise."""
    recipe = _load_recipe()
    contained = _run_source(recipe.first_party_probe_source, _REPO)
    assert contained.returncode == 0, contained.stdout + contained.stderr
    assert "True" in contained.stdout

    borrowed = _run_source(recipe.first_party_probe_source, tmp_path)
    assert borrowed.returncode != 0
    assert "False" in borrowed.stdout


@pytest.mark.docs
def test_t4_third_party_probe_contained() -> None:
    """T4: the numpy probe exits 0 under this interpreter's own venv."""
    recipe = _load_recipe()
    result = _run_source(recipe.third_party_probe_source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "True" in result.stdout


@pytest.mark.docs
def test_t5_third_party_probe_contaminated(tmp_path: Path) -> None:
    """T5: a throwaway venv resolving numpy via PYTHONPATH exits non-zero."""
    recipe = _load_recipe()
    throwaway = tmp_path / "throwaway-venv"
    build = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(throwaway)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"could not construct T5's throwaway venv: {build.stdout} {build.stderr}")
    throwaway_python = throwaway / "bin" / "python"
    if not throwaway_python.exists():
        pytest.fail(f"throwaway venv interpreter missing at {throwaway_python}")

    env = dict(os.environ)
    env["PYTHONPATH"] = _outer_site_packages()
    result = subprocess.run(
        [str(throwaway_python), "-c", recipe.third_party_probe_source],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, (
        f"T5 contamination did not reproduce on this runner: {result.stdout} {result.stderr}"
    )
    assert "False" in result.stdout


@pytest.mark.docs
def test_t6_probes_still_fail_closed_under_pythonoptimize(tmp_path: Path) -> None:
    """T6: every negative arm above still exits non-zero under PYTHONOPTIMIZE=2 (G2)."""
    recipe = _load_recipe()
    env = dict(os.environ)
    env["PYTHONOPTIMIZE"] = "2"

    prefix_result = _run_source(recipe.prefix_probe_source, tmp_path, env=env)
    assert prefix_result.returncode != 0

    first_party_result = _run_source(recipe.first_party_probe_source, tmp_path, env=env)
    assert first_party_result.returncode != 0

    throwaway = tmp_path / "throwaway-venv-optimize"
    build = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(throwaway)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"could not construct T6's throwaway venv: {build.stdout} {build.stderr}")
    throwaway_python = throwaway / "bin" / "python"
    contaminated_env = dict(env)
    contaminated_env["PYTHONPATH"] = _outer_site_packages()
    third_party_result = subprocess.run(
        [str(throwaway_python), "-c", recipe.third_party_probe_source],
        capture_output=True,
        text=True,
        env=contaminated_env,
    )
    assert third_party_result.returncode != 0


@pytest.mark.docs
def test_t7_probes_print_path_and_boolean_on_positive_arm() -> None:
    """T7: every probe prints exactly a path line then a boolean line, positive arm."""
    recipe = _load_recipe()
    for source, worktree in (
        (recipe.prefix_probe_source, Path(sys.prefix).resolve()),
        (recipe.first_party_probe_source, _REPO),
    ):
        result = _run_source(source, worktree)
        lines = [line for line in result.stdout.splitlines() if line]
        assert len(lines) == 2, f"expected path+boolean lines, got {lines!r}"
        assert lines[1] == "True"

    third_party_result = _run_source(recipe.third_party_probe_source)
    lines = [line for line in third_party_result.stdout.splitlines() if line]
    assert len(lines) == 2
    assert lines[1] == "True"


# --------------------------------------------------------------------------
# AC2 — pre-existing `.venv` (T8-T14)
# --------------------------------------------------------------------------


@pytest.mark.docs
def test_t8_absence_guard_exits_zero_when_venv_absent(tmp_path: Path) -> None:
    """T8: the absence guard exits 0 for a clean worktree."""
    result = _run_absence_guard(_load_recipe(), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.docs
def test_t9_absence_guard_exits_nonzero_for_real_directory(tmp_path: Path) -> None:
    """T9: a real `.venv` directory fails the guard."""
    (tmp_path / ".venv").mkdir()
    result = _run_absence_guard(_load_recipe(), tmp_path)
    assert result.returncode != 0


@pytest.mark.docs
def test_t10_absence_guard_exits_nonzero_for_live_symlink(tmp_path: Path) -> None:
    """T10: a live symlinked `.venv` fails the guard."""
    target = tmp_path / "other-checkout-venv"
    target.mkdir()
    (tmp_path / ".venv").symlink_to(target)
    result = _run_absence_guard(_load_recipe(), tmp_path)
    assert result.returncode != 0


@pytest.mark.docs
def test_t11_absence_guard_exits_nonzero_for_dangling_symlink(tmp_path: Path) -> None:
    """T11: a dangling symlinked `.venv` fails the guard (the lexists case)."""
    (tmp_path / ".venv").symlink_to(tmp_path / "does-not-exist")
    result = _run_absence_guard(_load_recipe(), tmp_path)
    assert result.returncode != 0


@pytest.mark.docs
def test_t12_absence_guard_exits_nonzero_for_regular_file(tmp_path: Path) -> None:
    """T12: a regular file named `.venv` fails the guard."""
    (tmp_path / ".venv").write_text("not a venv")
    result = _run_absence_guard(_load_recipe(), tmp_path)
    assert result.returncode != 0


@pytest.mark.docs
def test_t13_no_forbidden_literals_in_command_lines() -> None:
    """T13: the recipe's command lines carry no `--clear` and no `rm -rf .venv/`."""
    recipe = _load_recipe()
    _assert_no_forbidden_literals(recipe.all_lines)


@pytest.mark.docs
def test_t14_canonical_remedy_sentence_present_byte_exact() -> None:
    """T14: the closed rebuild and serialization disclosures are byte-exact."""
    runbook_text = _RUNBOOK.read_text(encoding="utf-8")
    assert CANONICAL_REMEDY_SENTENCE in runbook_text
    assert ABSENCE_GUARD_SERIALIZATION_SENTENCE in runbook_text
    assert ADJACENT_SCRUB_SERIALIZATION_SENTENCE in runbook_text


# --------------------------------------------------------------------------
# AC3 — `ROASTPILOT_*` namespace strip (T15-T19)
# --------------------------------------------------------------------------


@pytest.mark.docs
def test_t15_full_strip_reports_zero_leaks() -> None:
    """T15: the extracted strip prefix removes the whole poisoned namespace."""
    recipe = _load_recipe()
    prefix = _env_prefix(recipe.pytest_gate)
    source = _python_c_source(recipe.verification)
    result = subprocess.run(
        ["bash", "-c", _inspector_command(prefix, source)],
        capture_output=True,
        text=True,
        env=_poisoned_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _parse_leaked(result.stdout) == []


@pytest.mark.docs
def test_t16_verification_fails_closed_when_strip_bypassed() -> None:
    """T16: running the verification's inspector with no strip fails closed."""
    recipe = _load_recipe()
    source = _python_c_source(recipe.verification)
    poisoned = _poisoned_env()
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, env=poisoned
    )
    assert result.returncode != 0
    leaked = _parse_leaked(result.stdout)
    assert set(_ALL_POISON_VALUES) <= set(leaked)


@pytest.mark.docs
def test_t17_no_poisoned_value_disclosed() -> None:
    """T17: neither T15's nor T16's combined output ever prints a poisoned VALUE."""
    recipe = _load_recipe()
    prefix = _env_prefix(recipe.pytest_gate)
    source = _python_c_source(recipe.verification)
    poisoned = _poisoned_env()

    strip_applied = subprocess.run(
        ["bash", "-c", _inspector_command(prefix, source)],
        capture_output=True,
        text=True,
        env=poisoned,
    )
    strip_bypassed = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, env=poisoned
    )

    combined = (
        strip_applied.stdout + strip_applied.stderr + strip_bypassed.stdout + strip_bypassed.stderr
    )
    _assert_no_poisoned_values(combined)


@pytest.mark.docs
def test_t18_pythonpath_and_api_key_also_isolated() -> None:
    """T18: the full-strip inspector run also confirms PYTHONPATH/OPENROUTER_API_KEY
    absence (G9)."""
    recipe = _load_recipe()
    assert "-u PYTHONPATH" in recipe.pytest_gate
    assert "-u OPENROUTER_API_KEY" in recipe.pytest_gate
    prefix = _env_prefix(recipe.pytest_gate)
    source = _python_c_source(recipe.verification)
    result = subprocess.run(
        ["bash", "-c", _inspector_command(prefix, source)],
        capture_output=True,
        text=True,
        env=_poisoned_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    leaked = _parse_leaked(result.stdout)
    assert "PYTHONPATH" not in leaked
    assert "OPENROUTER_API_KEY" not in leaked


@pytest.mark.parametrize(
    "environment_name",
    (
        "ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE",
        "roastpilot_advisor__model_slug_by_phase",
        "RoAsTpIlOt_AdViSoR__MoDeL_SlUg_By_PhAsE",
    ),
)
@pytest.mark.slow
@pytest.mark.docs
def test_t19_pytest_gate_strips_ambient_phase_map(tmp_path: Path, environment_name: str) -> None:
    """T19 (load-bearing, #773's named case): the ambient phase map really
    inverts `test_configured_model_slug_governs_the_phase_that_consults`
    without the strip, and the extracted strip prefix rescues it.
    """
    recipe = _load_recipe()
    prefix = _env_prefix(recipe.pytest_gate)
    target = "tests/test_config.py::test_configured_model_slug_governs_the_phase_that_consults"

    poisoned_env = dict(os.environ)
    poisoned_env[environment_name] = _POISON_VALUES["ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE"]

    unstripped_basetemp = tmp_path / "unstripped"
    stripped_basetemp = tmp_path / "stripped"
    unstripped_basetemp.mkdir()
    stripped_basetemp.mkdir()

    unstripped = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            target,
            "--basetemp",
            str(unstripped_basetemp),
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=_REPO,
        env=poisoned_env,
        capture_output=True,
        text=True,
    )
    assert unstripped.returncode != 0, (
        "T19 premise did not reproduce: an ambient ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE "
        f"did not fail the named test.\nstdout:\n{unstripped.stdout}\nstderr:\n{unstripped.stderr}"
    )

    stripped_cmd = (
        f"{prefix} {shlex.quote(sys.executable)} -m pytest {shlex.quote(target)} "
        f"--basetemp {shlex.quote(str(stripped_basetemp))} -p no:cacheprovider -q"
    )
    stripped = subprocess.run(
        ["bash", "-c", stripped_cmd],
        cwd=_REPO,
        env=poisoned_env,
        capture_output=True,
        text=True,
    )
    assert stripped.returncode == 0, (
        "T19: the strip did not rescue the test.\n"
        f"stdout:\n{stripped.stdout}\nstderr:\n{stripped.stderr}"
    )


# --------------------------------------------------------------------------
# Routed-citation integrity (T20)
# --------------------------------------------------------------------------

_QUOTED_HEADING_PATTERN = re.compile(r'\*\*"([^"]+)"\*\*')


def _quoted_gate_headings() -> set[str]:
    """Scan `.claude/agents/*.md` for quoted gate-environment section names.

    Returns:
        Every distinct, whitespace-normalised quoted heading naming the
        gate-environment section.
    """
    headings: set[str] = set()
    for path in sorted((_REPO / ".claude" / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in _QUOTED_HEADING_PATTERN.finditer(text):
            candidate = " ".join(match.group(1).split())
            if "gate environment" in candidate.lower():
                headings.add(candidate)
    return headings


def _heading_lines(runbook_text: str) -> set[str]:
    """Return every level-two (`## `) heading text in `runbook_text`.

    Args:
        runbook_text: Markdown text to scan.

    Returns:
        The set of heading texts (without the `## ` marker).
    """
    return {line[3:].strip() for line in runbook_text.splitlines() if line.startswith("## ")}


@pytest.mark.docs
def test_t20_section_heading_matches_role_prompt_citations() -> None:
    """T20: the runbook heading still matches what every role prompt quotes."""
    headings = _quoted_gate_headings()
    assert headings, "no role prompt quoted a gate-environment heading"
    present = _heading_lines(_RUNBOOK.read_text(encoding="utf-8"))
    missing = headings - present
    assert not missing, f"role-prompt-quoted headings missing from the runbook: {missing}"


# --------------------------------------------------------------------------
# Mutation-style checks — one per guard (§3.5)
# --------------------------------------------------------------------------


@pytest.mark.docs
def test_mutation_g1_print_only_probe_exits_zero_when_borrowed(tmp_path: Path) -> None:
    """G1 mutation: dropping SystemExit restores the historic print-only form,
    which must fail T2/T3-negative/T5 (proving they are non-vacuous)."""
    recipe = _load_recipe()
    mutant_source = re.sub(r";\s*raise SystemExit\([^()]*\)$", "", recipe.prefix_probe_source)
    assert mutant_source != recipe.prefix_probe_source
    result = _run_source(mutant_source, tmp_path)
    assert result.returncode == 0, "print-only mutant unexpectedly failed closed (test is vacuous)"
    assert "False" in result.stdout


@pytest.mark.docs
def test_mutation_g2_assert_based_probe_passes_under_pythonoptimize(tmp_path: Path) -> None:
    """G2 mutation: an assert-based probe must fail T6 under PYTHONOPTIMIZE=2."""
    recipe = _load_recipe()
    mutant_source = re.sub(
        r";\s*raise SystemExit\(0 if ok else 1\)$", "; assert ok", recipe.prefix_probe_source
    )
    assert mutant_source != recipe.prefix_probe_source
    env = dict(os.environ)
    env["PYTHONOPTIMIZE"] = "2"
    result = _run_source(mutant_source, tmp_path, env=env)
    assert result.returncode == 0, (
        "assert-based mutant unexpectedly failed closed under -O (test is vacuous)"
    )


@pytest.mark.docs
def test_mutation_g3_relaxed_grep_would_false_pass(tmp_path: Path) -> None:
    """G3 mutation: relaxing `-Fx` to a substring `-F` would false-pass a
    `... = true` line the real `-Fx` line correctly rejects."""
    recipe = _load_recipe()
    assert "-Fx" in recipe.grep_line

    cfg = tmp_path / "pyvenv.cfg"
    cfg.write_text("include-system-site-packages = true\n")

    exact = subprocess.run(
        ["grep", "-Fx", "include-system-site-packages = false", str(cfg)],
        capture_output=True,
        text=True,
    )
    assert exact.returncode != 0

    relaxed = subprocess.run(
        ["grep", "-F", "include-system-site-packages =", str(cfg)],
        capture_output=True,
        text=True,
    )
    assert relaxed.returncode == 0, "relaxed -F mutant failed to demonstrate the false-pass hazard"


def test_mutation_g4_exists_based_guard_fails_open_on_dangling_symlink(tmp_path: Path) -> None:
    """G4 mutation: `lexists` -> `exists` fails open on a dangling symlink (fails T11)."""
    (tmp_path / ".venv").symlink_to(tmp_path / "does-not-exist")
    mutant_source = (
        "from pathlib import Path\n"
        'p = Path(".venv")\n'
        "exists = p.exists()\n"  # follows symlinks: False for a dangling link (the G4 defect)
        "print(p.resolve() if not exists else p)\n"
        "print(not exists)\n"
        "raise SystemExit(1 if exists else 0)\n"
    )
    command = f"cd {shlex.quote(str(tmp_path))} && python3.11 -c {shlex.quote(mutant_source)}"
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode == 0, (
        "exists()-based mutant unexpectedly failed closed (test is vacuous)"
    )


@pytest.mark.docs
def test_mutation_g5_forbidden_literals_detected_by_scanner() -> None:
    """G5 mutation: reintroducing `--clear` or `rm -rf .venv/` must fail T13's scanner."""
    recipe = _load_recipe()
    _assert_no_forbidden_literals(recipe.all_lines)  # positive: the real recipe passes

    clear_mutant = "cd <abs worktree> && python3.11 -m venv .venv --clear"
    with pytest.raises(AssertionError):
        _assert_no_forbidden_literals((*recipe.all_lines, clear_mutant))

    rm_mutant = "cd <abs worktree> && rm -rf .venv/"
    with pytest.raises(AssertionError):
        _assert_no_forbidden_literals((*recipe.all_lines, rm_mutant))


def test_mutation_g6_bare_rebuild_remedy_fails_t14() -> None:
    """G6 mutation: the historic bare "rebuild the venv" phrasing fails T14's anchor."""
    historic_phrasing = "discard the gate result and rebuild the venv."
    assert CANONICAL_REMEDY_SENTENCE not in historic_phrasing


@pytest.mark.docs
def test_mutation_g7_single_key_prefix_leaks_the_namespace() -> None:
    """G7 mutation: reinstating the historic single-key strip leaks `ROASTPILOT_*` (fails T15).

    T19's own unstripped arm already reproduces the same failure end to end
    against the real named test; this checks the mechanism directly.
    """
    recipe = _load_recipe()
    historic_prefix = "env -u PYTHONPATH -u OPENROUTER_API_KEY"
    source = _python_c_source(recipe.verification)
    poisoned = _poisoned_env()
    poisoned.pop("PYTHONPATH", None)  # the historic prefix DOES still unset this one
    result = subprocess.run(
        ["bash", "-c", _inspector_command(historic_prefix, source)],
        capture_output=True,
        text=True,
        env=poisoned,
    )
    assert result.returncode != 0, "historic single-key prefix unexpectedly leaked nothing"
    leaked = _parse_leaked(result.stdout)
    assert "ROASTPILOT_DB" in leaked
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE" in leaked


@pytest.mark.docs
def test_mutation_g8_missing_verification_line_fails_structural_check() -> None:
    """G8 mutation: deleting the verification line fails the structural parse (fails T16)."""
    recipe = _load_recipe()
    mutated = tuple(line for line in recipe.all_lines if line != recipe.verification)
    with pytest.raises(AssertionError):
        _locate_verification_and_pytest(mutated)


@pytest.mark.docs
def test_mutation_g9_g10_value_echoing_prefix_leaks_a_value() -> None:
    """G9/G10 mutation: the extracted verifier must never echo a value.

    The mutant removes the actual dynamic namespace strip from the extracted
    prefix and rewrites the actual verifier's `print(leaked)` call to echo the
    named values. It then runs through T15/T17's bash inspection route, so the
    exact value-disclosure comparison used by T17 must reject its output.
    """
    recipe = _load_recipe()
    prefix = _env_prefix(recipe.pytest_gate)
    before_namespace_strip, separator, _ = prefix.partition(" $(env | awk ")
    assert separator, "extracted strip prefix lost its dynamic namespace removal"
    source = _python_c_source(recipe.verification)
    mutant_source = source.replace(
        "print(leaked)", "print([(key, os.environ[key]) for key in leaked])"
    )
    assert mutant_source != source

    mutant = subprocess.run(
        ["bash", "-c", _inspector_command(before_namespace_strip, mutant_source)],
        capture_output=True,
        text=True,
        env=_poisoned_env(),
    )
    captured = mutant.stdout + mutant.stderr
    assert _POISON_VALUES["ROASTPILOT_DB"] in captured, (
        "mutant did not disclose a synthetic poisoned value (test is vacuous)"
    )
    with pytest.raises(AssertionError):
        _assert_no_poisoned_values(captured)


def test_mutation_g8_heading_rename_fails_t20() -> None:
    """G8-heading mutation: renaming the runbook section heading fails T20."""
    headings = _quoted_gate_headings()
    assert headings
    mutated_runbook_text = "## Some Renamed Heading\n\nbody text\n"
    present = _heading_lines(mutated_runbook_text)
    missing = headings - present
    assert missing, (
        "mutation check is vacuous: a renamed heading still satisfied T20's citation set"
    )
