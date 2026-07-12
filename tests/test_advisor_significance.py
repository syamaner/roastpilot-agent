"""Tests for the significance-stats reproduction harness (#495).

Network-free: everything here is either a tiny synthetic paired artifact with
a hand-computed expected value, or a read of the committed
``docs/advisor/bakeoff-results-prompts-2026-06-14.json`` artifact that the
D34 prompt-v4 claim in ``docs/advisor/experiment.md`` is derived from.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import advisor_significance as sig  # noqa: E402

_REAL_ARTIFACT = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "advisor"
    / "bakeoff-results-prompts-2026-06-14.json"
)


def _write_artifact(
    path: Path,
    *,
    pinned_model: str = "fake/model",
    mode: str = "replay",
    test_set: str = "synthetic",
    baseline_roasts: list[tuple[str, bool, float]],
    candidate_roasts: list[tuple[str, bool, float]],
    baseline_version: str = "v2",
    candidate_version: str = "v4",
) -> Path:
    """Write a tiny bake-off-shaped JSON artifact for one prompt pair.

    Args:
        path: File to write.
        pinned_model: The artifact's ``pinned_model`` field.
        mode: The artifact's ``mode`` field.
        test_set: The artifact's ``test_set`` field.
        baseline_roasts: ``(roast_name, called_drop, f1)`` rows for the
            baseline prompt's cell.
        candidate_roasts: Same shape for the candidate prompt's cell.
        baseline_version: The baseline cell's ``prompt_version``.
        candidate_version: The candidate cell's ``prompt_version``.

    Returns:
        The written path.
    """

    def _cell(version: str, rows: list[tuple[str, bool, float]]) -> dict[str, object]:
        return {
            "slug": pinned_model,
            "tier": "test",
            "prompt_version": version,
            "latency_risk": False,
            "scores": [
                {
                    "roast_name": name,
                    "drop": {
                        "recall": 1.0 if called else 0.0,
                        "f1": f1,
                    },
                }
                for name, called, f1 in rows
            ],
        }

    artifact = {
        "mode": mode,
        "test_set": test_set,
        "pinned_model": pinned_model,
        "prompt_versions": [baseline_version, candidate_version],
        "cells": [
            _cell(baseline_version, baseline_roasts),
            _cell(candidate_version, candidate_roasts),
        ],
    }
    path.write_text(json.dumps(artifact))
    return path


# --- McNemar -----------------------------------------------------------


def test_mcnemar_exact_matches_hand_computed_value(tmp_path: Path) -> None:
    """b=3, c=0 discordant pairs: exact p = 2 * C(3,0) * 0.5**3 = 0.25.

    Hand computation: n = b + c = 3 discordant pairs, all favoring the
    candidate (b=0 baseline-only, c=3 candidate-only in this test's naming
    -- see the roast rows below). The exact two-sided McNemar p-value is
    2 * sum_{k=0}^{min(b,c)} C(n,k) * 0.5**n = 2 * C(3,0) * (1/8) = 0.25.
    """
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[
            ("r1", False, 0.0),
            ("r2", False, 0.0),
            ("r3", False, 0.0),
            ("r4", True, 1.0),  # concordant: both catch it
        ],
        candidate_roasts=[
            ("r1", True, 1.0),  # candidate-only catch
            ("r2", True, 1.0),  # candidate-only catch
            ("r3", True, 1.0),  # candidate-only catch
            ("r4", True, 1.0),  # concordant
        ],
    )
    provenance, pairs = sig.load_paired_outcomes(artifact)
    assert provenance.n_roasts == 4
    result = sig.mcnemar_exact(pairs)
    assert result.both_yes == 1
    assert result.both_no == 0
    assert result.baseline_only == 0
    assert result.candidate_only == 3
    assert result.discordant_n == 3
    assert result.exact_p_two_sided == pytest.approx(0.25)


def test_mcnemar_exact_symmetric_discordant_pairs(tmp_path: Path) -> None:
    """b=4, c=1 (mixed direction): exact p = 2 * sum_{k=0}^{1} C(5,k) * 0.5**5 = 0.375."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[
            ("r1", True, 1.0),
            ("r2", True, 1.0),
            ("r3", True, 1.0),
            ("r4", True, 1.0),
            ("r5", False, 0.0),
        ],
        candidate_roasts=[
            ("r1", False, 0.0),
            ("r2", False, 0.0),
            ("r3", False, 0.0),
            ("r4", False, 0.0),
            ("r5", True, 1.0),
        ],
    )
    _, pairs = sig.load_paired_outcomes(artifact)
    result = sig.mcnemar_exact(pairs)
    assert result.baseline_only == 4
    assert result.candidate_only == 1
    assert result.discordant_n == 5
    assert result.exact_p_two_sided == pytest.approx(0.375)


def test_mcnemar_no_discordant_pairs_returns_p_one(tmp_path: Path) -> None:
    """No disagreement at all: nothing for McNemar to test, p defined as 1.0."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", True, 1.0), ("r2", False, 0.0)],
        candidate_roasts=[("r1", True, 1.0), ("r2", False, 0.0)],
    )
    _, pairs = sig.load_paired_outcomes(artifact)
    result = sig.mcnemar_exact(pairs)
    assert result.discordant_n == 0
    assert result.exact_p_two_sided == pytest.approx(1.0)


# --- Wilcoxon / sign test -------------------------------------------------


def test_wilcoxon_and_sign_test_on_tiny_synthetic_set(tmp_path: Path) -> None:
    """A small paired F1 set with 4 nonzero deltas, hand-checked ranks.

    Deltas (candidate - baseline): +0.2, +0.2, -0.1, 0.0 (tied, excluded).
    Nonzero abs values: 0.2, 0.2, 0.1 -> ranks 2.5, 2.5, 1 (ties averaged).
    W+ = 2.5 + 2.5 = 5.0, W- = 1.0, W = min(5.0, 1.0) = 1.0.
    """
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[
            ("r1", True, 0.5),
            ("r2", True, 0.5),
            ("r3", True, 0.5),
            ("r4", True, 0.5),
        ],
        candidate_roasts=[
            ("r1", True, 0.7),
            ("r2", True, 0.7),
            ("r3", True, 0.4),
            ("r4", True, 0.5),
        ],
    )
    _, pairs = sig.load_paired_outcomes(artifact)
    wilcoxon = sig.wilcoxon_signed_rank(pairs)
    assert wilcoxon.n_nonzero == 3
    assert wilcoxon.w_plus == pytest.approx(5.0)
    assert wilcoxon.w_minus == pytest.approx(1.0)
    assert wilcoxon.w_statistic == pytest.approx(1.0)
    assert wilcoxon.exact_p_two_sided is not None
    # Exact enumeration over 3 nonzero deltas: 8 sign assignments.
    assert 0.0 <= wilcoxon.exact_p_two_sided <= 1.0

    sign = sig.sign_test(pairs)
    assert sign.wins == 2
    assert sign.losses == 1
    assert sign.ties == 1
    # Exact two-sided binomial, k=min(2,1)=1, n=3:
    # 2 * (C(3,0) + C(3,1)) * 0.5**3 = 2 * 4/8 = 1.0 (capped at 1.0; with
    # only 3 discordant pairs, 2-vs-1 is not distinguishable from chance).
    assert sign.exact_p_two_sided == pytest.approx(1.0)


def test_wilcoxon_all_zero_deltas_is_degenerate(tmp_path: Path) -> None:
    """Identical F1 everywhere: no signal, reported as non-significant."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", True, 0.5), ("r2", True, 0.5)],
        candidate_roasts=[("r1", True, 0.5), ("r2", True, 0.5)],
    )
    _, pairs = sig.load_paired_outcomes(artifact)
    wilcoxon = sig.wilcoxon_signed_rank(pairs)
    assert wilcoxon.n_nonzero == 0
    assert wilcoxon.normal_approx_p_two_sided == pytest.approx(1.0)
    sign = sig.sign_test(pairs)
    assert sign.exact_p_two_sided == pytest.approx(1.0)


def test_wilcoxon_skips_exact_enumeration_above_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above MAX_EXACT_ENUMERATION_N nonzero deltas, exact_p_two_sided is None.

    Enumerating 2**n sign assignments is exponential; the cap keeps the
    reporting tool fast on a much larger future test set. Rather than
    generate 25+ synthetic roasts, lower the cap to exercise the skip
    branch directly.
    """
    monkeypatch.setattr(sig, "MAX_EXACT_ENUMERATION_N", 2)
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", True, 0.0), ("r2", True, 0.0), ("r3", True, 0.0)],
        candidate_roasts=[("r1", True, 1.0), ("r2", True, 1.0), ("r3", True, 1.0)],
    )
    _, pairs = sig.load_paired_outcomes(artifact)
    wilcoxon = sig.wilcoxon_signed_rank(pairs)
    assert wilcoxon.n_nonzero == 3
    assert wilcoxon.exact_p_two_sided is None


def test_print_report_omits_exact_line_when_enumeration_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human-readable report has no 'exact' Wilcoxon line when it is None."""
    monkeypatch.setattr(sig, "MAX_EXACT_ENUMERATION_N", 2)
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", True, 0.0), ("r2", True, 0.0), ("r3", True, 0.0)],
        candidate_roasts=[("r1", True, 1.0), ("r2", True, 1.0), ("r3", True, 1.0)],
    )
    rc = sig.main(["--artifact", str(artifact)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "normal-approx two-sided p" in out
    assert "exact (sign-permutation over tied ranks)" not in out


# --- Loading / error paths -------------------------------------------------


def test_load_paired_outcomes_missing_prompt_version_raises(tmp_path: Path) -> None:
    """A requested prompt_version absent from the artifact is an error."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", True, 1.0)],
        candidate_roasts=[("r1", True, 1.0)],
    )
    with pytest.raises(ValueError, match="no cell with prompt_version"):
        sig.load_paired_outcomes(artifact, baseline_prompt="v99")


def test_load_paired_outcomes_mismatched_roast_sets_raises(tmp_path: Path) -> None:
    """Baseline/candidate cells scored over different roast sets is an error."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", True, 1.0), ("r2", True, 1.0)],
        candidate_roasts=[("r1", True, 1.0), ("r3", True, 1.0)],
    )
    with pytest.raises(ValueError, match="roast sets differ"):
        sig.load_paired_outcomes(artifact)


def test_duplicate_roast_name_within_a_cell_raises(tmp_path: Path) -> None:
    """A duplicate roast_name in one cell's scores must not silently corrupt pairing.

    A naive ``{roast_name: outcome for ...}`` dict comprehension keeps only the
    LAST occurrence with no error and a normal-looking n_roasts count — this
    pins the fix (qa-found, #495 pre-open review).
    """
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps(
            {
                "mode": "replay",
                "test_set": "synthetic",
                "pinned_model": "fake/model",
                "prompt_versions": ["v2", "v4"],
                "cells": [
                    {
                        "slug": "fake/model",
                        "tier": "test",
                        "prompt_version": "v2",
                        "latency_risk": False,
                        "scores": [
                            {"roast_name": "r1", "drop": {"recall": 0.0, "f1": 0.0}},
                            {"roast_name": "r1", "drop": {"recall": 1.0, "f1": 1.0}},
                        ],
                    },
                    {
                        "slug": "fake/model",
                        "tier": "test",
                        "prompt_version": "v4",
                        "latency_risk": False,
                        "scores": [
                            {"roast_name": "r1", "drop": {"recall": 1.0, "f1": 1.0}},
                        ],
                    },
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="duplicate roast_name"):
        sig.load_paired_outcomes(path)


def test_duplicate_prompt_version_cell_raises(tmp_path: Path) -> None:
    """A duplicate prompt_version cell (e.g. a re-run-appended artifact) is an error.

    Returning the FIRST match would silently shadow a later duplicate cell —
    this pins the fix (qa-found, #495 pre-open review).
    """
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps(
            {
                "mode": "replay",
                "test_set": "synthetic",
                "pinned_model": "fake/model",
                "prompt_versions": ["v2", "v4"],
                "cells": [
                    {
                        "slug": "fake/model",
                        "tier": "test",
                        "prompt_version": "v2",
                        "latency_risk": False,
                        "scores": [{"roast_name": "r1", "drop": {"recall": 0.0, "f1": 0.0}}],
                    },
                    {
                        "slug": "fake/model",
                        "tier": "test",
                        "prompt_version": "v2",
                        "latency_risk": False,
                        "scores": [{"roast_name": "r1", "drop": {"recall": 1.0, "f1": 1.0}}],
                    },
                    {
                        "slug": "fake/model",
                        "tier": "test",
                        "prompt_version": "v4",
                        "latency_risk": False,
                        "scores": [{"roast_name": "r1", "drop": {"recall": 1.0, "f1": 1.0}}],
                    },
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="2 cells with prompt_version"):
        sig.load_paired_outcomes(path)


def test_empty_artifact_degrades_gracefully_to_p_one(tmp_path: Path) -> None:
    """Zero roasts in both cells: every test degrades to p=1.0, no crash.

    Pinned per qa's pre-open review (#495): confirms the zero-discordant /
    zero-nonzero-delta / zero-wins-and-losses degenerate paths all agree with
    "no evidence of a difference" rather than raising or dividing by zero.
    """
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[],
        candidate_roasts=[],
    )
    report = sig.build_report(artifact)
    assert report.provenance.n_roasts == 0
    assert report.mcnemar.discordant_n == 0
    assert report.mcnemar.exact_p_two_sided == pytest.approx(1.0)
    assert report.wilcoxon.n_nonzero == 0
    assert report.wilcoxon.normal_approx_p_two_sided == pytest.approx(1.0)
    assert report.wilcoxon.exact_p_two_sided == pytest.approx(1.0)
    assert report.sign_test.wins == 0
    assert report.sign_test.losses == 0
    assert report.sign_test.exact_p_two_sided == pytest.approx(1.0)
    assert report.mean_f1_delta == pytest.approx(0.0)


# --- Real committed artifact: reproduces the experiment.md claim ----------


@pytest.mark.skipif(not _REAL_ARTIFACT.exists(), reason="bake-off artifact not committed")
def test_reproduces_documented_mcnemar_p_value() -> None:
    """The committed Phase 3 artifact reproduces experiment.md's p=0.0039 claim."""
    report = sig.build_report(_REAL_ARTIFACT)
    assert report.provenance.n_roasts == 28
    assert report.provenance.pinned_model == "google/gemini-3.1-flash-lite"
    assert report.mcnemar.candidate_only == 9
    assert report.mcnemar.baseline_only == 0
    assert report.mcnemar.discordant_n == 9
    assert report.mcnemar.exact_p_two_sided == pytest.approx(0.0039, abs=0.0001)


@pytest.mark.skipif(not _REAL_ARTIFACT.exists(), reason="bake-off artifact not committed")
def test_reproduces_documented_f1_delta_and_sign_test() -> None:
    """The committed artifact reproduces the doc's 9/3/16 split, W=6, mean +0.23."""
    report = sig.build_report(_REAL_ARTIFACT)
    assert report.sign_test.wins == 9
    assert report.sign_test.losses == 3
    assert report.sign_test.ties == 16
    assert report.mean_f1_delta == pytest.approx(0.2263, abs=0.0005)
    assert report.wilcoxon.w_statistic == pytest.approx(6.0)
    # "borderline" per the doc: not significant at the conventional 0.05 level.
    assert report.sign_test.exact_p_two_sided == pytest.approx(0.146, abs=0.001)
    assert report.sign_test.exact_p_two_sided > 0.05
    # Wilcoxon "significant" per the doc: below the conventional 0.05 level.
    assert report.wilcoxon.normal_approx_p_two_sided < 0.05


# --- CLI --------------------------------------------------------------


def test_main_json_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI runs end-to-end against a synthetic artifact and emits JSON."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", False, 0.0), ("r2", True, 1.0)],
        candidate_roasts=[("r1", True, 1.0), ("r2", True, 1.0)],
    )
    rc = sig.main(["--artifact", str(artifact), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["n_roasts"] == 2
    assert payload["mcnemar"]["candidate_only"] == 1


def test_main_human_readable_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The default (non-JSON) report prints the contingency table and p-values."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", False, 0.0), ("r2", True, 1.0)],
        candidate_roasts=[("r1", True, 1.0), ("r2", True, 1.0)],
    )
    rc = sig.main(["--artifact", str(artifact)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "McNemar" in out
    assert "Wilcoxon" in out
    assert "Sign test" in out


def test_main_custom_prompt_versions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--baseline-prompt / --candidate-prompt select a different cell pair."""
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=[("r1", False, 0.0)],
        candidate_roasts=[("r1", True, 1.0)],
        baseline_version="c1",
        candidate_version="c3",
    )
    rc = sig.main(
        [
            "--artifact",
            str(artifact),
            "--baseline-prompt",
            "c1",
            "--candidate-prompt",
            "c3",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["baseline_prompt"] == "c1"
    assert payload["provenance"]["candidate_prompt"] == "c3"


def test_wilcoxon_normal_approx_matches_symmetric_case(tmp_path: Path) -> None:
    """A symmetric all-positive-delta set gives a known small p (z well past 2).

    With 6 concordant, equal-magnitude positive deltas, W- = 0 and the
    continuity-corrected z is large enough to push the two-sided normal-
    approx p-value comfortably under 0.05 — an independent sanity check on
    the hand-rolled normal CDF underlying :func:`wilcoxon_signed_rank`,
    without depending on scipy being installed.
    """
    candidate_rows = [(f"r{i}", True, 1.0) for i in range(6)]
    baseline_rows = [(f"r{i}", True, 0.0) for i in range(6)]
    artifact = _write_artifact(
        tmp_path / "artifact.json",
        baseline_roasts=baseline_rows,
        candidate_roasts=candidate_rows,
    )
    _, pairs = sig.load_paired_outcomes(artifact)
    result = sig.wilcoxon_signed_rank(pairs)
    assert result.n_nonzero == 6
    assert result.w_minus == pytest.approx(0.0)
    assert result.normal_approx_p_two_sided < 0.05
    assert result.exact_p_two_sided == pytest.approx(2 / 64)
