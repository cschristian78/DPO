"""Synthetic-data tests for the leave-one-out PML computation.

Run:  python tests/test_analysis.py
(No database needed - pure computation.)
"""

import numpy as np
import pandas as pd

from rdm.analysis import (TARGET_POINTS, downsample_curve, ep_curve,
                          leave_one_out_pml, pml_at_points)


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
    test_ep_curve_ranking()
    test_pml_interpolation_monotonic()
    test_leave_one_out_properties()
    test_tail_dominant_entity_identified()
    test_downsample()
    print("All computation tests passed.")
