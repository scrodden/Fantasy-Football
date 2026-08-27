"""Isotonic win-probability calibration.

The model's win probabilities are close but not perfect (the backtest's
calibration table shows small biases -- e.g. 85%-confidence picks winning ~81%).
Isotonic regression fits a monotonic mapping from raw probability to observed
frequency, so a stated 70% really means 70%.  Better-calibrated probabilities
directly improve moneyline expected value and Kelly staking.

Pure standard library: the fit is the Pool-Adjacent-Violators Algorithm.
"""

from __future__ import annotations


def fit(preds: list[float], outcomes: list[float]) -> list[list[float]]:
    """Fit an isotonic (non-decreasing) calibration curve.

    Returns a compact curve as ``[[x_upper, calibrated_prob], ...]`` -- for a
    query p, the calibrated value is the first block whose ``x_upper`` >= p.
    """
    pts = sorted(zip(preds, outcomes))
    if not pts:
        return []
    # PAVA: build monotonic blocks of [sum_y, n, x_upper].
    stack: list[list[float]] = []
    for x, y in pts:
        stack.append([y, 1.0, x])
        while len(stack) >= 2 and (stack[-2][0] / stack[-2][1]) > (stack[-1][0] / stack[-1][1]):
            a = stack.pop(); b = stack.pop()
            stack.append([b[0] + a[0], b[1] + a[1], a[2]])
    return [[blk[2], blk[0] / blk[1]] for blk in stack]


def apply(curve: list[list[float]], p: float) -> float:
    """Map a raw probability through the calibration curve."""
    if not curve:
        return p
    for x_upper, val in curve:
        if p <= x_upper:
            return val
    return curve[-1][1]


# ---------------------------------------------------------------------------
# Platt scaling: a gentle 2-parameter logistic recalibration. Robust (won't
# overfit like isotonic), which suits an already-decent model.
# ---------------------------------------------------------------------------
import math


def _logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1 - p))


def fit_platt(preds: list[float], outcomes: list[float], iters: int = 800,
              lr: float = 0.3) -> list[float]:
    """Fit calibrated = sigmoid(a * logit(p) + b) by gradient descent."""
    X = [_logit(p) for p in preds]
    Y = outcomes
    n = len(X) or 1
    a, b = 1.0, 0.0
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in zip(X, Y):
            pr = 1.0 / (1.0 + math.exp(-(a * x + b)))
            e = pr - y
            ga += e * x; gb += e
        a -= lr * ga / n
        b -= lr * gb / n
    return [a, b]


def apply_platt(params: list[float], p: float) -> float:
    a, b = params
    return 1.0 / (1.0 + math.exp(-(a * _logit(p) + b)))
