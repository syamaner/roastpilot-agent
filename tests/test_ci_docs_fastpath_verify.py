"""Unit and real-Git tests for the docs-fastpath independent re-verification."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import ci_change_classifier
import ci_docs_fastpath_verify as verify
import pytest

_BASE = "a" * 40
_HEAD = "b" * 40


def _fake_classify(
    mode: ci_change_classifier.ChangeMode,
) -> Callable[[str, str, str], ci_change_classifier.ChangeMode]:
    """Return a stand-in for `classify_change` that always resolves to `mode`."""

    def classify(event_name: str, base_sha: str, head_sha: str) -> ci_change_classifier.ChangeMode:
        del event_name, base_sha, head_sha
        return mode

    return classify


def _fake_recompute(paths: tuple[str, ...]) -> Callable[[str, str], tuple[str, ...]]:
    """Return a stand-in for `_recomputed_docs_only_paths` that returns `paths`."""

    def recompute(base_sha: str, head_sha: str) -> tuple[str, ...]:
        del base_sha, head_sha
        return paths

    return recompute


@pytest.mark.docs_ci
def test_verify_passes_when_classifier_and_narrowing_check_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine docs-only verdict, confirmed by the narrowing redundancy, passes."""

    monkeypatch.setattr(
        verify, "classify_change", _fake_classify(ci_change_classifier.ChangeMode.DOCS_ONLY)
    )
    monkeypatch.setattr(verify, "_recomputed_docs_only_paths", _fake_recompute(("docs/guide.md",)))
    verify.verify_docs_only(_BASE, _HEAD)


@pytest.mark.docs_ci
def test_verify_polarity_rejects_a_non_docs_only_classifier_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M21: the verifier must fail on any verdict other than DOCS_ONLY, never pass it."""

    monkeypatch.setattr(
        verify, "classify_change", _fake_classify(ci_change_classifier.ChangeMode.FULL)
    )
    with pytest.raises(verify.DocsFastpathVerificationError, match="not docs-only"):
        verify.verify_docs_only(_BASE, _HEAD)


@pytest.mark.docs_ci
def test_narrowing_check_rejects_a_path_outside_docs_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M22: the narrowing redundancy must reject any recomputed non-docs/*.md path."""

    monkeypatch.setattr(
        verify, "classify_change", _fake_classify(ci_change_classifier.ChangeMode.DOCS_ONLY)
    )
    monkeypatch.setattr(
        verify, "_recomputed_docs_only_paths", _fake_recompute(("docs/guide.md", "src/leak.py"))
    )
    with pytest.raises(verify.DocsFastpathVerificationError, match="outside docs"):
        verify.verify_docs_only(_BASE, _HEAD)


@pytest.mark.docs_ci
def test_narrowing_check_never_prints_the_unbounded_path_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded diagnostic never leaks the full rejected-path list."""

    monkeypatch.setattr(
        verify, "classify_change", _fake_classify(ci_change_classifier.ChangeMode.DOCS_ONLY)
    )
    rejected = tuple(f"src/leak-{index}.py" for index in range(10))
    monkeypatch.setattr(verify, "_recomputed_docs_only_paths", _fake_recompute(rejected))
    with pytest.raises(verify.DocsFastpathVerificationError) as excinfo:
        verify.verify_docs_only(_BASE, _HEAD)
    message = str(excinfo.value)
    assert "leak-9.py" not in message
    assert "+5 more" in message


@pytest.mark.docs_ci
def test_recomputation_git_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Git failure during independent recomputation is reported, not silently ignored."""

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(returncode=1, cmd=["git", "diff"])

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    with pytest.raises(verify.DocsFastpathVerificationError, match="Git recomputation failed"):
        verify._recomputed_docs_only_paths(_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_recomputation_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Git timeout during independent recomputation is reported."""

    def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=arguments, timeout=20.0)

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    with pytest.raises(verify.DocsFastpathVerificationError, match="Git recomputation failed"):
        verify._recomputed_docs_only_paths(_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_recomputation_empty_changed_set_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty independently-recomputed changed-path set fails closed."""

    def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if arguments[1] == "merge-base":
            return subprocess.CompletedProcess(arguments, 0, stdout=b"c" * 40 + b"\n")
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    with pytest.raises(verify.DocsFastpathVerificationError, match="no changed paths"):
        verify._recomputed_docs_only_paths(_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_recomputation_undecodable_path_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-UTF-8 changed path from the independent recomputation fails closed."""

    def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if arguments[1] == "merge-base":
            return subprocess.CompletedProcess(arguments, 0, stdout=b"c" * 40 + b"\n")
        return subprocess.CompletedProcess(arguments, 0, stdout=b"docs/\xffbad.md\0")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    with pytest.raises(verify.DocsFastpathVerificationError, match="not valid UTF-8"):
        verify._recomputed_docs_only_paths(_BASE, _HEAD)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.docs_ci
def test_main_exits_zero_on_agreement(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` reports exit 0 when the full verification succeeds."""

    def succeed(base_sha: str, head_sha: str) -> None:
        del base_sha, head_sha

    monkeypatch.setattr(verify, "verify_docs_only", succeed)
    assert verify.main(["--base-sha", _BASE, "--head-sha", _HEAD]) == 0


@pytest.mark.docs_ci
def test_main_exits_nonzero_and_reports_on_disagreement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` reports a non-zero exit and a stderr message on verification failure."""

    def fail(*_a: object) -> None:
        raise verify.DocsFastpathVerificationError("classifier and narrowing check disagree")

    monkeypatch.setattr(verify, "verify_docs_only", fail)
    exit_code = verify.main(["--base-sha", _BASE, "--head-sha", _HEAD])
    assert exit_code == 1
    assert "docs-fastpath re-verification failed" in capsys.readouterr().err


@pytest.mark.docs_ci
def test_main_fails_closed_on_a_genuinely_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unexpected exception is a backstop failure, never a silent pass."""

    def explode(*_a: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(verify, "verify_docs_only", explode)
    exit_code = verify.main(["--base-sha", _BASE, "--head-sha", _HEAD])
    assert exit_code == 1
    assert "unexpected RuntimeError" in capsys.readouterr().err


@pytest.mark.docs_ci
def test_writes_nothing_to_github_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.5: the verifier never touches GITHUB_OUTPUT."""

    monkeypatch.setenv("GITHUB_OUTPUT", "/dev/null")
    monkeypatch.setattr(
        verify, "classify_change", _fake_classify(ci_change_classifier.ChangeMode.DOCS_ONLY)
    )
    monkeypatch.setattr(verify, "_recomputed_docs_only_paths", _fake_recompute(("docs/guide.md",)))
    # Passing (no exception) with no GITHUB_OUTPUT write attempted is the
    # whole assertion here: the module never imports/uses `os.environ` for
    # GITHUB_OUTPUT at all, unlike `ci_change_classifier`.
    verify.verify_docs_only(_BASE, _HEAD)
    assert "GITHUB_OUTPUT" not in Path(verify.__file__).read_text(encoding="utf-8")


@pytest.mark.slow
def test_real_git_full_pipeline_end_to_end(tmp_path: Path) -> None:
    """A real docs-only commit pair passes the full classifier + narrowing pipeline."""

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "rp702@example.invalid")
    git("config", "user.name", "RP702 Verify Fixture")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("v1\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "seed")
    base = git("rev-parse", "HEAD")
    (repo / "docs" / "guide.md").write_text("v2\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "edit docs")
    head = git("rev-parse", "HEAD")

    import os

    original_cwd = Path.cwd()
    try:
        os.chdir(repo)
        verify.verify_docs_only(base, head)
    finally:
        os.chdir(original_cwd)
