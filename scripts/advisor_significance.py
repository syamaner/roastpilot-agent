"""Reproduce the significance tests behind the D34 prompt-v4 drop-recall claim.

``docs/advisor/experiment.md`` (Phase 3) reports, for the pinned model
(``google/gemini-3.1-flash-lite``) on the 28-roast "good" Artisan set:

- an exact McNemar test on the drop-recall calls, p = 0.0039;
- a per-roast drop-F1 delta of "better on 9, worse on 3, tied on 16" roasts
  (mean delta +0.23), with a Wilcoxon signed-rank test reported as
  significant (W = 6) and a sign test reported as borderline (p = 0.15).

Those numbers were computed in a working session and never checked into a
script, so they were not reproducible from the repo (issue #495). This
script closes that gap: it reads the **committed** bake-off artifact the
claim is derived from
(``docs/advisor/bakeoff-results-prompts-2026-06-14.json``), rebuilds the
paired per-roast (v2, v4) drop outcomes for the pinned model, and recomputes
all three tests from scratch — no network access, no LLM calls. The McNemar
p, the F1-delta split/mean, the Wilcoxon W statistic, and the sign-test p all
**reproduce** the session's reported figures exactly. The Wilcoxon
**p-value** this script prints is **new**: the original session reported
only "significant (W = 6)", never a specific p, so the normal-approximation
p this script computes is a first-time figure, not a reproduction of a prior
number — ``docs/advisor/experiment.md`` marks it as such.

Statistical notes (no scipy dependency; see the repo's precedent in
``scripts/curve_feature_eval.py``, which hand-rolls its own significance
approximation rather than adding scipy for one script):

- **McNemar** is computed via its exact binomial form
  (``2 * sum_{k=0}^{min(b,c)} C(n,k) * 0.5^n`` for ``n = b + c`` discordant
  pairs), which is exact for any ``n`` and is preferred here over the
  chi-square approximation, which is unreliable at this sample size
  (``b + c = 9``).
- **Wilcoxon signed-rank** uses average ranks for ties, then reports two
  p-values: the classic **normal approximation with continuity correction**
  (what any package reports once ties are present — e.g. SciPy's default
  ``mode="auto"`` falls back to this, and R's ``wilcox.test`` warns and does
  the same), and, as a secondary check, an **exact permutation p-value**
  from full enumeration of the ``2**n_nonzero`` sign assignments over the
  same (tied) rank vector. The two are close but not identical because
  "exact" is definition-dependent once ties exist; both are printed so the
  reader can see the gap rather than a single silently-chosen number.
- **Sign test** is the exact two-sided binomial test on (wins, losses),
  ties excluded — matching the classic sign-test definition.

Run::

    python scripts/advisor_significance.py
    python scripts/advisor_significance.py --json

Exit code is always 0; this is a reporting tool, not a gate. If the
committed artifact ever changes shape or the discordant-pair counts stop
matching the documented 9/0 split, the printed numbers will simply differ
from the doc's claim — compare the output to ``experiment.md`` by eye (or
diff the ``--json`` output against a saved run) rather than relying on this
script to fail loudly.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any

# Resolved from this file so the script is CWD-independent (mirrors
# scripts/check_contract_drift.py's convention).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ARTIFACT = _REPO_ROOT / "docs" / "advisor" / "bakeoff-results-prompts-2026-06-14.json"

#: The two prompt versions the documented claim compares (Phase 3, D34).
BASELINE_PROMPT = "v2"
CANDIDATE_PROMPT = "v4"

#: Enumerating every sign assignment is exact but exponential; above this many
#: nonzero (discordant) pairs, skip the exact permutation cross-check rather
#: than hang (28 roasts means at most 28 nonzero diffs; well under the cap in
#: any realistic bake-off set, but guarded in case a future rerun grows the
#: test set substantially).
MAX_EXACT_ENUMERATION_N = 24


# --- Loading ------------------------------------------------------------


@dataclass(frozen=True)
class RoastDropOutcome:
    """One roast's drop-call outcome for one prompt version.

    Attributes:
        roast_name: The fixture identifier (``scores[i].roast_name`` in the
            artifact).
        called_drop: Whether this prompt version called the drop at least
            once on this roast (``drop.recall >= 1.0``, equivalently
            ``drop_confusion.true_positives >= 1``) — the discrete outcome
            McNemar compares.
        f1: The roast's drop F1 score for this prompt version — the
            continuous outcome the Wilcoxon / sign tests compare.
    """

    roast_name: str
    called_drop: bool
    f1: float


@dataclass(frozen=True)
class Provenance:
    """Where the paired data came from, for the printed/JSON report.

    Attributes:
        artifact_path: The bake-off JSON file read.
        mode: The artifact's ``mode`` field (e.g. ``"replay"``).
        test_set: The artifact's ``test_set`` field.
        pinned_model: The model slug the paired roasts were scored under.
        baseline_prompt: The baseline prompt version (v2).
        candidate_prompt: The candidate prompt version (v4).
        n_roasts: Paired roast count.
    """

    artifact_path: str
    mode: str
    test_set: str
    pinned_model: str
    baseline_prompt: str
    candidate_prompt: str
    n_roasts: int


def _cell_scores(artifact: dict[str, Any], prompt_version: str) -> dict[str, RoastDropOutcome]:
    """Extract per-roast drop outcomes for one prompt-version cell.

    Args:
        artifact: The parsed bake-off JSON.
        prompt_version: The ``cells[i].prompt_version`` to select.

    Returns:
        A ``roast_name -> RoastDropOutcome`` map.

    Raises:
        ValueError: If no cell matches ``prompt_version``, more than one cell
            matches it (an ambiguous, e.g. re-run-appended artifact), or the
            matched cell has a duplicate ``roast_name`` in its ``scores``
            (silently keeping only the last occurrence would corrupt the
            pairing without any error).
    """
    matches = [cell for cell in artifact["cells"] if cell["prompt_version"] == prompt_version]
    if not matches:
        raise ValueError(f"no cell with prompt_version={prompt_version!r} in artifact")
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} cells with prompt_version={prompt_version!r} in artifact "
            "(ambiguous — expected exactly one; a re-run-appended artifact?)"
        )
    scores = matches[0]["scores"]
    names = [s["roast_name"] for s in scores]
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(
            f"prompt_version={prompt_version!r} has duplicate roast_name entries in "
            f"scores: {duplicates} (would silently corrupt pairing by roast name)"
        )
    return {
        s["roast_name"]: RoastDropOutcome(
            roast_name=s["roast_name"],
            called_drop=s["drop"]["recall"] >= 1.0,
            f1=float(s["drop"]["f1"]),
        )
        for s in scores
    }


def load_paired_outcomes(
    artifact_path: Path,
    baseline_prompt: str = BASELINE_PROMPT,
    candidate_prompt: str = CANDIDATE_PROMPT,
) -> tuple[Provenance, list[tuple[RoastDropOutcome, RoastDropOutcome]]]:
    """Load and pair per-roast drop outcomes for two prompt versions.

    Args:
        artifact_path: Path to a ``bakeoff_replay.py``-shaped JSON artifact
            (``cells[i].scores[j]`` per (prompt_version, roast)).
        baseline_prompt: The baseline prompt version's cell to read.
        candidate_prompt: The candidate prompt version's cell to read.

    Returns:
        The run provenance, and a list of ``(baseline, candidate)`` pairs
        ordered by roast name, one per roast common to both cells.

    Raises:
        ValueError: If either prompt version is missing from the artifact,
            or the two cells do not cover the same roast set.
    """
    artifact = json.loads(artifact_path.read_text())
    baseline_map = _cell_scores(artifact, baseline_prompt)
    candidate_map = _cell_scores(artifact, candidate_prompt)
    if baseline_map.keys() != candidate_map.keys():
        only_baseline = sorted(baseline_map.keys() - candidate_map.keys())
        only_candidate = sorted(candidate_map.keys() - baseline_map.keys())
        raise ValueError(
            f"roast sets differ between {baseline_prompt!r} and {candidate_prompt!r}: "
            f"only in {baseline_prompt!r}={only_baseline}, "
            f"only in {candidate_prompt!r}={only_candidate}"
        )
    names = sorted(baseline_map.keys())
    pairs = [(baseline_map[n], candidate_map[n]) for n in names]
    provenance = Provenance(
        artifact_path=str(artifact_path),
        mode=str(artifact.get("mode", "")),
        test_set=str(artifact.get("test_set", "")),
        pinned_model=str(artifact.get("pinned_model", "")),
        baseline_prompt=baseline_prompt,
        candidate_prompt=candidate_prompt,
        n_roasts=len(pairs),
    )
    return provenance, pairs


# --- McNemar (exact, binomial form) --------------------------------------


@dataclass(frozen=True)
class McNemarResult:
    """Exact McNemar test on a discrete paired (called-drop) outcome.

    Attributes:
        both_yes: Roasts where both prompt versions called the drop.
        both_no: Roasts where neither prompt version called the drop.
        baseline_only: Roasts the baseline caught and the candidate missed
            (the "regression" cell).
        candidate_only: Roasts the candidate caught and the baseline missed
            (the "improvement" cell).
        discordant_n: ``baseline_only + candidate_only`` — the only pairs
            McNemar uses.
        exact_p_two_sided: The exact two-sided McNemar p-value.
    """

    both_yes: int
    both_no: int
    baseline_only: int
    candidate_only: int
    discordant_n: int
    exact_p_two_sided: float


def _binomial_coefficient(n: int, k: int) -> int:
    """Exact integer ``n choose k`` (``math.comb`` wrapper for readability)."""
    return math.comb(n, k)


def mcnemar_exact(pairs: list[tuple[RoastDropOutcome, RoastDropOutcome]]) -> McNemarResult:
    """Exact (binomial-form) McNemar test on the ``called_drop`` outcome.

    Builds the 2x2 contingency table over paired (baseline, candidate)
    boolean outcomes, then computes the exact two-sided p-value as
    ``2 * sum_{k=0}^{min(b,c)} C(n,k) * 0.5**n`` for ``n = b + c``
    discordant pairs (Fisher's exact form of McNemar's test — equivalent to
    a two-sided exact binomial test of the discordant count against p=0.5).
    This is exact at any sample size, unlike the common chi-square
    approximation, which is unreliable when ``n`` is small (as it is here).

    Args:
        pairs: Paired per-roast outcomes, ``(baseline, candidate)``.

    Returns:
        The :class:`McNemarResult`.
    """
    both_yes = both_no = baseline_only = candidate_only = 0
    for baseline, candidate in pairs:
        if baseline.called_drop and candidate.called_drop:
            both_yes += 1
        elif not baseline.called_drop and not candidate.called_drop:
            both_no += 1
        elif baseline.called_drop and not candidate.called_drop:
            baseline_only += 1
        else:
            candidate_only += 1

    b, c = baseline_only, candidate_only
    n = b + c
    if n == 0:
        # No discordant pairs at all: the two prompts never disagree, so
        # there is nothing for McNemar to test — report p=1.0 (no evidence
        # of a difference) rather than dividing by zero.
        exact_p = 1.0
    else:
        k = min(b, c)
        tail = sum(_binomial_coefficient(n, i) for i in range(k + 1))
        exact_p = min(1.0, 2.0 * tail * (0.5**n))

    return McNemarResult(
        both_yes=both_yes,
        both_no=both_no,
        baseline_only=baseline_only,
        candidate_only=candidate_only,
        discordant_n=n,
        exact_p_two_sided=exact_p,
    )


# --- Wilcoxon signed-rank (paired F1 deltas) -----------------------------


@dataclass(frozen=True)
class WilcoxonResult:
    """Wilcoxon signed-rank test on the paired F1 deltas.

    Attributes:
        n_nonzero: Roasts with a nonzero F1 delta (zeros are dropped, the
            conventional ``zero_method="wilcox"`` handling).
        w_statistic: ``min(W+, W-)`` — the conventional reported statistic.
        w_plus: Sum of ranks where the candidate beat the baseline.
        w_minus: Sum of ranks where the baseline beat the candidate.
        normal_approx_p_two_sided: Two-sided p-value from the normal
            approximation with a continuity correction and a tie-corrected
            variance (the value any standard package reports once ties are
            present).
        exact_p_two_sided: Two-sided p-value from full enumeration of the
            ``2**n_nonzero`` sign assignments over the same (tied) rank
            vector, or ``None`` if ``n_nonzero`` exceeds
            :data:`MAX_EXACT_ENUMERATION_N`. Included as a cross-check; not
            identical to the normal-approximation figure because "exact"
            with ties is definition-dependent (see module docstring).
    """

    n_nonzero: int
    w_statistic: float
    w_plus: float
    w_minus: float
    normal_approx_p_two_sided: float
    exact_p_two_sided: float | None


def _average_ranks(abs_values: list[float]) -> list[float]:
    """Rank ``abs_values`` ascending, 1-based, averaging ranks within ties."""
    n = len(abs_values)
    order = sorted(range(n), key=lambda i: abs_values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_values[order[j + 1]] == abs_values[order[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for pos in range(i, j + 1):
            ranks[order[pos]] = avg_rank
        i = j + 1
    return ranks


def wilcoxon_signed_rank(pairs: list[tuple[RoastDropOutcome, RoastDropOutcome]]) -> WilcoxonResult:
    """Wilcoxon signed-rank test on paired (candidate - baseline) F1 deltas.

    Args:
        pairs: Paired per-roast outcomes, ``(baseline, candidate)``.

    Returns:
        The :class:`WilcoxonResult`.
    """
    diffs = [candidate.f1 - baseline.f1 for baseline, candidate in pairs]
    nonzero = [d for d in diffs if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return WilcoxonResult(0, 0.0, 0.0, 0.0, 1.0, 1.0)

    abs_diffs = [abs(d) for d in nonzero]
    ranks = _average_ranks(abs_diffs)
    w_plus = sum(r for r, d in zip(ranks, nonzero, strict=True) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero, strict=True) if d < 0)
    w_stat = min(w_plus, w_minus)

    # Normal approximation with a continuity correction and the standard
    # tie-correction to the variance (Cureton 1967 / the textbook formula
    # used when the exact null distribution assumes no ties).
    mean_w = n * (n + 1) / 4.0
    tie_groups: dict[float, int] = {}
    for v in abs_diffs:
        tie_groups[v] = tie_groups.get(v, 0) + 1
    tie_correction = sum(t**3 - t for t in tie_groups.values())
    var_w = n * (n + 1) * (2 * n + 1) / 24.0 - tie_correction / 48.0
    if var_w <= 0:  # pragma: no cover - the tie-corrected variance is provably
        # non-negative for any partition of n>=1 nonzero deltas into tie groups
        # (even the maximal single-group case leaves var_w > 0); this guard is
        # defensive against a future formula change, not a reachable branch.
        normal_p = 1.0
    else:
        z_numerator = w_plus - mean_w
        z = (z_numerator - math.copysign(0.5, z_numerator)) / math.sqrt(var_w)
        normal_p = 2.0 * (1.0 - _standard_normal_cdf(abs(z)))
        normal_p = max(0.0, min(1.0, normal_p))

    exact_p: float | None = None
    if n <= MAX_EXACT_ENUMERATION_N:
        rank_total = w_plus + w_minus
        # Two-sided extremity is symmetric around rank_total / 2: a permuted
        # W+ is "at least as extreme" as the observed statistic when it falls
        # at or beyond w_stat in either tail (w_stat is the smaller of
        # W+/W-, so the upper tail boundary is rank_total - w_stat).
        enumerated = 0
        at_least_as_extreme = 0
        for signs in product((1, -1), repeat=n):
            enumerated += 1
            candidate_w_plus = sum(r for r, s in zip(ranks, signs, strict=True) if s > 0)
            if candidate_w_plus <= w_stat or candidate_w_plus >= rank_total - w_stat:
                at_least_as_extreme += 1
        exact_p = min(1.0, at_least_as_extreme / enumerated)

    return WilcoxonResult(
        n_nonzero=n,
        w_statistic=w_stat,
        w_plus=w_plus,
        w_minus=w_minus,
        normal_approx_p_two_sided=normal_p,
        exact_p_two_sided=exact_p,
    )


def _standard_normal_cdf(z: float) -> float:
    """Standard normal CDF via the error function (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --- Sign test ------------------------------------------------------------


@dataclass(frozen=True)
class SignTestResult:
    """Exact two-sided sign test on the paired F1 deltas.

    Attributes:
        wins: Roasts where the candidate's F1 beat the baseline's.
        losses: Roasts where the baseline's F1 beat the candidate's.
        ties: Roasts with equal F1 (excluded from the test).
        exact_p_two_sided: Two-sided exact binomial p-value of
            ``wins`` vs ``wins + losses`` at ``p=0.5``.
    """

    wins: int
    losses: int
    ties: int
    exact_p_two_sided: float


def sign_test(pairs: list[tuple[RoastDropOutcome, RoastDropOutcome]]) -> SignTestResult:
    """Exact two-sided sign test on paired (candidate vs baseline) F1.

    Args:
        pairs: Paired per-roast outcomes, ``(baseline, candidate)``.

    Returns:
        The :class:`SignTestResult`.
    """
    wins = sum(1 for baseline, candidate in pairs if candidate.f1 > baseline.f1)
    losses = sum(1 for baseline, candidate in pairs if candidate.f1 < baseline.f1)
    ties = len(pairs) - wins - losses
    n = wins + losses
    if n == 0:
        return SignTestResult(wins, losses, ties, 1.0)
    k = min(wins, losses)
    tail = sum(_binomial_coefficient(n, i) for i in range(k + 1))
    p = min(1.0, 2.0 * tail * (0.5**n))
    return SignTestResult(wins=wins, losses=losses, ties=ties, exact_p_two_sided=p)


# --- Report -----------------------------------------------------------------


@dataclass(frozen=True)
class SignificanceReport:
    """The full significance report for one (baseline, candidate) comparison.

    Attributes:
        provenance: Where the paired data came from.
        mcnemar: The exact McNemar result on ``called_drop``.
        wilcoxon: The Wilcoxon signed-rank result on F1 deltas.
        sign_test: The exact sign test result on F1 deltas.
        mean_f1_delta: Mean of (candidate F1 - baseline F1) across all roasts.
    """

    provenance: Provenance
    mcnemar: McNemarResult
    wilcoxon: WilcoxonResult
    sign_test: SignTestResult
    mean_f1_delta: float


def build_report(
    artifact_path: Path,
    baseline_prompt: str = BASELINE_PROMPT,
    candidate_prompt: str = CANDIDATE_PROMPT,
) -> SignificanceReport:
    """Load the artifact and compute all three significance tests.

    Args:
        artifact_path: Path to the bake-off JSON artifact.
        baseline_prompt: The baseline prompt version.
        candidate_prompt: The candidate prompt version.

    Returns:
        The assembled :class:`SignificanceReport`.
    """
    provenance, pairs = load_paired_outcomes(artifact_path, baseline_prompt, candidate_prompt)
    diffs = [candidate.f1 - baseline.f1 for baseline, candidate in pairs]
    mean_delta = sum(diffs) / len(diffs) if diffs else 0.0
    return SignificanceReport(
        provenance=provenance,
        mcnemar=mcnemar_exact(pairs),
        wilcoxon=wilcoxon_signed_rank(pairs),
        sign_test=sign_test(pairs),
        mean_f1_delta=mean_delta,
    )


def _print_report(report: SignificanceReport) -> None:
    """Print a human-readable report to stdout."""
    p = report.provenance
    print(f"Source: {p.artifact_path}")
    print(f"  mode={p.mode} test_set={p.test_set} pinned_model={p.pinned_model}")
    print(f"  comparison: {p.candidate_prompt} (candidate) vs {p.baseline_prompt} (baseline)")
    print(f"  n roasts: {p.n_roasts}")
    print()

    m = report.mcnemar
    print("== McNemar (exact, binomial form) on drop-recall calls ==")
    print("  contingency table (rows=baseline, cols=candidate):")
    print(f"    both called drop:            {m.both_yes}")
    print(f"    neither called drop:         {m.both_no}")
    print(f"    baseline only (candidate missed): {m.baseline_only}")
    print(f"    candidate only (baseline missed): {m.candidate_only}")
    print(f"  discordant pairs (n = b + c): {m.discordant_n}")
    print(f"  exact two-sided p-value:      {m.exact_p_two_sided:.4f}")
    print()

    w = report.wilcoxon
    print("== Wilcoxon signed-rank on per-roast drop F1 deltas ==")
    print(f"  nonzero deltas: {w.n_nonzero}")
    print(f"  W+ = {w.w_plus:.1f}  W- = {w.w_minus:.1f}  W (reported) = {w.w_statistic:.1f}")
    print(
        "  normal-approx two-sided p (continuity + tie correction): "
        f"{w.normal_approx_p_two_sided:.4f}"
    )
    if w.exact_p_two_sided is not None:
        print(
            f"  exact (sign-permutation over tied ranks) two-sided p:    {w.exact_p_two_sided:.4f}"
        )
    print()

    s = report.sign_test
    print("== Sign test on per-roast drop F1 (candidate vs baseline) ==")
    print(f"  wins={s.wins} losses={s.losses} ties={s.ties}")
    print(f"  exact two-sided p-value: {s.exact_p_two_sided:.4f}")
    print()
    print(f"Mean F1 delta (candidate - baseline): {report.mean_f1_delta:+.4f}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: compute and print the significance report.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code (always ``0`` — this is a reporting tool).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        type=Path,
        default=_DEFAULT_ARTIFACT,
        help="Bake-off JSON artifact to read (default: the Phase 3 prompt-sweep result).",
    )
    parser.add_argument(
        "--baseline-prompt",
        default=BASELINE_PROMPT,
        help=f"Baseline prompt_version cell to compare from (default: {BASELINE_PROMPT}).",
    )
    parser.add_argument(
        "--candidate-prompt",
        default=CANDIDATE_PROMPT,
        help=f"Candidate prompt_version cell to compare to (default: {CANDIDATE_PROMPT}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the human-readable summary.",
    )
    args = parser.parse_args(argv)

    report = build_report(args.artifact, args.baseline_prompt, args.candidate_prompt)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    raise SystemExit(main())
