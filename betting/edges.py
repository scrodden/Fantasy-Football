"""Turn a projection + market lines into ranked betting opportunities.

For each game we compare the model's numbers to the sportsbook line on three
markets -- spread, total, and moneyline -- remove the vig, compute the expected
value of the bet, and size it with fractional Kelly.  When multiple books are
available (Odds API layer) we report the best number and which book has it, so
the user can line-shop.
"""

from __future__ import annotations

import math
from statistics import median

# Standard -110 juice on spreads/totals unless a book price says otherwise.
STD_PRICE = -110
# Only surface bets with at least this edge, to cut noise.
MIN_SPREAD_EDGE = 1.5     # points
MIN_TOTAL_EDGE = 2.0      # points
MIN_ML_EV = 0.03          # 3% expected value
KELLY_FRACTION = 0.25     # quarter-Kelly staking
# Don't trust the model on longshot moneylines -- payoff-driven "EV" there is
# noise, not alpha.  And skip games the line calls a mismatch (FBS-vs-FCS buy
# games, huge favorites): the model is unreliable and there's no bet worth it.
MAX_ML_PRICE = 400
BLOWOUT_SPREAD = {"nfl": 16.5, "cfb": 21.5}
# SD of (actual total - closing total). Empirically close to the margin SD, so
# a few points of total edge is only a small win-rate bump -- not a coin-flip
# turned 66%.
TOTAL_SD = {"nfl": 13.5, "cfb": 16.5}


# ---------------------------------------------------------------------------
# Odds math
# ---------------------------------------------------------------------------
def american_to_decimal(odds: int) -> float:
    odds = int(odds)
    return 1 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))


def american_to_prob(odds: int) -> float:
    odds = int(odds)
    return (100.0 / (odds + 100.0)) if odds > 0 else (abs(odds) / (abs(odds) + 100.0))


def devig_two_way(p_a: float, p_b: float) -> tuple[float, float]:
    s = p_a + p_b
    if s <= 0:
        return 0.5, 0.5
    return p_a / s, p_b / s


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def ev_at_price(p_win: float, price: int) -> float:
    """Expected profit per 1u staked at American ``price`` with win prob p."""
    dec = american_to_decimal(price)
    return p_win * (dec - 1) - (1 - p_win)


def kelly(p_win: float, price: int) -> float:
    b = american_to_decimal(price) - 1
    if b <= 0:
        return 0.0
    f = (b * p_win - (1 - p_win)) / b
    return max(0.0, f)


# ---------------------------------------------------------------------------
# Book aggregation
# ---------------------------------------------------------------------------
def _consensus(books: list[dict]):
    """Median line across books, plus best number available on each side."""
    hs = [b["home_spread"] for b in books if b.get("home_spread") is not None]
    tot = [b["total"] for b in books if b.get("total") is not None]
    hml = [(b["home_ml"], b.get("book")) for b in books if b.get("home_ml") is not None]
    aml = [(b["away_ml"], b.get("book")) for b in books if b.get("away_ml") is not None]
    cons = {
        "home_spread": median(hs) if hs else None,
        "total": median(tot) if tot else None,
        # best (most points) for a home bettor = max; for away = min home_spread.
        "best_home_spread": max(hs) if hs else None,
        "best_away_spread": (-min(hs)) if hs else None,
        "best_home_spread_book": None, "best_away_spread_book": None,
        "best_home_ml": None, "best_away_ml": None,
        "best_home_ml_book": None, "best_away_ml_book": None,
    }
    if hs:
        for b in books:
            if b.get("home_spread") == cons["best_home_spread"]:
                cons["best_home_spread_book"] = b.get("book")
            if b.get("home_spread") == min(hs):
                cons["best_away_spread_book"] = b.get("book")
    # best moneyline = highest american number (best payout) for each side.
    if hml:
        v, bk = max(hml, key=lambda t: t[0]); cons["best_home_ml"], cons["best_home_ml_book"] = v, bk
    if aml:
        v, bk = max(aml, key=lambda t: t[0]); cons["best_away_ml"], cons["best_away_ml_book"] = v, bk
    return cons


# ---------------------------------------------------------------------------
# Per-market edge evaluation
# ---------------------------------------------------------------------------
def _confidence(edge_pts: float, sd: float, gp: int) -> str:
    """Tiered confidence from edge size (in stdevs) tempered by sample size."""
    z = abs(edge_pts) / sd
    if gp < 4:               # thin sample early in the season
        z *= 0.6
    if z >= 0.55:
        return "high"
    if z >= 0.3:
        return "medium"
    return "low"


def evaluate(game: dict, proj: dict, league: str, min_gp: int = 0) -> list[dict]:
    """Return a list of edge dicts (may be empty) for one game."""
    books = game.get("books") or []
    if not books:
        return []
    cons = _consensus(books)
    home, away = game["home"], game["away"]
    sd = TOTAL_SD.get(league, 11.0)
    spread_sd = proj.get("_spread_sd", 13.2 if league == "nfl" else 16.5)
    gp = min(home.get("_gp", 99), away.get("_gp", 99))
    out = []

    # Mismatch guard: if the market prices a blowout, the model can't add value.
    if cons["home_spread"] is not None and \
            abs(cons["home_spread"]) >= BLOWOUT_SPREAD.get(league, 17):
        return []

    # --- Spread ---------------------------------------------------------
    if cons["home_spread"] is not None:
        line = cons["home_spread"]
        edge_home = proj["proj_margin"] + line       # >0 => value on home
        side = "home" if edge_home > 0 else "away"
        edge = abs(edge_home)
        if edge >= MIN_SPREAD_EDGE:
            p = _norm_cdf(edge / spread_sd)
            if side == "home":
                number, book = cons["best_home_spread"], cons["best_home_spread_book"]
                team = home
            else:
                number, book = cons["best_away_spread"], cons["best_away_spread_book"]
                team = away
            out.append({
                "market": "spread", "side": side, "team": team["abbr"],
                "pick": f"{team['abbr']} {number:+g}",
                "line": number, "consensus_home_spread": line,
                "book": book, "edge": round(edge, 1),
                "win_prob": round(p, 3), "price": STD_PRICE,
                "ev": round(ev_at_price(p, STD_PRICE), 3),
                "kelly": round(KELLY_FRACTION * kelly(p, STD_PRICE), 4),
                "confidence": _confidence(edge, spread_sd, gp),
            })

    # --- Total ----------------------------------------------------------
    if cons["total"] is not None:
        line = cons["total"]
        edge_t = proj["proj_total"] - line
        side = "over" if edge_t > 0 else "under"
        edge = abs(edge_t)
        if edge >= MIN_TOTAL_EDGE:
            p = _norm_cdf(edge / sd)
            out.append({
                "market": "total", "side": side, "team": None,
                "pick": f"{side.capitalize()} {line:g}",
                "line": line, "book": None, "edge": round(edge, 1),
                "win_prob": round(p, 3), "price": STD_PRICE,
                "ev": round(ev_at_price(p, STD_PRICE), 3),
                "kelly": round(KELLY_FRACTION * kelly(p, STD_PRICE), 4),
                "confidence": _confidence(edge, sd, gp),
            })

    # --- Moneyline ------------------------------------------------------
    hml = cons["best_home_ml"]; aml = cons["best_away_ml"]
    if hml is not None and aml is not None:
        fair_home, fair_away = devig_two_way(american_to_prob(hml), american_to_prob(aml))
        for side, model_p, fair_p, price, book, team in (
            ("home", proj["home_win_prob"], fair_home, hml, cons["best_home_ml_book"], home),
            ("away", proj["away_win_prob"], fair_away, aml, cons["best_away_ml_book"], away),
        ):
            if price > MAX_ML_PRICE:      # longshot: not a trustworthy edge
                continue
            ev = ev_at_price(model_p, price)
            prob_edge = model_p - fair_p
            if ev >= MIN_ML_EV and prob_edge > 0.02:
                # Confidence tracks the probability edge (not raw EV, which
                # longshot payouts inflate) and is tempered on thin samples and
                # on big underdogs where our estimate is least trustworthy.
                pe = prob_edge
                if gp < 4:
                    pe *= 0.6
                if price >= 250:            # +250 or longer: discount
                    pe *= 0.6
                conf = "high" if pe >= 0.06 else ("medium" if pe >= 0.035 else "low")
                out.append({
                    "market": "moneyline", "side": side, "team": team["abbr"],
                    "pick": f"{team['abbr']} ML {price:+d}",
                    "line": price, "book": book,
                    "edge": round(prob_edge * 100, 1),   # probability points
                    "model_prob": round(model_p, 3), "fair_prob": round(fair_p, 3),
                    "win_prob": round(model_p, 3), "price": price,
                    "ev": round(ev, 3),
                    "kelly": round(KELLY_FRACTION * kelly(model_p, price), 4),
                    "confidence": conf,
                })

    out.sort(key=lambda e: e["ev"], reverse=True)
    return out
