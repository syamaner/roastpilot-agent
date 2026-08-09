"""Anonymised fixture registry for RP-B's (#709) offline ambient-doctrine eval.

D3 of the RP-D PR plan (plan repo D124) called for "register both Conebosque
fixtures". Investigation (#711 follow-up, issue discussion 8-9 Aug 2026) found:

- The RP-D corpus scorer (``scripts/rpd_corpus_score.py``) reads the operator's
  real SQLite store directly and needs no fixture step at all — the two
  Conebosque roasts (#559) already score there with zero registration.
- The actual consumer is RP-B's own offline decision-level comparison (D124,
  refined by D127: the baseline arm is **c10**, never c3, so the comparison
  isolates the ambient section from every teaching c11 inherits) — a
  c10-vs-c11 fan-direction comparison over a small, fixed set of replayed
  roasts. That comparison script does not exist yet (tracked separately); this
  module is its eval-set INPUT, built ahead of it so the set is fixed and
  reviewed independently of the harness that will consume it.
- The two Conebosque roasts alone (23.05 / 23.54 °C, both well below the
  doctrine's default 26.0 °C threshold) cannot exercise the at-or-above branch
  or the doctrine's central CONDITIONAL claim — the operator (D129) ratified a
  four-point spread instead, chosen to straddle the boundary tightly enough to
  test the switch itself while still anchoring both extremes.

**Mirrors ``scripts/advisor_bakeoff.py``'s ``.artisan-fixtures`` pattern
exactly** (``ARTISAN_FIXTURES_DIR`` / ``FULL_MEDIUM_FIXTURE_NAMES`` /
``fixture_path_for`` / ``resolve_test_set``), because that pattern already
solves the exact problem this module has: real roast telemetry is the
operator's personal data (AGENTS.md — never committed), while the ANONYMISED
NAMES that select which roasts form an eval set are inert metadata safe to
commit and review. So the fixture data lives in a local, gitignored directory
(:data:`AMBIENT_FIXTURES_DIR`, see ``.gitignore``) regenerated on demand via
``scripts/store_to_fixture.py``; only the names below, carrying nothing but
each roast's position relative to the doctrine's threshold, are committed.

**Deliberately standalone and stdlib-only** (unlike ``advisor_bakeoff.py``,
which pulls in the full pydantic-ai / OpenRouter provider stack): the eval-set
registration is a separate concern from the LLM-calling harness that will
consume it, and keeping this import-light lets it be tested in isolation and
imported by whichever script ends up owning the c10-vs-c11 comparison without
dragging in network-provider dependencies just to resolve a path.

**Committed content carries ambient position ONLY** (team-lead ruling, 9 Aug
2026, on the operator's behalf): no bean supplier, no run id, no roast date,
no drop temperature, no development-time ratio, no RP-D HIT/MISS status.
Those are either the operator's personal roast data or already public
elsewhere in the plan/registry, and the eval gains nothing from a second copy
living in a fixture list that would then need redacting later — every extra
field committed here is one more thing an anonymisation review has to check.
The real name-to-run-id mapping (personal data) lives in a GITIGNORED local
file the operator uses to regenerate fixtures; see
:data:`AMBIENT_FIXTURES_DIR`'s docstring reference below — this module never
writes or reads that mapping itself, and never calls ``store_to_fixture.py``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Local-only (gitignored — see ``.gitignore``) directory holding the
#: regenerated ``roast.jsonl`` / ``summary.json`` pairs for the RP-B
#: ambient-doctrine eval set, plus the operator's own gitignored
#: ``MAPPING.local.md`` (name -> real run id + the exact
#: ``scripts/store_to_fixture.py`` command that produced it). Mirrors
#: ``advisor_bakeoff.ARTISAN_FIXTURES_DIR`` exactly: never committed, never
#: written by this module or any agent — regeneration is the operator's own
#: local action against his own SQLite store.
AMBIENT_FIXTURES_DIR = REPO_ROOT / ".rp-b-fixtures"

#: D129 (operator, 9 Aug 2026): the ratified four-point ambient spread for the
#: c10-vs-c11 offline decision-level comparison (D124, refined by D127 — the
#: baseline arm is c10, never c3, so the comparison isolates the ambient
#: section from every teaching c11 inherits, per the D122 one-variable-at-a-
#: time ruling). Each name carries ONLY its ambient position relative to the
#: doctrine's default 26.0 C threshold
#: (``ControllerConfig.ambient_fan_doctrine.threshold_c``) — see the module
#: docstring for what is deliberately NOT encoded here.
#:
#: ``eval-02`` / ``eval-06`` sit below the threshold (well below / just
#: below); ``eval-07`` / ``eval-10`` sit at-or-above (just above / well
#: above). ``eval-06`` and ``eval-07`` straddle the boundary tightly (under
#: 1 C apart) so that pair alone can test the doctrine's CONDITIONAL switch;
#: ``eval-02`` and ``eval-10`` anchor the two extremes so a roast merely just
#: above the boundary is never mistaken for evidence that the at-or-above
#: branch genuinely sanctions aggressive fan (the #498 direction the doctrine
#: exists to protect).
AMBIENT_EVAL_FIXTURE_NAMES: tuple[str, ...] = (
    "eval-02",  # well below the threshold
    "eval-06",  # just below the threshold
    "eval-07",  # just above the threshold
    "eval-10",  # well above the threshold
)


def fixture_path_for(name: str) -> Path:
    """Return the ``roast.jsonl`` path for an ambient-eval fixture dir name.

    Args:
        name: One of :data:`AMBIENT_EVAL_FIXTURE_NAMES`.

    Returns:
        The (possibly absent) local ``roast.jsonl`` path.
    """
    return AMBIENT_FIXTURES_DIR / name / "roast.jsonl"


def resolve_ambient_eval_set(
    names: tuple[str, ...] = AMBIENT_EVAL_FIXTURE_NAMES,
) -> tuple[Path, ...]:
    """Resolve the RP-B ambient-eval fixture names to local ``roast.jsonl`` paths.

    Mirrors ``advisor_bakeoff.resolve_test_set`` exactly: the fixture DATA is
    local-only (gitignored, regenerated from the operator's own store via
    ``scripts/store_to_fixture.py`` — see the operator's own
    ``AMBIENT_FIXTURES_DIR / "MAPPING.local.md"``, which this module never
    reads or writes), so a run on a checkout without it fails loudly here —
    listing every missing fixture by name — rather than silently scoring a
    partial set.

    Args:
        names: The fixture dir names to resolve, defaulting to the full D129
            ratified set.

    Returns:
        The resolved ``roast.jsonl`` paths, in ``names`` order.

    Raises:
        FileNotFoundError: If any named fixture's ``roast.jsonl`` is absent.
    """
    paths = tuple(fixture_path_for(name) for name in names)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "missing local-only RP-B ambient-eval fixtures (gitignored — "
            "regenerate with scripts/store_to_fixture.py per "
            f"{AMBIENT_FIXTURES_DIR / 'MAPPING.local.md'}): " + ", ".join(missing)
        )
    return paths
