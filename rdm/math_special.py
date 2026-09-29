"""Beta-distribution helpers used to turn an event loss table into an EP curve.

Port of MathSpecial.cs. Log-gamma and the regularized incomplete beta follow
Numerical Recipes (3rd ed.) sections 6.1 and 6.4.
"""

import math

import numpy as np

_LANCZOS = (
    0.99999999999980993,
    676.5203681218851,
    -1259.1392167224028,
    771.32342877765313,
    -176.61502916214059,
    12.507343278686905,
    -0.13857109526572012,
    9.9843695780195716e-6,
    1.5056327351493116e-7,
)


def log_gamma(z):
    """log(Γ(z)) via the Lanczos approximation. z must be positive."""
    if z < 0.5:
        return math.log(math.pi / math.sin(math.pi * z)) - log_gamma(1.0 - z)
    z -= 1.0
    x = _LANCZOS[0]
    for i in range(1, 9):
        x += _LANCZOS[i] / (z + i)
    t = z + 7.5
    return 0.5 * math.log(2.0 * math.pi) + (z + 0.5) * math.log(t) - t + math.log(x)


def regularized_incomplete_beta(a, b, x):
    """I_x(a, b), the CDF of Beta(a, b) at x. Result is in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if a <= 0.0 or b <= 0.0:
        raise ValueError("a and b must be positive.")
    log_bt = log_gamma(a + b) - log_gamma(a) - log_gamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _beta_continued_fraction(a, b, x) / a
    return 1.0 - bt * _beta_continued_fraction(b, a, 1.0 - x) / b


def _beta_continued_fraction(a, b, x):
    max_iter = 200
    eps = 3.0e-16
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    raise ArithmeticError("Incomplete beta continued fraction failed to converge.")


def regularized_incomplete_beta_array(a, b, x):
    """Vector I_x(a, b). Same Lanczos continued fraction as the scalar form."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    x = np.clip(np.asarray(x, dtype=float), 0.0, 1.0)
    out = np.zeros(a.shape, dtype=float)
    valid = (a > 0.0) & (b > 0.0) & (x > 0.0) & (x < 1.0)
    if not np.any(valid):
        out[x >= 1.0] = 1.0
        return out
    aa = a[valid]
    bb = b[valid]
    xx = x[valid]
    log_bt = (
        _log_gamma_array(aa + bb) - _log_gamma_array(aa) - _log_gamma_array(bb)
        + aa * np.log(xx) + bb * np.log(1.0 - xx)
    )
    bt = np.exp(log_bt)
    use_cf = xx < (aa + 1.0) / (aa + bb + 2.0)
    cdf = np.empty(aa.shape, dtype=float)
    if np.any(use_cf):
        cdf[use_cf] = bt[use_cf] * _beta_continued_fraction_array(aa[use_cf], bb[use_cf], xx[use_cf]) / aa[use_cf]
    other = ~use_cf
    if np.any(other):
        cdf[other] = 1.0 - bt[other] * _beta_continued_fraction_array(bb[other], aa[other], 1.0 - xx[other]) / bb[other]
    out[valid] = cdf
    out[x >= 1.0] = 1.0
    return out


def _log_gamma_array(z):
    z = np.asarray(z, dtype=float)
    out = np.empty(z.shape, dtype=float)
    reflect = z < 0.5
    if np.any(reflect):
        zr = z[reflect]
        out[reflect] = np.log(np.pi / np.sin(np.pi * zr)) - _log_gamma_array(1.0 - zr)
    direct = ~reflect
    if np.any(direct):
        zd = z[direct] - 1.0
        x = np.full(zd.shape, _LANCZOS[0])
        for i in range(1, 9):
            x = x + _LANCZOS[i] / (zd + i)
        t = zd + 7.5
        out[direct] = 0.5 * np.log(2.0 * np.pi) + (zd + 0.5) * np.log(t) - t + np.log(x)
    return out


def _beta_continued_fraction_array(a, b, x):
    eps = 3.0e-12
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = np.ones(a.shape, dtype=float)
    d = 1.0 - qab * x / qap
    d = np.where(np.abs(d) < fpmin, fpmin, d)
    d = 1.0 / d
    h = d.copy()
    active = np.ones(a.shape, dtype=bool)
    for m in range(1, 201):
        idx = np.flatnonzero(active)
        if idx.size == 0:
            break
        m2 = 2 * m
        aa = m * (b[idx] - m) * x[idx] / ((qam[idx] + m2) * (a[idx] + m2))
        d_i = 1.0 + aa * d[idx]
        d_i = np.where(np.abs(d_i) < fpmin, fpmin, d_i)
        c_i = 1.0 + aa / c[idx]
        c_i = np.where(np.abs(c_i) < fpmin, fpmin, c_i)
        d_i = 1.0 / d_i
        h_i = h[idx] * d_i * c_i
        aa = -(a[idx] + m) * (qab[idx] + m) * x[idx] / ((a[idx] + m2) * (qap[idx] + m2))
        d_i = 1.0 + aa * d_i
        d_i = np.where(np.abs(d_i) < fpmin, fpmin, d_i)
        c_i = 1.0 + aa / c_i
        c_i = np.where(np.abs(c_i) < fpmin, fpmin, c_i)
        d_i = 1.0 / d_i
        delta = d_i * c_i
        h[idx] = h_i * delta
        d[idx] = d_i
        c[idx] = c_i
        active[idx] = np.abs(delta - 1.0) >= eps
    return h
