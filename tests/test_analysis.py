"""Synthetic-data tests for the leave-one-out PML computation.

Run:  python tests/test_analysis.py
(No database needed - pure computation.)
"""

import numpy as np
import pandas as pd

from rdm.analysis import (TARGET_POINTS, annual_metrics, downsample_curve,
                          ep_curve, excluding_annual, excluding_pml,
                          leave_one_out_pml, occurrence_ep, pml_at_points,
                          pml_from_elt, portfolio_pml_from_portep, subtract_elt)
from rdm.math_special import log_gamma, regularized_incomplete_beta


def make_matrix(n_events=3000, n_entities=12, seed=7):
    rng = np.random.default_rng(seed)
    # heavy-tailed-ish event losses per entity
    mat = rng.pareto(1.6, size=(n_events, n_entities)) * 1e6
    mat[rng.random(mat.shape) < 0.35] = 0.0  # sparsity: entities miss events
    return pd.DataFrame(mat, columns=[f"ENT-{i:03d}" for i in range(n_entities)])


def test_ep_curve_ranking():
    losses = np.array([10.0, 30.0, 20.0])
    tps, l = ep_curve(losses)
    assert list(l) == [10.0, 20.0, 30.0], "losses must be ascending"
    # N=3 -> T = (N+1)/rank_from_top = 4/3, 2, 4
    assert np.allclose(tps, [4.0 / 3.0, 2.0, 4.0])
    assert np.all(np.diff(tps) > 0), "return periods must ascend"


def test_beta_matches_known_values():
    from rdm.math_special import regularized_incomplete_beta_array
    assert abs(log_gamma(1.0)) < 1e-12
    assert abs(log_gamma(5.0) - np.log(24.0)) < 1e-10
    assert abs(regularized_incomplete_beta(1.0, 1.0, 0.5) - 0.5) < 1e-12
    assert abs(regularized_incomplete_beta(2.0, 2.0, 0.5) - 0.5) < 1e-8
    xs = np.array([0.2, 0.5, 0.8])
    got = regularized_incomplete_beta_array(np.full(3, 2.0), np.full(3, 5.0), xs)
    expect = [regularized_incomplete_beta(2.0, 5.0, float(x)) for x in xs]
    assert np.allclose(got, expect, atol=1e-8)


def test_annual_metrics_from_event_rate():
    aal, stddev = annual_metrics([100.0], [0.0], [0.1])
    assert abs(aal - 10.0) < 1e-9
    assert abs(stddev - np.sqrt(1000.0)) < 1e-9
    excl_aal, excl_std = excluding_annual([100.0], [0.0], [0.2], [40.0])
    assert abs(excl_aal - 12.0) < 1e-9
    assert abs(excl_std - np.sqrt(720.0)) < 1e-9


def test_excluding_curve_annual_metrics():
    values, aal, stddev = excluding_pml(
        [1, 2],
        [100.0, 40.0],
        [10.0, 0.0],
        [100.0, 40.0],
        [0.2, 0.5],
        {1: 25.0},
        [10])
    assert len(values) == 1
    expect_aal, expect_std = annual_metrics([75.0, 40.0], [7.5, 0.0], [0.2, 0.5])
    assert abs(aal - expect_aal) < 1e-9
    assert abs(stddev - expect_std) < 1e-9
    assert abs(aal - (0.2 * 75.0 + 0.5 * 40.0)) < 1e-9


def test_subtracted_elt_curve():
    remaining = subtract_elt([100.0, 40.0], [25.0, 40.0])
    assert list(remaining) == [75.0, 0.0]
    # One certain-enough event: mean 50, no uncertainty, rate 0.01.
    # Occurrence EP below the mean is 1-exp(-0.01) ≈ 0.00995 (about 100.5 years).
    ep = occurrence_ep([50.0], [0.0], [50.0], [0.01], 10.0)
    assert abs(ep - (1.0 - np.exp(-0.01))) < 1e-12
    losses = pml_from_elt([50.0], [0.0], [50.0], [0.01], [200])
    assert abs(losses[0] - 50.0) < 1.0


def test_portfolio_curve_uses_exceedance():
    # EP 0.1 is a 10-year return period with loss 4e6.
    out = portfolio_pml_from_portep([0.5, 0.1, -1], [1e6, 4e6, -1], [10])
    assert out[0] == 4e6


def test_pml_interpolation_monotonic():
    tps = np.array([2.0, 4.0, 10.0, 100.0])
    losses = np.array([2.0, 3.0, 4.0, 5.0]) * 1e6
    out = pml_at_points(tps, losses, [5, 10, 25, 50])
    assert np.all(np.diff(out) >= 0), "PML must rise as return period rises"
    assert out[1] == 4e6, "exact point on the curve must interpolate exactly"


def test_leave_one_out_properties():
    mat = make_matrix()
    excl, diff, portfolio_pml, curve = leave_one_out_pml(mat)

    # shapes
    assert list(excl.columns) == [f"T{p}" for p in TARGET_POINTS]
    assert list(diff.columns) == [f"T{p}" for p in TARGET_POINTS]
    assert excl.index[0] == "PORTFOLIO"
    assert len(excl) == mat.shape[1] + 1
    assert len(diff) == mat.shape[1]

    # portfolio row consistent
    assert np.allclose(excl.loc["PORTFOLIO"].to_numpy(), portfolio_pml)

    # diffs non-negative (removing an entity cannot add loss)
    assert (diff.to_numpy() >= -1e-6).all(), "DIFF must be >= 0"

    # excl PML never exceeds portfolio PML
    assert (excl.iloc[1:].to_numpy() <=
            portfolio_pml + 1e-6).all(), "EXCL rows must be <= portfolio"

    # diff == portfolio - excl (row-wise)
    recon = portfolio_pml - excl.iloc[1:].to_numpy()
    assert np.allclose(diff.to_numpy(), np.maximum(recon, 0.0), atol=1e-6)

    # determinism
    excl2, diff2, _, _ = leave_one_out_pml(mat)
    assert excl.equals(excl2) and diff.equals(diff2)

    # curve sanity
    tps, l = curve
    assert len(tps) == mat.shape[0] and np.all(np.diff(l) >= 0)


def test_tail_dominant_entity_identified():
    # One entity owns the single largest event; with N=1000 the T1000
    # point sits at rank ~1, so its marginal impact there must dominate.
    n = 1000
    rng = np.random.default_rng(3)
    mat = pd.DataFrame({
        "BIG": np.concatenate([[50e6], np.zeros(n - 1)]),
        "small1": rng.pareto(2.0, n) * 1e5,
        "small2": rng.pareto(2.0, n) * 1e5,
    })
    excl, diff, _, _ = leave_one_out_pml(mat)
    assert diff.loc["BIG", "T1000"] > 0
    assert diff.loc["BIG", "T1000"] == diff["T1000"].max()
    # ...but at a frequent point the small fry matter relatively more
    assert diff.loc["BIG", "T5"] < diff.loc["BIG", "T1000"]


def test_downsample():
    t = np.geomspace(1, 1000, 5000)
    l = np.linspace(1e6, 1e3, 5000)
    td, ld = downsample_curve(t, l, max_points=200)
    assert len(td) <= 200 and td[0] == t[0] and td[-1] == t[-1]


if __name__ == "__main__":
    test_beta_matches_known_values()
    test_annual_metrics_from_event_rate()
    test_excluding_curve_annual_metrics()
    test_subtracted_elt_curve()
    test_ep_curve_ranking()
    test_pml_interpolation_monotonic()
    test_leave_one_out_properties()
    test_tail_dominant_entity_identified()
    test_downsample()
    print("All computation tests passed.")
