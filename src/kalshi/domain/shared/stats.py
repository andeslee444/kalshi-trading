"""Shared statistical helper functions used across domain models."""

from __future__ import annotations

import math


def _norm_cdf(x):
    """Standard normal CDF. P(Z <= x) using math.erf."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _owens_t(h, a):
    """Owen's T function via 10-point Gauss-Legendre quadrature."""
    if abs(a) < 1e-15:
        return 0.0

    gl_nodes = [
        -0.9739065285171717, -0.8650633666889845, -0.6794095682990244,
        -0.4333953941292472, -0.1488743389816312,
        0.1488743389816312, 0.4333953941292472, 0.6794095682990244,
        0.8650633666889845, 0.9739065285171717,
    ]
    gl_weights = [
        0.0666713443086881, 0.1494513491505806, 0.2190863625159820,
        0.2692667193099963, 0.2955242247147529,
        0.2955242247147529, 0.2692667193099963, 0.2190863625159820,
        0.1494513491505806, 0.0666713443086881,
    ]

    half_a = a / 2.0
    mid_a = a / 2.0

    result = 0.0
    h_sq = h * h
    for i in range(10):
        t = mid_a + half_a * gl_nodes[i]
        t_sq = t * t
        integrand = math.exp(-0.5 * h_sq * (1 + t_sq)) / (1 + t_sq)
        result += gl_weights[i] * integrand

    return result * half_a / (2.0 * math.pi)


def _skew_normal_cdf(x, alpha=0.0):
    """Skew-normal CDF. alpha=0 reduces to standard normal."""
    result = _norm_cdf(x) - 2.0 * _owens_t(x, alpha)
    return max(0.0, min(1.0, result))


def _ln_gamma(x):
    """Log-gamma via Lanczos approximation (g=7, n=9)."""
    if x <= 0:
        return float("inf")
    coefs = [
        0.99999999999980993,
        676.5203681218851,
        -1259.1392167224028,
        771.32342877765313,
        -176.61502916214059,
        12.507343278686905,
        -0.13857109526572012,
        9.9843695780195716e-6,
        1.5056327351493116e-7,
    ]
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _ln_gamma(1 - x)
    x -= 1
    a = coefs[0]
    t = x + 7.5
    for i in range(1, 9):
        a += coefs[i] / (x + i)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _regularized_beta_cf(x, a, b, max_iter=200, tol=1e-12):
    """Regularized incomplete beta I_x(a, b) via continued fraction."""
    if x < 0 or x > 1:
        return 0.0
    if x == 0 or x == 1:
        return x

    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_beta_cf(1 - x, b, a, max_iter, tol)

    ln_prefactor = a * math.log(x) + b * math.log(1 - x) - math.log(a) \
                   + _ln_gamma(a + b) - _ln_gamma(a) - _ln_gamma(b)
    prefactor = math.exp(ln_prefactor)

    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d

    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c

        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < tol:
            break

    return prefactor * h


def _student_t_cdf(x, df=6):
    """Student's t CDF using regularized incomplete beta."""
    if df <= 0:
        return _norm_cdf(x)
    t2 = x * x
    ix = _regularized_beta_cf(df / (df + t2), df / 2.0, 0.5)
    cdf = 0.5 * ix
    if x >= 0:
        return 1.0 - cdf
    return cdf


__all__ = ["_norm_cdf", "_skew_normal_cdf", "_student_t_cdf"]
