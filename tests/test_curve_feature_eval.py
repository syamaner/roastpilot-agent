"""Tests for the curve-feature validation harness (#229).

Network- / key- / fixture-free: the operator's ``.artisan-fixtures`` set is
gitignored and absent in CI, so these tests synthesise small deterministic
roast fixtures in a temp directory and assert the numeric machinery computes
known answers (RoR slope, correlation, FC-ETA extrapolation, turning point) and
that the report builder runs end-to-end. They assert real values, not that the
code merely executes.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import curve_feature_eval as cf  # noqa: E402


def _write_roast(
    directory: Path,
    *,
    charge_s: float,
    fc_s: float,
    drop_s: float,
    bean_at: dict[float, float],
    sample_dt: float = 1.0,
) -> Path:
    """Write a synthetic ``roast.jsonl`` with a piecewise-linear bean curve.

    Args:
        directory: Fixture directory to create.
        charge_s: ``beans_added`` time.
        fc_s: ``first_crack_detected`` time.
        drop_s: ``beans_dropped`` time.
        bean_at: Sparse ``time -> bean_temp_c`` control points; bean temp is
            linearly interpolated between them.
        sample_dt: Telemetry cadence in seconds.

    Returns:
        The created fixture directory.
    """
    directory.mkdir(parents=True, exist_ok=True)
    points = sorted(bean_at.items())
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    end = points[-1][0]
    times = np.arange(0.0, end + 1e-9, sample_dt)
    beans = np.interp(times, xs, ys)
    lines: list[str] = []
    for t, b in zip(times, beans, strict=True):
        lines.append(
            json.dumps(
                {
                    "type": "telemetry",
                    "monotonic_seconds": float(t),
                    "bean_temp_c": float(b),
                    "env_temp_c": float(b) + 30.0,
                    "heat_level_percent": 100,
                    "fan_level_percent": 30,
                }
            )
        )
    for kind, when in (
        ("beans_added", charge_s),
        ("first_crack_detected", fc_s),
        ("beans_dropped", drop_s),
    ):
        lines.append(json.dumps({"type": "event", "kind": kind, "monotonic_seconds": when}))
    (directory / "roast.jsonl").write_text("\n".join(lines) + "\n")
    return directory


# --- numeric helpers --------------------------------------------------------


def test_slope_per_min_is_exact_for_a_line() -> None:
    """A 10 °C/min line yields a slope of exactly 10."""
    t = np.arange(0.0, 60.0, 5.0)
    values = 100.0 + (10.0 / 60.0) * t  # 10 °C/min
    assert cf.slope_per_min(t, values) == pytest.approx(10.0)


def test_slope_per_min_handles_too_few_points() -> None:
    """A single point cannot define a slope."""
    assert math.isnan(cf.slope_per_min(np.array([1.0]), np.array([2.0])))


def test_ror_series_leading_nan_then_constant_slope() -> None:
    """A constant-slope curve gives a constant RoR after the window fills."""
    t = np.arange(0.0, 120.0, 5.0)
    values = 100.0 + (12.0 / 60.0) * t
    ror = cf.ror_series(t, values, span_seconds=30.0)
    assert math.isnan(ror[0])  # window not yet full
    assert ror[-1] == pytest.approx(12.0)


def test_pearson_perfect_and_anticorrelation() -> None:
    """Pearson is +1 for a rising line and -1 for a falling one."""
    x = np.arange(0.0, 10.0)
    assert cf.pearson(x, 2.0 * x + 1.0) == pytest.approx(1.0)
    assert cf.pearson(x, -3.0 * x) == pytest.approx(-1.0)


def test_pearson_degenerate_is_nan() -> None:
    """A constant series has no correlation."""
    x = np.arange(0.0, 10.0)
    assert math.isnan(cf.pearson(x, np.zeros_like(x)))


def test_spearman_monotone_nonlinear_is_one() -> None:
    """Spearman is 1 for a strictly increasing (non-linear) relationship."""
    x = np.arange(1.0, 10.0)
    assert cf.spearman(x, x**3) == pytest.approx(1.0)


def test_rankdata_averages_ties() -> None:
    """Tied values share the mean rank."""
    ranks = cf.rankdata(np.array([10.0, 10.0, 20.0]))
    assert list(ranks) == [1.5, 1.5, 3.0]


def test_approx_p_small_for_strong_correlation() -> None:
    """A near-perfect correlation at N=28 is flagged highly significant."""
    assert cf.approx_two_sided_p(0.84, 28) < 0.01
    assert math.isnan(cf.approx_two_sided_p(1.0, 28))


# --- ETA extrapolation ------------------------------------------------------


def test_eta_linear_projection_hits_target_time() -> None:
    """Linear projection: 10 °C below target at 12 °C/min => +50 s."""
    eta = cf.eta_at_tick(
        now_t=100.0,
        bean_now=165.0,
        ror_now=12.0,
        accel_per_min2=0.0,
        target_c=175.0,
        use_quadratic=False,
    )
    assert eta == pytest.approx(150.0)


def test_eta_none_when_ror_nonpositive() -> None:
    """A flat/falling curve cannot reach a higher target."""
    assert (
        cf.eta_at_tick(
            now_t=0.0,
            bean_now=160.0,
            ror_now=0.0,
            accel_per_min2=0.0,
            target_c=175.0,
            use_quadratic=False,
        )
        is None
    )


def test_eta_returns_now_when_already_past_target() -> None:
    """If bean temp already reached the band, ETA is now."""
    eta = cf.eta_at_tick(
        now_t=42.0,
        bean_now=180.0,
        ror_now=5.0,
        accel_per_min2=0.0,
        target_c=175.0,
        use_quadratic=False,
    )
    assert eta == pytest.approx(42.0)


# --- loading + features end-to-end ------------------------------------------


def test_load_roast_decimates_and_marks_events(tmp_path: Path) -> None:
    """Loading resamples to the cadence and reads event bean temps."""
    d = _write_roast(
        tmp_path / "artisan-01",
        charge_s=10.0,
        fc_s=500.0,
        drop_s=620.0,
        bean_at={0.0: 120.0, 60.0: 80.0, 500.0: 175.0, 620.0: 190.0},
    )
    roast = cf.load_roast(d, sample_seconds=5.0)
    assert roast.name == "artisan-01"
    assert float(roast.t[1] - roast.t[0]) == pytest.approx(5.0)
    assert roast.fc_bean_c == pytest.approx(175.0, abs=0.5)
    assert roast.drop_bean_c == pytest.approx(190.0, abs=0.5)
    assert roast.time_to_fc_seconds == pytest.approx(490.0)


def test_load_roast_missing_event_raises(tmp_path: Path) -> None:
    """A fixture without all three events is rejected."""
    d = tmp_path / "artisan-bad"
    d.mkdir()
    (d / "roast.jsonl").write_text(
        json.dumps(
            {
                "type": "telemetry",
                "monotonic_seconds": 0.0,
                "bean_temp_c": 100.0,
                "env_temp_c": 120.0,
                "heat_level_percent": 0,
                "fan_level_percent": 0,
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="lacks required events"):
        cf.load_roast(d)


def test_turning_point_finds_post_charge_minimum(tmp_path: Path) -> None:
    """The TP is the post-charge bean-temp minimum."""
    d = _write_roast(
        tmp_path / "artisan-02",
        charge_s=10.0,
        fc_s=500.0,
        drop_s=620.0,
        bean_at={0.0: 120.0, 10.0: 118.0, 70.0: 85.0, 500.0: 175.0, 620.0: 190.0},
    )
    roast = cf.load_roast(d, sample_seconds=5.0)
    tp_temp, time_to_tp = cf.turning_point(roast)
    assert tp_temp == pytest.approx(85.0, abs=1.0)
    assert time_to_tp == pytest.approx(60.0, abs=5.0)


def test_build_report_runs_on_synthetic_set(tmp_path: Path) -> None:
    """The full report builds across several synthetic roasts."""
    for i in range(3):
        _write_roast(
            tmp_path / f"artisan-0{i + 1}",
            charge_s=10.0,
            fc_s=500.0 + 20.0 * i,
            drop_s=620.0 + 20.0 * i,
            bean_at={
                0.0: 120.0,
                70.0: 85.0 + i,
                500.0 + 20.0 * i: 175.0,
                620.0 + 20.0 * i: 190.0 + i,
            },
        )
    report = cf.build_report(tmp_path)
    assert report.n_roasts == 3
    assert report.fc_band_c[0] <= report.fc_eta.fc_target_c <= report.fc_band_c[1]
    assert len(report.turning_point.per_roast) == 3
    # Every predictor x outcome correlation is present.
    assert len(report.turning_point.correlations) == 9


def test_build_report_honours_explicit_fc_band(tmp_path: Path) -> None:
    """An explicit FC band overrides the empirical derivation."""
    _write_roast(
        tmp_path / "artisan-01",
        charge_s=10.0,
        fc_s=500.0,
        drop_s=620.0,
        bean_at={0.0: 120.0, 70.0: 85.0, 500.0: 175.0, 620.0: 190.0},
    )
    report = cf.build_report(tmp_path, fc_band=(168.0, 182.0))
    assert report.fc_band_c == (168.0, 182.0)
    assert report.fc_eta.fc_target_c == pytest.approx(175.0)


def test_main_json_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI runs end-to-end and emits parseable JSON."""
    _write_roast(
        tmp_path / "artisan-01",
        charge_s=10.0,
        fc_s=500.0,
        drop_s=620.0,
        bean_at={0.0: 120.0, 70.0: 85.0, 500.0: 175.0, 620.0: 190.0},
    )
    rc = cf.main(["--fixtures-dir", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_roasts"] == 1


def test_load_all_empty_dir_raises(tmp_path: Path) -> None:
    """An empty fixtures directory is an error, not a silent empty report."""
    with pytest.raises(ValueError, match="no artisan-"):
        cf.load_all(tmp_path)
