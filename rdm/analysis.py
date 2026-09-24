"""Core computation: leave-one-out marginal PML analysis.

Methodology
-----------
Given an event x entity loss matrix for one analysis, one granularity
(account / policy / location) and one loss perspective (ground up / gross /
net before tax):

1. Portfolio event loss = sum of entity losses per event.  (Assumption: the
   entity-level ELTs partition the portfolio, so the portfolio ELT is the
   sum of the entity ELTs at the chosen granularity.)
2. The EP (exceedance probability) curve is built by ranking: with N events,
   the i-th largest loss L_(i) gets EP_i = i / (N + 1), i.e. return period
   T_i = (N + 1) / i.
3. PML at a target return period T is obtained by linear interpolation of
   loss against exceedance probability (1/T) between the two bracketing
   ranked events. Targets rarer than any modelled event are capped at the
   largest modelled loss; targets more frequent than any modelled event take
   the smallest modelled loss.
4. Leave-one-out: for each entity e, the excluding-e event loss vector is
   (portfolio event loss - entity e event loss), floored at zero. Its EP
   curve is rebuilt and interpolated at the target points -> EXCL row.
5. DIFF row for entity e = portfolio PML - excluding-e PML at each point.
   Removing an entity cannot increase losses, so differences are >= 0
   (tiny negative interpolation noise is clipped to 0).
"""

import numpy as np
import pandas as pd

# Exceedance-probability / return-period points used on every PML/XPML curve.
TARGET_POINTS = [5, 10, 25, 50, 75, 100, 130, 250, 500, 750, 1000]


def ep_curve(event_losses):
    """Build a ranked EP curve from a vector of per-event losses.

    Returns (return_periods, losses): both ascending and element-wise
    paired (most frequent / smallest loss first, rarest / largest last).
    """
    losses = np.asarray(event_losses, dtype=float)
    losses = losses[np.isfinite(losses)]
    if losses.size == 0:
        return np.array([]), np.array([])
    losses = np.sort(losses)  # ascending
    n = losses.size
    # rank counted from the top: the largest loss has rank 1
    ranks_from_top = np.arange(n, 0, -1)
    eps = ranks_from_top / (n + 1.0)
    tps = 1.0 / eps  # ascending
    return tps, losses


def pml_at_points(return_periods, losses, points=TARGET_POINTS):
    """Interpolate losses at target return periods (linear in EP space)."""
    return_periods = np.asarray(return_periods, dtype=float)
    losses = np.asarray(losses, dtype=float)
    points = np.asarray(points, dtype=float)
    if return_periods.size == 0:
        return np.full(points.shape, np.nan)
    order = np.argsort(return_periods)
    t = return_periods[order]
    l = losses[order]
    ep = 1.0 / t                  # descending as T ascends
    ep_a, l_a = ep[::-1], l[::-1]  # ascending in EP for np.interp
    target_ep = 1.0 / points
    # Rarer than any modelled event -> cap at the largest modelled loss;
    # more frequent than any modelled event -> smallest modelled loss.
    vals = np.interp(target_ep, ep_a, l_a, left=l_a[0], right=l_a[-1])
    return vals


def downsample_curve(return_periods, losses, max_points=1000):
    """Evenly thin a curve for charting."""
    t = np.asarray(return_periods, dtype=float)
    l = np.asarray(losses, dtype=float)
    if t.size <= max_points:
        return t, l
    idx = np.unique(np.linspace(0, t.size - 1, max_points).astype(int))
    return t[idx], l[idx]


def leave_one_out_pml(loss_matrix, points=TARGET_POINTS):
    """Run the full leave-one-out marginal PML computation.

    loss_matrix: DataFrame indexed by event_id, one column per entity,
                 values = loss under the chosen perspective.
    points:      return-period points for the PML/XPML output.

    Returns (excl_df, diff_df, portfolio_pml, portfolio_curve):
      excl_df:        first row 'PORTFOLIO', then one row per entity removed;
                      columns T5..T1000.
      diff_df:        one row per entity: portfolio PML minus excl PML.
      portfolio_pml:  1-D array of portfolio PML at `points`.
      portfolio_curve:(return_periods, losses) of the portfolio EP curve.
    """
    mat = loss_matrix.fillna(0.0)
    entity_ids = [str(c) for c in mat.columns]
    mat = mat.to_numpy(dtype=float)

    portfolio = mat.sum(axis=1)
    p_tps, p_losses = ep_curve(portfolio)
    portfolio_pml = pml_at_points(p_tps, p_losses, points)
    col_names = [f"T{p}" for p in points]

    excl_rows = []
    for j in range(mat.shape[1]):
        excl_event = portfolio - mat[:, j]
        np.maximum(excl_event, 0.0, out=excl_event)  # floor at zero
        tps, losses = ep_curve(excl_event)
        excl_rows.append(pml_at_points(tps, losses, points))
    excl = pd.DataFrame(excl_rows, index=entity_ids, columns=col_names)

    diff_vals = portfolio_pml - excl.to_numpy(dtype=float)
    np.maximum(diff_vals, 0.0, out=diff_vals)  # clip interpolation noise
    diff = pd.DataFrame(diff_vals, index=entity_ids, columns=col_names)

    portfolio_row = pd.DataFrame([portfolio_pml], index=["PORTFOLIO"],
                                 columns=col_names)
    excl_out = pd.concat([portfolio_row, excl])
    return excl_out, diff, portfolio_pml, (p_tps, p_losses)
