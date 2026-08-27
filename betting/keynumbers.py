"""Key-number-aware spread pricing.

Football margins aren't smoothly distributed -- they pile up on 3, 7, 10, 6, 14
(field goals and touchdowns).  Pricing a spread with a plain normal curve
therefore misvalues bets whose line sits on or near a key number: a projection
of "home by 2.5" against a line of 3 is worth more than the raw half-point of
edge implies, because 3 is a wall.

We estimate the empirical frequency of each final margin from history, turn it
into a per-margin weight (how much more/less likely than a smooth baseline), and
reweight the normal by it when computing cover / push probabilities.  The result
is a cover probability that respects the key numbers -- validated in the
backtest before it's trusted.
"""

from __future__ import annotations

import math
from collections import Counter

from fantasy import util
from betting import history

_MAXM = 70          # consider margins up to +/-70
_SEASONS = {"nfl": list(range(2015, 2026)), "cfb": [2021, 2022, 2023, 2024, 2025]}


def _smooth(freq: dict, radius: int = 3) -> dict:
    """A moving-average baseline (spikes removed) to divide against."""
    out = {}
    for m in range(0, _MAXM + 1):
        s = n = 0.0
        for d in range(-radius, radius + 1):
            k = m + d
            if 0 <= k <= _MAXM:
                s += freq.get(k, 0.0); n += 1
        out[m] = s / n if n else 0.0
    return out


def margin_weights(league: str) -> dict:
    """Per-|margin| weight = empirical frequency / smooth baseline.
    >1 on key numbers (3, 7, …), <1 in the gaps (4, 5, 8, …). Cached."""
    ckey = f"bet_keyweights_{league}"
    cached = util.cache_get(ckey, 30 * 24 * 3600)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    games = history.games(league, _SEASONS[league], with_lines=False) \
        if league == "cfb" else history.nfl_games(_SEASONS[league])
    c = Counter()
    for g in games:
        if g.get("home_score") is None:
            continue
        c[abs(g["home_score"] - g["away_score"])] += 1
    total = sum(c.values()) or 1
    freq = {m: c.get(m, 0) / total for m in range(0, _MAXM + 1)}
    base = _smooth(freq)
    weights = {}
    for m in range(0, _MAXM + 1):
        b = base[m]
        weights[m] = (freq[m] / b) if b > 1e-9 else 1.0
    # keep weights sane
    weights = {m: max(0.2, min(3.5, w)) for m, w in weights.items()}
    util.cache_put(ckey, {str(k): v for k, v in weights.items()})
    return weights


def _norm_pdf(x, mu, sd):
    z = (x - mu) / sd
    return math.exp(-0.5 * z * z)


def margin_pmf(proj_margin: float, sd: float, league: str) -> dict:
    """Probability of each integer home margin m, as normal(proj, sd) reweighted
    by the empirical key-number weights, normalized to sum to 1."""
    w = margin_weights(league)
    pmf = {}
    tot = 0.0
    for m in range(-_MAXM, _MAXM + 1):
        p = _norm_pdf(m, proj_margin, sd) * w.get(abs(m), 1.0)
        pmf[m] = p
        tot += p
    if tot <= 0:
        return {int(round(proj_margin)): 1.0}
    return {m: p / tot for m, p in pmf.items()}


def spread_cover_prob(proj_margin: float, home_spread: float, side: str,
                      sd: float, league: str) -> tuple[float, float]:
    """Return (cover_prob, push_prob) for a spread bet.

    ``home_spread`` is home-relative (negative = home favored). Betting ``home``
    covers if actual_margin + home_spread > 0; ``away`` covers if
    actual_margin + home_spread < 0; a push needs an integer line.
    """
    pmf = margin_pmf(proj_margin, sd, league)
    thr = -home_spread                      # home covers when margin > thr
    cover = push = 0.0
    integer_line = abs(home_spread - round(home_spread)) < 1e-9
    for m, p in pmf.items():
        diff = m - thr
        if abs(diff) < 1e-9 and integer_line:
            push += p
        elif (diff > 0) == (side == "home"):
            cover += p
    denom = 1 - push
    cover_adj = cover / denom if denom > 1e-9 else cover
    return cover_adj, push
