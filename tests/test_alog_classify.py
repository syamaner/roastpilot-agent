"""Tests for the offline ``.alog`` roast-degree classifier (#224).

Network-, key-, and private-data-free: the operator's raw ``.alog`` logs are
never committed, so these tests build synthetic Artisan profile dicts in memory
and assert the analysis logic — event-mark resolution and its guards, the
metric derivations (DTR, development time, RoR), the strict crash test that
reconciles against #229, the deterministic k=2 roast-degree split, and the
fixture matching by ``(drop_bt, dtr)``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import alog_classify as ac  # noqa: E402


def _profile(
    *,
    charge_idx: int,
    fc_idx: int,
    drop_idx: int,
    timex: list[float],
    bt: list[float],
    sc_idx: int = 0,
    uuid: str = "uuid-x",
) -> dict[str, Any]:
    """Build a minimal synthetic Artisan profile dict.

    Args:
        charge_idx: CHARGE index into ``timex`` (``-1`` for unset).
        fc_idx: FCs index (``0`` for unset).
        drop_idx: DROP index (``0`` for unset).
        timex: The timeline.
        bt: The bean-temperature series, parallel to ``timex``.
        sc_idx: SCs index (``0`` for unset).
        uuid: The roast UUID seed for the anonymised id.

    Returns:
        A profile dict shaped like a parsed ``.alog``.
    """
    timeindex = [charge_idx, 0, fc_idx, 0, sc_idx, 0, drop_idx, 0]
    return {
        "timeindex": timeindex,
        "timex": timex,
        "temp2": bt,
        "roastUUID": uuid,
        "roastisodate": "2025-01-01",
    }


def _ramp(n: int, start: float, step: float) -> list[float]:
    """A linear temperature ramp of ``n`` samples (1 s apart).

    Args:
        n: Sample count.
        start: First value.
        step: Per-sample increment.

    Returns:
        The ramp values.
    """
    return [start + i * step for i in range(n)]


def test_unset_charge_yields_no_metrics() -> None:
    """A CHARGE slot left at the ``-1`` sentinel produces no metrics."""
    timex = _ramp(760, 0.0, 1.0)
    bt = _ramp(760, 100.0, 0.12)
    prof = _profile(charge_idx=-1, fc_idx=600, drop_idx=750, timex=timex, bt=bt)
    assert ac.compute_metrics(prof, fallback_id="f") is None


def test_pre_charge_first_crack_is_rejected() -> None:
    """A first crack marked before charge is treated as unusable (no metrics)."""
    timex = _ramp(760, 0.0, 1.0)
    bt = _ramp(760, 100.0, 0.12)
    # FC index 2 (t=2) sits before charge index 100 (t=100).
    prof = _profile(charge_idx=100, fc_idx=2, drop_idx=750, timex=timex, bt=bt)
    assert ac.compute_metrics(prof, fallback_id="f") is None


def test_compute_metrics_basic_derivations() -> None:
    """DTR, development time, total time, and temp rise come out as expected."""
    # charge@t=0 bt=100, fc@t=600 bt=180, drop@t=750 bt=195
    timex = _ramp(760, 0.0, 1.0)
    bt = _ramp(760, 100.0, (195.0 - 100.0) / 759.0)
    bt[600] = 180.0
    bt[750] = 195.0
    prof = _profile(charge_idx=0, fc_idx=600, drop_idx=750, timex=timex, bt=bt)
    m = ac.compute_metrics(prof, fallback_id="f")
    assert m is not None
    assert m.fc_time_s == 600.0
    assert m.total_time_s == 750.0
    assert m.dev_time_s == 150.0
    assert m.dtr_percent == 20.0  # 150 / 750
    assert m.dev_temp_rise_c == 15.0  # 195 - 180


def test_compute_metrics_returns_none_without_fc_or_drop() -> None:
    """A log lacking first crack or drop yields no metrics."""
    timex = _ramp(10, 0.0, 1.0)
    bt = _ramp(10, 100.0, 1.0)
    no_fc = _profile(charge_idx=0, fc_idx=0, drop_idx=8, timex=timex, bt=bt)
    no_drop = _profile(charge_idx=0, fc_idx=4, drop_idx=0, timex=timex, bt=bt)
    assert ac.compute_metrics(no_fc, fallback_id="f") is None
    assert ac.compute_metrics(no_drop, fallback_id="f") is None


def test_crash_detector_flags_negative_ror_and_not_a_decline() -> None:
    """A genuine post-FC RoR dip below zero sets ``crash_negative``."""
    # Rising to FC, then BT falls after FC (a real crash), then recovers.
    timex = _ramp(700, 0.0, 1.0)
    bt = _ramp(700, 100.0, 0.13)
    fc = 600
    bt[fc] = 180.0
    # Drive BT down for 40 s after FC, then up to drop.
    for i in range(fc, fc + 40):
        bt[i] = 180.0 - (i - fc) * 0.5
    for i in range(fc + 40, 700):
        bt[i] = bt[fc + 39] + (i - (fc + 39)) * 0.2
    prof = _profile(charge_idx=0, fc_idx=fc, drop_idx=699, timex=timex, bt=bt)
    m = ac.compute_metrics(prof, fallback_id="f")
    assert m is not None
    assert m.crash_negative is True
    assert m.ror_min_within_crash_window < 0.0


def test_no_crash_when_ror_stays_positive() -> None:
    """A gently declining-but-positive RoR is a decline, not a crash."""
    timex = _ramp(760, 0.0, 1.0)
    # Steep ramp to FC (high RoR), gentler ramp after (lower but positive RoR).
    bt = _ramp(601, 100.0, 0.18)  # ~10.8 C/min to FC
    bt += [bt[-1] + (i + 1) * 0.12 for i in range(159)]  # ~7.2 C/min after
    prof = _profile(charge_idx=0, fc_idx=600, drop_idx=759, timex=timex, bt=bt)
    m = ac.compute_metrics(prof, fallback_id="f")
    assert m is not None
    assert m.crash_negative is False
    assert m.ror_min_within_crash_window > 0.0
    assert m.ror_declining is True


def test_classify_degrees_second_crack_is_dark() -> None:
    """Reaching second crack forces a ``dark`` label regardless of clustering."""
    timex = _ramp(760, 0.0, 1.0)
    bt = _ramp(760, 100.0, 0.12)
    bt[600] = 180.0
    bt[750] = 195.0
    prof = _profile(
        charge_idx=0, fc_idx=600, drop_idx=750, sc_idx=720, timex=timex, bt=bt, uuid="sc"
    )
    m = ac.compute_metrics(prof, fallback_id="f")
    assert m is not None
    assert m.sc_reached is True
    labels = ac.classify_degrees([m])
    assert labels[m.anon_id] == "dark"


def test_classify_degrees_over_done_promotion() -> None:
    """A drop past the over-done proxy (>200 C) is promoted to over-dark."""
    metrics: list[ac.RoastMetrics] = []
    # Two cool mediums and one very hot roast.
    specs = [(190.0, "a"), (191.0, "b"), (202.0, "c")]
    for drop_bt, uid in specs:
        timex = _ramp(760, 0.0, 1.0)
        bt = _ramp(601, 100.0, (175.0 - 100.0) / 600.0)
        bt += [175.0 + (i + 1) * ((drop_bt - 175.0) / 159.0) for i in range(159)]
        prof = _profile(charge_idx=0, fc_idx=600, drop_idx=759, timex=timex, bt=bt, uuid=uid)
        m = ac.compute_metrics(prof, fallback_id=uid)
        assert m is not None
        metrics.append(m)
    labels = ac.classify_degrees(metrics)
    hot = next(m for m in metrics if m.drop_bt > 200.0)
    assert labels[hot.anon_id] == "over-dark"


def test_kmeans_two_separates_clusters_deterministically() -> None:
    """k=2 split is reproducible and separates two obvious groups."""
    points = [(-2.0,), (-1.9,), (-2.1,), (2.0,), (1.9,), (2.1,)]
    first = ac.kmeans_two(points)
    second = ac.kmeans_two(points)
    assert first == second
    assert first[0] == first[1] == first[2]
    assert first[3] == first[4] == first[5]
    assert first[0] != first[3]


def test_match_fixtures_by_drop_and_dtr(tmp_path: Path) -> None:
    """A roast matches a manifest fixture on its ``(drop_bt, dtr)`` key."""
    timex = _ramp(760, 0.0, 1.0)
    bt = _ramp(760, 100.0, 0.12)
    bt[600] = 180.0
    bt[750] = 195.0
    prof = _profile(charge_idx=0, fc_idx=600, drop_idx=750, timex=timex, bt=bt)
    m = ac.compute_metrics(prof, fallback_id="f")
    assert m is not None
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"selected": [{"label": "artisan-99", "drop_temp_c": '
        + f"{m.drop_bt}"
        + ', "development_time_ratio_percent": '
        + f"{m.dtr_percent}"
        + "}]}",
        encoding="utf-8",
    )
    matched = ac.match_fixtures([m], manifest)
    assert matched[m.anon_id] == "artisan-99"


def test_infer_origin_specific_tokens_win_over_australia() -> None:
    """``au-nica`` resolves to Nicaragua, not the ambiguous Australia rule."""
    assert ac.infer_origin("24-09-07_1326-au-nica.alog") == "Nicaragua"
    assert ac.infer_origin("24-06-23_1237-australia-1.alog") == "Australia (AMBIGUOUS)"


def test_infer_origin_processing_variants() -> None:
    """Fermented Brazil is distinguished from plain Brazil; spellings normalise."""
    assert ac.infer_origin("brasil-ferm-b2-24-11-09_1656.alog") == "Brazil (fermented/anaerobic)"
    assert ac.infer_origin("brasil-fm-1-24-11-09_1610.alog") == "Brazil (fermented/anaerobic)"
    assert ac.infer_origin("25-10-19_1126-brazil1.alog") == "Brazil"
    assert ac.infer_origin("jamaice_b1_24-12-06_1855.alog") == "Jamaica (Blue Mountain)"
    assert ac.infer_origin("tw_nantouu-24-10-20_1503.alog") == "Taiwan (Nantou/Alishan)"


def test_infer_origin_bare_batch_files_map_to_cuba_session() -> None:
    """Bare ``b1``/``b3`` files dated to the Jul-2025 Cuba session resolve to Cuba."""
    assert ac.infer_origin("25-07-16_1854_b1.alog") == "Cuba"
    assert ac.infer_origin("25-07-16_2009-b3.alog") == "Cuba"
    # A bare batch file from a different date stays Unknown.
    assert ac.infer_origin("99-01-01_0000_b1.alog") == "Unknown"


def test_filename_says_dark() -> None:
    """The explicit ``_dark`` ground-truth token is detected."""
    assert ac.filename_says_dark("kona_3_dark24-12-06_2016.alog") is True
    assert ac.filename_says_dark("kona-24-11-12_1943.alog") is False
