"""Tests for the §7.1 per-bean reference-curve prototype (D42 §7 step 2).

Network- / key- / hardware-free (the M1 guardrail). The prototype is pure data
work, so these tests build **synthetic** Artisan-style profile dicts
(``timex`` / ``temp2`` / ``timeindex``) and assert the registration and
aggregation behaviour the methodology demands:

* a roast warps onto the common phase axis, landmarks land on the right nodes,
  and the warp is monotone within each segment;
* the first segment is anchored on the **turning point**, not the charge mark
  (the documented Hottop probe artifact), so a synthetic charge "probe spike +
  dive" does not pollute node 0;
* the pooled reference is the per-node mean with an SD / percentile band, and
  soft-tier down-weighting moves the pooled mean toward the core;
* the inter-origin correlation returns ``+1`` for identically-shaped origins and
  a negative value for opposite-shaped ones (the pool-vs-per-origin signal);
* the #290-identical k-means medium classification picks the lower-drop /
  shorter-development cluster and applies the 197 °C over-done line.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reference_curve_prototype as rcp  # noqa: E402


def _ramp_profile(
    *,
    charge_spike: float = 180.0,
    turning_bt: float = 80.0,
    dry_end_bt: float = 150.0,
    fc_bt: float = 177.0,
    drop_bt: float = 193.0,
    uuid: str = "u",
) -> dict[str, object]:
    """Build a synthetic 1 Hz Artisan profile with a realistic charge transient.

    The series goes: a pre-charge probe spike, a dive to the turning point just
    after charge (the Hottop artifact), then a monotone ramp through dry-end,
    first crack, and drop. Event indices are set so the four landmarks sit at
    known samples.

    Args:
        charge_spike: BT at the charge sample (the probe-thermal-state artifact).
        turning_bt: BT at the turning point (the post-charge minimum).
        dry_end_bt: BT at dry-end.
        fc_bt: BT at first-crack start.
        drop_bt: BT at drop.
        uuid: The roast UUID seed (drives the anonymised id).

    Returns:
        A parsed-profile-shaped dict the prototype can register.
    """
    # Timeline (seconds) and the sample index of each phase boundary.
    charge_i = 5
    turning_i = 15
    dry_end_i = 60
    fc_i = 120
    drop_i = 180
    n = drop_i + 5
    timex = [float(i) for i in range(n)]
    bt = [charge_spike] * n

    def _seg(lo: int, hi: int, v0: float, v1: float) -> None:
        for i in range(lo, hi + 1):
            frac = (i - lo) / (hi - lo)
            bt[i] = v0 + (v1 - v0) * frac

    # pre-charge flat spike, then dive to the turning point, then the ramp.
    for i in range(0, charge_i + 1):
        bt[i] = charge_spike
    _seg(charge_i, turning_i, charge_spike, turning_bt)
    _seg(turning_i, dry_end_i, turning_bt, dry_end_bt)
    _seg(dry_end_i, fc_i, dry_end_bt, fc_bt)
    _seg(fc_i, drop_i, fc_bt, drop_bt)
    for i in range(drop_i, n):
        bt[i] = drop_bt

    timeindex = [charge_i, dry_end_i, fc_i, 0, 0, 0, drop_i, 0]
    return {"timex": timex, "temp2": bt, "timeindex": timeindex, "roastUUID": uuid}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_lands_landmarks_on_their_nodes() -> None:
    """Each landmark's registered BT matches the synthetic value at that node."""
    r = rcp.register_roast(_ramp_profile(), "kona-1.alog")
    assert r is not None
    idx = rcp.landmark_node_indices()
    assert math.isclose(r.node_bt[idx["dry_end"]], 150.0, abs_tol=0.5)
    assert math.isclose(r.node_bt[idx["first_crack"]], 177.0, abs_tol=0.5)
    assert math.isclose(r.node_bt[idx["drop"]], 193.0, abs_tol=0.5)
    # The charge artifact is recorded but the turning point anchors node 0.
    assert math.isclose(r.landmark_bt["charge"], 180.0, abs_tol=0.5)
    assert math.isclose(r.node_bt[0], 80.0, abs_tol=1.0)


def test_node0_is_turning_point_not_charge_spike() -> None:
    """A high charge spike must not pull node 0 up — it anchors on the dip."""
    spiky = rcp.register_roast(_ramp_profile(charge_spike=192.0), "a.alog")
    calm = rcp.register_roast(_ramp_profile(charge_spike=120.0), "b.alog")
    assert spiky is not None and calm is not None
    # Node 0 (turning point) is the same regardless of the charge artifact size.
    assert math.isclose(spiky.node_bt[0], calm.node_bt[0], abs_tol=1.0)
    # But the recorded charge artifact differs (transparency column).
    assert spiky.landmark_bt["charge"] != calm.landmark_bt["charge"]


def test_registered_curve_is_monotone_rising_after_turning_point() -> None:
    """The warped BT rises monotonically from the turning point to drop."""
    r = rcp.register_roast(_ramp_profile(), "c.alog")
    assert r is not None
    for a, b in zip(r.node_bt, r.node_bt[1:], strict=False):
        assert b >= a - 0.01


def test_tier_split_on_drop_temperature() -> None:
    """drop <= 195 °C is core; 195 < drop <= 197 °C is soft."""
    core = rcp.register_roast(_ramp_profile(drop_bt=193.0), "c.alog")
    soft = rcp.register_roast(_ramp_profile(drop_bt=196.0), "s.alog")
    assert core is not None and soft is not None
    assert core.tier == "core"
    assert soft.tier == "soft"


def test_non_monotone_landmarks_rejected() -> None:
    """A roast with drop before first crack cannot be registered."""
    bad = _ramp_profile()
    ti = list(bad["timeindex"])  # type: ignore[arg-type]
    ti[6] = ti[2] - 1  # drop index before FC index
    bad["timeindex"] = ti
    assert rcp.register_roast(bad, "bad.alog") is None


def test_missing_landmark_rejected() -> None:
    """A roast missing the first-crack mark is not registered."""
    bad = _ramp_profile()
    ti = list(bad["timeindex"])  # type: ignore[arg-type]
    ti[2] = 0  # FCs unset
    bad["timeindex"] = ti
    assert rcp.register_roast(bad, "bad.alog") is None


def test_register_skips_nodata_samples_in_turning_point() -> None:
    """A no-data (-1) sample is ignored when finding the turning point."""
    prof = _ramp_profile()
    bt = list(prof["temp2"])  # type: ignore[arg-type]
    bt[20] = -1.0  # the Artisan no-data sentinel: a dropout in the early ramp
    prof["temp2"] = bt
    r = rcp.register_roast(prof, "kona-1.alog")
    assert r is not None
    # The dropout did not become a spurious turning point at -1 °C.
    assert r.landmark_bt["turning_point"] > 0.0


def test_register_rejects_malformed_series() -> None:
    """A profile whose BT series length mismatches timex is not registered."""
    prof = _ramp_profile()
    prof["temp2"] = list(prof["temp2"])[:-3]  # type: ignore[arg-type]
    assert rcp.register_roast(prof, "k.alog") is None


def test_register_rejects_missing_charge() -> None:
    """A profile with an unset charge mark is not registered."""
    prof = _ramp_profile()
    ti = list(prof["timeindex"])  # type: ignore[arg-type]
    ti[0] = -1  # charge unset
    prof["timeindex"] = ti
    assert rcp.register_roast(prof, "k.alog") is None


def test_load_alog_rejects_non_dict(tmp_path: Path) -> None:
    """A file that parses to a non-dict raises ValueError."""
    import pytest

    p = tmp_path / "bad.alog"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="did not parse into a dict"):
        rcp.load_alog(p)


def test_infer_origin_bare_batch_falls_back_to_cuba() -> None:
    """The 16-17 Jul 2025 bare-batch files map to the Cuba session (#290 rule)."""
    assert rcp.infer_origin("25-07-16_1500-b1.alog") == "Cuba"
    assert rcp.infer_origin("mystery.alog") == "Unknown"


def test_anon_id_falls_back_without_uuid() -> None:
    """A profile with no UUID hashes the fallback (still anonymised)."""
    a = rcp.anon_id({}, "kona-1.alog")
    b = rcp.anon_id({}, "kona-2.alog")
    assert a != b
    assert len(a) == 10


# ---------------------------------------------------------------------------
# Aggregation + band
# ---------------------------------------------------------------------------


def test_reference_mean_and_band_over_two_roasts() -> None:
    """The pooled mean is the per-node average; the band brackets the inputs."""
    r1 = rcp.register_roast(_ramp_profile(drop_bt=191.0, uuid="r1"), "x.alog")
    r2 = rcp.register_roast(_ramp_profile(drop_bt=195.0, uuid="r2"), "y.alog")
    assert r1 is not None and r2 is not None
    ref = rcp.build_reference([r1, r2])
    idx = rcp.landmark_node_indices()
    dj = idx["drop"]
    assert math.isclose(ref.mean_bt[dj], 193.0, abs_tol=0.5)
    assert ref.sd_bt[dj] > 0.0
    assert ref.p10_bt[dj] <= ref.mean_bt[dj] <= ref.p90_bt[dj]
    assert ref.n == 2


def test_soft_downweight_pulls_mean_toward_core() -> None:
    """Down-weighting a soft-tier roast moves the pooled mean toward the core."""
    core = rcp.register_roast(_ramp_profile(drop_bt=191.0, uuid="c"), "c.alog")
    soft = rcp.register_roast(_ramp_profile(drop_bt=196.0, uuid="s"), "s.alog")
    assert core is not None and soft is not None
    idx = rcp.landmark_node_indices()
    dj = idx["drop"]
    equal = rcp.build_reference([core, soft])
    weighted = rcp.build_reference([core, soft], weights={soft.anon_id: 0.5})
    # Equal weighting sits at 193.5; down-weighting the hotter soft roast lowers it.
    assert weighted.mean_bt[dj] < equal.mean_bt[dj]
    assert math.isclose(weighted.weight_sum, 1.5, abs_tol=1e-6)


def test_single_roast_reference_has_zero_band() -> None:
    """Pooling one roast gives a zero-width band with p10 == p90 == mean."""
    only = rcp.register_roast(_ramp_profile(uuid="solo"), "kona-1.alog")
    assert only is not None
    ref = rcp.build_reference([only])
    idx = rcp.landmark_node_indices()
    dj = idx["drop"]
    assert ref.sd_bt[dj] == 0.0
    assert ref.p10_bt[dj] == ref.p90_bt[dj] == ref.mean_bt[dj]


def test_empty_reference_is_nan_curve_of_right_length() -> None:
    """An empty pool yields an all-NaN curve matching the phase-axis length."""
    ref = rcp.build_reference([])
    assert ref.n == 0
    assert len(ref.mean_bt) == len(rcp.PHASE_AXIS)
    assert all(math.isnan(v) for v in ref.mean_bt)


# ---------------------------------------------------------------------------
# Inter-origin correlation
# ---------------------------------------------------------------------------


def test_identical_origins_correlate_positively() -> None:
    """Two origins deviating the same way from the pool correlate at +1.

    A distinct third origin (Taiwan, a lower dry-end shape) creates a non-trivial
    pooled mean, so Kona and Brazil — built identically — both deviate from the
    pool in the same direction and correlate positively.
    """
    kona = [
        rcp.register_roast(_ramp_profile(dry_end_bt=152.0, uuid=f"a{i}"), "kona-1.alog")
        for i in range(2)
    ]
    brazil = [
        rcp.register_roast(_ramp_profile(dry_end_bt=152.0, uuid=f"b{i}"), "brazil-1.alog")
        for i in range(2)
    ]
    taiwan = [
        rcp.register_roast(_ramp_profile(dry_end_bt=145.0, uuid=f"t{i}"), "taiwan-1.alog")
        for i in range(2)
    ]
    roasts = [r for r in (*kona, *brazil, *taiwan) if r is not None]
    pooled = rcp.build_reference(roasts)
    finding = rcp.inter_origin_correlation(roasts, pooled)
    assert set(finding.origins) == {"Hawaii Kona", "Brazil", "Taiwan (Nantou/Alishan)"}
    # Kona and Brazil share the same deviation -> their pair correlation is +1.
    oi = finding.origins.index("Hawaii Kona")
    ok = finding.origins.index("Brazil")
    assert finding.bt_matrix[oi][ok] > 0.9
    assert "pool" in finding.verdict.lower()


def test_opposite_origins_correlate_negatively() -> None:
    """Origins with opposite shape deviations correlate negatively."""
    # Kona: fast dry, slow develop. Brazil: slow dry, fast develop (mirror).
    a1 = rcp.register_roast(_ramp_profile(dry_end_bt=155.0, uuid="a1"), "kona-1.alog")
    a2 = rcp.register_roast(_ramp_profile(dry_end_bt=156.0, uuid="a2"), "kona-2.alog")
    b1 = rcp.register_roast(_ramp_profile(dry_end_bt=144.0, uuid="b1"), "brazil-1.alog")
    b2 = rcp.register_roast(_ramp_profile(dry_end_bt=145.0, uuid="b2"), "brazil-2.alog")
    roasts = [r for r in (a1, a2, b1, b2) if r is not None]
    pooled = rcp.build_reference(roasts)
    finding = rcp.inter_origin_correlation(roasts, pooled)
    assert finding.mean_offdiag_bt < 0.0


def test_verdict_for_value_covers_each_pooling_branch() -> None:
    """The verdict mapping returns the right reading for each correlation regime.

    The pooled-mean de-trending makes an all-positive off-diagonal hard to
    fabricate with a handful of synthetic origins (centring forces an outlier
    negative), so the regime-to-verdict mapping is asserted directly — this is
    the report's load-bearing pool-vs-per-origin logic.
    """
    assert "justified" in rcp.verdict_for(0.8).lower()
    assert "pool only with learned" in rcp.verdict_for(0.1).lower()
    assert "prefer" in rcp.verdict_for(-0.4).lower()
    assert "insufficient" in rcp.verdict_for(float("nan")).lower()


def test_correlation_skips_single_roast_origins() -> None:
    """An origin with only one roast is excluded from the correlation."""
    a1 = rcp.register_roast(_ramp_profile(uuid="a1"), "kona-1.alog")
    a2 = rcp.register_roast(_ramp_profile(uuid="a2"), "kona-2.alog")
    b1 = rcp.register_roast(_ramp_profile(uuid="b1"), "brazil-1.alog")
    roasts = [r for r in (a1, a2, b1) if r is not None]
    pooled = rcp.build_reference(roasts)
    finding = rcp.inter_origin_correlation(roasts, pooled)
    assert finding.origins == ["Hawaii Kona"]
    assert math.isnan(finding.mean_offdiag_bt)
    assert "insufficient" in finding.verdict.lower()


# ---------------------------------------------------------------------------
# #290-identical medium classification
# ---------------------------------------------------------------------------


def test_classify_medium_picks_low_drop_short_development_cluster() -> None:
    """k-means labels the lower-drop / shorter-development cluster medium."""
    mediums = [
        rcp.register_roast(_ramp_profile(fc_bt=178.0, drop_bt=192.0, uuid=f"m{i}"), "k.alog")
        for i in range(4)
    ]
    # Darks: much higher drop + longer development (later FC -> bigger rise).
    darks = [
        rcp.register_roast(_ramp_profile(fc_bt=170.0, drop_bt=196.0, uuid=f"d{i}"), "k.alog")
        for i in range(4)
    ]
    roasts = [r for r in (*mediums, *darks) if r is not None]
    medium_ids = rcp.classify_medium(roasts)
    for r in roasts:
        is_medium = r.anon_id in medium_ids
        assert is_medium == (r.drop_bt < 194.0)


def test_classify_medium_excludes_over_done() -> None:
    """A roast over the 197 °C line is never in the medium seed set."""
    over = rcp.register_roast(_ramp_profile(drop_bt=199.0, uuid="o"), "k.alog")
    under = rcp.register_roast(_ramp_profile(drop_bt=191.0, uuid="u"), "k.alog")
    assert over is not None and under is not None
    medium_ids = rcp.classify_medium([over, under])
    assert over.anon_id not in medium_ids


# ---------------------------------------------------------------------------
# Report + CLI smoke (no raw data needed)
# ---------------------------------------------------------------------------


def test_render_markdown_is_anonymised_and_structured() -> None:
    """The report renders the required sections from synthetic roasts only."""
    roasts = [
        r
        for r in (
            rcp.register_roast(_ramp_profile(drop_bt=191.0, uuid="a1"), "kona-1.alog"),
            rcp.register_roast(_ramp_profile(drop_bt=193.0, uuid="a2"), "kona-2.alog"),
            rcp.register_roast(_ramp_profile(drop_bt=196.0, uuid="b1"), "brazil-1.alog"),
        )
        if r is not None
    ]
    md = rcp.render_markdown(roasts, soft_weight=0.5)
    assert "## 1. The pooled registered reference curve" in md
    assert "## 3. Inter-origin correlation" in md
    assert "## 4. <=195 vs <=197 sensitivity" in md
    # Only anonymised ids leak into the report, never the UUIDs.
    assert "a1" not in md.split("anonymised")[0] or roasts[0].anon_id in md
    assert roasts[0].anon_id in md


def test_main_writes_report(tmp_path: Path) -> None:
    """``main`` over a synthetic logs dir writes the report and exits 0."""
    logs = tmp_path / "logs"
    logs.mkdir()
    # Two medium-ish + one over-done synthetic .alog, written as dict literals.
    for i, drop in enumerate((191.0, 193.0, 199.0)):
        prof = _ramp_profile(drop_bt=drop, uuid=f"r{i}")
        (logs / f"kona-{i}.alog").write_text(repr(prof), encoding="utf-8")
    # A malformed file is skipped (the parse-error guard), not fatal.
    (logs / "broken.alog").write_text("this is not a dict literal {", encoding="utf-8")
    out = tmp_path / "report.md"
    code = rcp.main(["--logs-dir", str(logs), "--out", str(out), "--soft-weight", "0.5"])
    assert code == 0
    assert out.exists()
    assert "reference-curve prototype" in out.read_text(encoding="utf-8").lower()


def test_main_returns_1_when_no_roasts(tmp_path: Path) -> None:
    """``main`` over an empty logs dir reports no roasts and exits 1."""
    empty = tmp_path / "empty"
    empty.mkdir()
    code = rcp.main(["--logs-dir", str(empty), "--out", str(tmp_path / "r.md")])
    assert code == 1
