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

import math

import numpy as np
import pandas as pd

from rdm.math_special import regularized_incomplete_beta, regularized_incomplete_beta_array

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


def portfolio_pml_from_portep(exceedance, losses, points):
    """Interpolate rdm_portep losses at target return periods.

    exceedance is the EP column (probability). Return period is 1 / EP.
    Negative RMS sentinels are ignored.
    """
    ep = np.asarray(exceedance, dtype=float)
    values = np.asarray(losses, dtype=float)
    mask = np.isfinite(ep) & (ep > 0) & np.isfinite(values) & (values >= 0)
    if not np.any(mask):
        return np.full(len(points), np.nan)
    return pml_at_points(1.0 / ep[mask], values[mask], points)


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


def subtract_elt(portfolio_loss, entity_loss):
    """Portfolio event loss minus the loss-level event loss, floored at zero."""
    portfolio_loss = np.asarray(portfolio_loss, dtype=float)
    entity_loss = np.asarray(entity_loss, dtype=float)
    remaining = portfolio_loss - entity_loss
    np.maximum(remaining, 0.0, out=remaining)
    return remaining


def annual_metrics(mean, std, rate):
    """AAL and total standard deviation of an event loss table.

    AAL is the sum of annual rate times mean loss. The standard deviation is
    the compound-Poisson total, sqrt(sum(rate * (mean^2 + std^2))).
    """
    mean = np.asarray(mean, dtype=float)
    std = np.asarray(std, dtype=float)
    rate = np.asarray(rate, dtype=float)
    aal = float(np.sum(rate * mean))
    variance = float(np.sum(rate * (mean * mean + std * std)))
    if variance <= 0.0:
        return aal, 0.0
    return aal, math.sqrt(variance)


def excluding_annual(mean, std, rate, removed):
    """AAL and standard deviation of the portfolio after one risk is removed."""
    mean = np.asarray(mean, dtype=float)
    remaining = subtract_elt(mean, removed)
    scale = np.zeros(mean.shape, dtype=float)
    positive = mean > 0
    scale[positive] = remaining[positive] / mean[positive]
    return annual_metrics(remaining, np.asarray(std, dtype=float) * scale, rate)


def _beta_survival(mean, std, exposure, loss):
    """P(severity > loss) for one event. Zero stddev is a point mass at the mean."""
    if loss <= 0.0:
        return 1.0
    if mean <= 0.0:
        return 0.0
    maximum = exposure if exposure > mean else mean
    if loss >= maximum:
        return 0.0
    if std <= 0.0:
        return 1.0 if mean > loss else 0.0
    mu = mean / maximum
    var = (std / maximum) ** 2
    cap = mu * (1.0 - mu) * 0.99
    if var > cap:
        var = cap
    if var <= 0.0:
        return 1.0 if mean > loss else 0.0
    factor = mu * (1.0 - mu) / var - 1.0
    a = mu * factor
    b = (1.0 - mu) * factor
    return 1.0 - regularized_incomplete_beta(a, b, loss / maximum)


def _beta_survival_array(mean, std, exposure, loss):
    """P(severity > loss) for every event at one loss threshold."""
    mean = np.asarray(mean, dtype=float)
    std = np.asarray(std, dtype=float)
    exposure = np.asarray(exposure, dtype=float)
    survival = np.zeros(mean.shape, dtype=float)
    if loss <= 0.0:
        survival[mean > 0.0] = 1.0
        return survival
    maximum = np.maximum(exposure, mean)
    point = (std <= 0.0) & (mean > loss) & (mean > 0.0)
    survival[point] = 1.0
    beta = (mean > 0.0) & (std > 0.0) & (loss < maximum)
    if not np.any(beta):
        return survival
    mu = mean[beta] / maximum[beta]
    var = (std[beta] / maximum[beta]) ** 2
    cap = mu * (1.0 - mu) * 0.99
    var = np.minimum(var, cap)
    factor = np.maximum(mu * (1.0 - mu) / np.maximum(var, 1e-18) - 1.0, 1e-8)
    a = mu * factor
    b = (1.0 - mu) * factor
    cdf = regularized_incomplete_beta_array(a, b, loss / maximum[beta])
    survival[beta] = 1.0 - cdf
    return survival


def occurrence_ep(mean, std, exposure, rate, loss):
    """Occurrence exceedance probability of a loss threshold.

    Each event is an independent Poisson arrival. Severity is beta-distributed
    on [0, exposure] using the event mean and standard deviation. The caller
    supplies that standard deviation as STDDEVI + STDDEVC.
    """
    rate = np.asarray(rate, dtype=float)
    expected = float(np.sum(rate * _beta_survival_array(mean, std, exposure, loss)))
    if expected <= 0.0:
        return 0.0
    return 1.0 - math.exp(-expected)


def pml_from_elt(mean, std, exposure, rate, points):
    """Loss at each return period from an event loss table."""
    mean = np.asarray(mean, dtype=float)
    std = np.asarray(std, dtype=float)
    exposure = np.asarray(exposure, dtype=float)
    rate = np.asarray(rate, dtype=float)
    points = np.asarray(points, dtype=float)
    active = (rate > 0.0) & (mean > 0.0) & np.isfinite(mean)
    if not np.any(active):
        return np.zeros(points.shape)
    maximum = float(np.max(np.maximum(exposure[active], mean[active])))
    out = np.zeros(points.shape)
    for j, period in enumerate(points):
        if period <= 0.0:
            continue
        target = 1.0 / period
        low = 0.0
        high = maximum
        if occurrence_ep(mean, std, exposure, rate, 0.0) <= target:
            continue
        for _ in range(40):
            mid = 0.5 * (low + high)
            if occurrence_ep(mean, std, exposure, rate, mid) > target:
                low = mid
            else:
                high = mid
        out[j] = 0.5 * (low + high)
    return out


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
