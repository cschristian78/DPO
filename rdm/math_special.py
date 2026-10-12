"""Beta-distribution helpers used to turn an event loss table into an EP curve.

Port of MathSpecial.cs. Log-gamma and the regularized incomplete beta follow
Numerical Recipes (3rd ed.) sections 6.1 and 6.4.
"""

import math

import numpy as np
from scipy.special import betainc

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
    """Vector I_x(a, b). SciPy's compiled regularized incomplete beta."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    x = np.clip(np.asarray(x, dtype=float), 0.0, 1.0)
    out = np.zeros(a.shape, dtype=float)
    valid = (a > 0.0) & (b > 0.0) & (x > 0.0) & (x < 1.0)
    if np.any(valid):
        out[valid] = betainc(a[valid], b[valid], x[valid])
    out[x >= 1.0] = 1.0
    return out
