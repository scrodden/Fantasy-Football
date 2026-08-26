"""Walk-forward backtesting: does the model actually beat the closing line?

Replays whole seasons chronologically with a *fresh* model that only ever sees
games already played, records each pre-game prediction, then grades it against
the real closing line and result.  This is the yardstick every other
improvement is measured against -- and the loss function the calibrator
minimizes.

Key honesty rule: predictions are the **pure** model (``anchor=False``).  We
only have the closing line to grade against, so anchoring the projection toward
that same line would be circular -- instead we ask the hard question directly:
would the model's own number have beaten the close?
"""

from __future__ import annotations

import math

from betting import history, edges
from betting.model import Projector
from betting.strategies import _grade_spread, _grade_ml, _profit, STAKE


def _pgame(rec: dict) -> dict:
    """Adapt a history record into the dict the Projector/edges expect."""
    c = rec.get("closing") or {}
    return {
        "id": rec["id"], "date": rec["date"], "neutral": rec.get("neutral", False),
        "home": {**rec["home"], "score": rec["home_score"], "_gp": 99},
        "away": {**rec["away"], "score": rec["away_score"], "_gp": 99},
        "espn_odds": c,
        "books": [c] if c.get("home_spread") is not None else [],
        "weather": rec.get("weather") or {},
        "qb": rec.get("qb") or {},
        "rest": rec.get("rest") or {},
    }


def _blank():
    return {"n": 0, "win": 0, "loss": 0, "push": 0, "staked": 0.0, "profit": 0.0}


def _finalize(b):
    dec = b["win"] + b["loss"]
    b["ats_pct"] = round(100 * b["win"] / dec, 1) if dec else None
    b["roi"] = round(100 * b["profit"] / b["staked"], 1) if b["staked"] else None
    b["profit"] = round(b["profit"], 2)
    b["record"] = f"{b['win']}-{b['loss']}" + (f"-{b['push']}" if b["push"] else "")
    return b


def run(league: str, seasons: list[int], *, test_from: int | None = None,
        overrides: dict | None = None, warmup_games: int = 40,
        min_gp: int = 3) -> dict:
    """Backtest ``league`` over ``seasons`` (chronological).

    ``test_from``: only score games in this season and later (earlier seasons
    are warm-up so ratings aren't cold).  Defaults to the 2nd season given.
    ``min_gp``: don't score a game until both teams have this many games (skips
    the noisy cold-start each season).
    """
    seasons = sorted(seasons)
    if test_from is None:
        test_from = seasons[1] if len(seasons) > 1 else seasons[0]

    proj = Projector.fresh(league, overrides)
    games = history.games(league, seasons)

    # metrics
    n = su_ok = su_tot = 0
    abs_margin = abs_total = brier = 0.0
    total_n = 0
    spread_all = _blank()      # bet model's ATS pick on every game
    ml_all = _blank()          # bet model's SU winner every game
    value = _blank()           # only model-flagged edges (spread/total/ml)
    cal_bins = {i: [0, 0] for i in range(10)}   # win-prob calibration (deciles)
    line_mae = 0.0; line_n = 0
    cur_season = None
    seen = 0

    for rec in games:
        if rec["season"] != cur_season:
            if cur_season is not None:
                proj.new_season(rec["season"])
            cur_season = rec["season"]

        pg = _pgame(rec)
        hs, as_ = rec["home_score"], rec["away_score"]
        margin = hs - as_
        c = rec.get("closing") or {}
        has_line = c.get("home_spread") is not None

        gp = min((proj.elo.teams.get(str(rec["home"]["id"]), {}) or {}).get("gp", 0),
                 (proj.elo.teams.get(str(rec["away"]["id"]), {}) or {}).get("gp", 0))
        scoreable = rec["season"] >= test_from and seen >= warmup_games and gp >= min_gp

        if scoreable:
            p = proj.project(pg, anchor=False)
            pm = p["proj_margin"]
            # straight-up + errors
            if margin != 0:
                su_tot += 1
                su_ok += 1 if (pm > 0) == (margin > 0) else 0
            abs_margin += abs(pm - margin)
            n += 1
            if p.get("proj_total") is not None:
                abs_total += abs(p["proj_total"] - (hs + as_)); total_n += 1
            # win-prob calibration + brier
            wp = p["home_win_prob"]
            hw = 1.0 if margin > 0 else (0.0 if margin < 0 else 0.5)
            brier += (wp - hw) ** 2
            b = min(9, int(wp * 10)); cal_bins[b][0] += hw; cal_bins[b][1] += 1

            if has_line:
                line = c["home_spread"]
                line_mae += abs(pm - (-line)); line_n += 1
                # spread: every game, model's ATS side
                lean = pm + line
                if abs(lean) > 1e-6:
                    side = "home" if lean > 0 else "away"
                    num = line if side == "home" else -line
                    price = (c.get("home_spread_odds") if side == "home" else c.get("away_spread_odds")) or -110
                    res, pnl = _grade_spread(side, num, price, hs, as_)
                    _tally(spread_all, res, pnl)
                # moneyline: every game, model's winner
                if pm != 0:
                    side = "home" if pm > 0 else "away"
                    price = c.get(f"{side}_ml")
                    if price is not None:
                        res, pnl = _grade_ml(side, price, hs, as_)
                        _tally(ml_all, res, pnl)
                # value: model-flagged edges vs the closing line
                pe = dict(p); pe["_spread_sd"] = proj.elo.score_sd
                for e in edges.evaluate(pg, pe, league):
                    if e["market"] == "spread":
                        res, pnl = _grade_spread(e["side"], e["line"], e.get("price") or -110, hs, as_)
                    elif e["market"] == "moneyline":
                        res, pnl = _grade_ml(e["side"], e["price"], hs, as_)
                    else:  # total
                        res, pnl = _grade_total(e["side"], e["line"], hs, as_)
                    _tally(value, res, pnl)

        proj.process_game(pg)
        seen += 1

    # calibration table
    calibration = []
    for i in range(10):
        won, tot = cal_bins[i]
        if tot:
            calibration.append({"bucket": f"{i*10}-{i*10+10}%",
                                "predicted": i * 10 + 5, "actual": round(100 * won / tot, 1),
                                "n": tot})

    return {
        "league": league, "seasons": seasons, "test_from": test_from,
        "n_games_scored": n,
        "su_pct": round(100 * su_ok / su_tot, 1) if su_tot else None,
        "margin_mae": round(abs_margin / n, 2) if n else None,
        "total_mae": round(abs_total / total_n, 2) if total_n else None,
        "brier": round(brier / n, 4) if n else None,
        "vs_close_mae": round(line_mae / line_n, 2) if line_n else None,
        "spread_all": _finalize(spread_all),
        "ml_all": _finalize(ml_all),
        "value": _finalize(value),
        "calibration": calibration,
    }


def _grade_total(side: str, line: float, hs: int, as_: int):
    total = hs + as_
    if abs(total - line) < 1e-9:
        return "push", 0.0
    win = (total > line) == (side == "over")
    return ("win", _profit(STAKE, -110)) if win else ("loss", -STAKE)


def _tally(bucket, res, pnl):
    if res in ("win", "loss", "push"):
        bucket[res] += 1
        if res != "push":
            bucket["staked"] += STAKE
        bucket["profit"] += pnl


def epa_ablation(seasons: list[int], weights=(0.0, 0.15, 0.25, 0.35, 0.5, 0.65, 0.8, 1.0),
                 *, test_from: int | None = None, warmup_games: int = 40,
                 min_gp: int = 3, epa_alpha=0.18, epa_hfa=1.6, epa_plays=63.0) -> dict:
    """Does blending EPA ratings with Elo lower margin error, and at what weight?

    Replays NFL history feeding both an Elo Projector and an ``EPAModel``, then
    scores the blended margin ``(1-w)*elo + w*epa`` at each weight.  NFL only
    (EPA needs play-by-play)."""
    from betting import epa as _epa
    seasons = sorted(seasons)
    if test_from is None:
        test_from = seasons[1] if len(seasons) > 1 else seasons[0]

    agg = {}
    for yr in seasons:
        agg.update(_epa.team_game_epa(yr))

    proj = Projector.fresh("nfl", {"pace": {"form_w": 0.0}})
    epaM = _epa.EPAModel(alpha=epa_alpha, hfa=epa_hfa, plays=epa_plays)
    games = history.nfl_games(seasons)

    err = {w: 0.0 for w in weights}
    n = 0; cur = None; seen = 0
    for rec in games:
        if rec["season"] != cur:
            if cur is not None:
                proj.new_season(rec["season"])
                epaM.new_season()
            cur = rec["season"]
        pg = _pgame(rec)
        margin = rec["home_score"] - rec["away_score"]
        gp = min((proj.elo.teams.get(str(rec["home"]["id"]), {}) or {}).get("gp", 0),
                 (proj.elo.teams.get(str(rec["away"]["id"]), {}) or {}).get("gp", 0))
        if rec["season"] >= test_from and seen >= warmup_games and gp >= min_gp:
            elo_m = proj.project(pg, anchor=False)["model_margin"]
            epa_m = epaM.margin(rec["home"]["abbr"], rec["away"]["abbr"], rec.get("neutral", False))
            for w in weights:
                blended = (1 - w) * elo_m + w * epa_m
                err[w] += abs(blended - margin)
            n += 1
        proj.process_game(pg)
        r = agg.get(rec["id"])
        if r:
            epaM.update(r)
        seen += 1

    maes = {w: round(err[w] / n, 3) for w in weights} if n else {}
    best_w = min(maes, key=maes.get) if maes else None
    return {"n": n, "mae_by_weight": maes, "best_weight": best_w,
            "elo_only_mae": maes.get(0.0), "best_mae": maes.get(best_w)}


def qb_ablation(seasons: list[int], *, test_from: int | None = None,
                warmup_games: int = 40, min_gp: int = 3, weights=(0.0, 0.5, 1.0)) -> dict:
    """Does the passing-EPA QB adjustment lower margin error? Uses the real
    starter from nflverse (games.csv) and each QB's rolling passing EPA (PBP).
    ``weights`` scales the adjustment (0 = off, 1 = full)."""
    from betting import epa as _epa
    seasons = sorted(seasons)
    if test_from is None:
        test_from = seasons[1] if len(seasons) > 1 else seasons[0]
    starters = {}
    for yr in seasons:
        starters.update(_epa.qb_game_starters(yr))

    proj = Projector.fresh("nfl", {"pace": {"form_w": 0.0}})
    qb = _epa.QBModel()
    games = history.nfl_games(seasons)
    err = {w: 0.0 for w in weights}
    n = 0; cur = None; seen = 0; fired = 0
    for rec in games:
        if rec["season"] != cur:
            cur = rec["season"] if cur is None else (proj.new_season(rec["season"]) or rec["season"])
        pg = _pgame(rec)
        margin = rec["home_score"] - rec["away_score"]
        gp = min((proj.elo.teams.get(str(rec["home"]["id"]), {}) or {}).get("gp", 0),
                 (proj.elo.teams.get(str(rec["away"]["id"]), {}) or {}).get("gp", 0))
        if rec["season"] >= test_from and seen >= warmup_games and gp >= min_gp:
            elo_m = proj.project(pg, anchor=False)["model_margin"]
            qb_adj = qb.adjustment(rec["home"]["abbr"], rec["away"]["abbr"],
                                   (rec.get("qb") or {}).get("home_id"),
                                   (rec.get("qb") or {}).get("away_id"))
            if qb_adj:
                fired += 1
            for w in weights:
                err[w] += abs((elo_m + w * qb_adj) - margin)
            n += 1
        proj.process_game(pg)
        s = starters.get(rec["id"])
        if s:
            qb.update(s)
        seen += 1
    maes = {w: round(err[w] / n, 3) for w in weights} if n else {}
    best = min(maes, key=maes.get) if maes else None
    return {"n": n, "adjustments_fired": fired, "mae_by_weight": maes,
            "best_weight": best, "off_mae": maes.get(0.0), "best_mae": maes.get(best)}


def cfb_epa_ablation(seasons: list[int], weights=(0.0, 0.15, 0.25, 0.35, 0.5, 0.75, 1.0),
                     *, test_from: int | None = None, warmup_games: int = 80,
                     min_gp: int = 3, epa_plays=70.0, epa_hfa=2.4, epa_alpha=0.12) -> dict:
    """Does blending CFBD PPA ratings with Elo lower CFB margin error? Joins by
    ESPN game id; EPA ratings are keyed by CFBD team name."""
    from betting import epa as _epa, cfbd as _cfbd
    if not _cfbd.enabled():
        return {"error": "no CFBD key"}
    seasons = sorted(seasons)
    if test_from is None:
        test_from = seasons[1] if len(seasons) > 1 else seasons[0]
    agg = {}
    for yr in seasons:
        agg.update(_cfbd.team_game_epa(yr))

    proj = Projector.fresh("cfb", {"pace": {"form_w": 0.22}})
    epaM = _epa.EPAModel(alpha=epa_alpha, hfa=epa_hfa, plays=epa_plays)
    games = history.cfb_games(seasons)
    err = {w: 0.0 for w in weights}
    n = 0; cur = None; seen = 0
    for rec in games:
        if rec["season"] != cur:
            if cur is not None:
                proj.new_season(rec["season"]); epaM.new_season()
            cur = rec["season"]
        pg = _pgame(rec)
        margin = rec["home_score"] - rec["away_score"]
        gp = min((proj.elo.teams.get(str(rec["home"]["id"]), {}) or {}).get("gp", 0),
                 (proj.elo.teams.get(str(rec["away"]["id"]), {}) or {}).get("gp", 0))
        ep = agg.get(str(rec["id"]))
        if rec["season"] >= test_from and seen >= warmup_games and gp >= min_gp and ep:
            elo_m = proj.project(pg, anchor=False)["model_margin"]
            epa_m = epaM.margin(ep["home"], ep["away"], rec.get("neutral", False))
            for w in weights:
                err[w] += abs((1 - w) * elo_m + w * epa_m - margin)
            n += 1
        proj.process_game(pg)
        if ep:
            epaM.update(ep)
        seen += 1
    maes = {w: round(err[w] / n, 3) for w in weights} if n else {}
    best = min(maes, key=maes.get) if maes else None
    return {"n": n, "mae_by_weight": maes, "best_weight": best,
            "elo_only_mae": maes.get(0.0), "best_mae": maes.get(best)}


def _apply_cfb_priors(proj, year, priors, id_to_name, rmean, rsd, ret_slope, rec_coef):
    """Prior-aware season reversion: teams returning more production keep more of
    last year's rating; recruiting nudges the baseline."""
    mean = proj.elo.mean
    revert = proj.elo.revert
    for tid, node in proj.elo.teams.items():
        name = id_to_name.get(tid)
        pri = priors.get(name) if name else None
        keep = 1 - revert
        bump = 0.0
        if pri:
            pct = pri.get("ret_pct")
            if pct is not None:
                keep = min(0.85, max(0.45, (1 - revert) + ret_slope * (pct - 0.55)))
            rpts = pri.get("recruit_points")
            if rpts is not None and rsd:
                bump = rec_coef * ((rpts - rmean) / rsd)
        node["rating"] = mean + keep * (node["rating"] - mean) + bump
        node["gp"] = 0
    proj.pace.new_season()
    proj.elo.meta["season"] = year


def cfb_priors_ablation(seasons: list[int], *, test_from: int | None = None,
                        early_gp: int = 4, ret_slope=1.0, rec_coef=8.0) -> dict:
    """Do CFBD preseason priors (returning production + recruiting) sharpen the
    early-season CFB projection? Compares flat mean-reversion vs prior-aware
    reversion, scoring only early games (both teams < ``early_gp`` games in)."""
    from betting import cfbd as _cfbd, epa as _epa
    if not _cfbd.enabled():
        return {"error": "no CFBD key"}
    seasons = sorted(seasons)
    if test_from is None:
        test_from = seasons[1] if len(seasons) > 1 else seasons[0]
    id_to_name = _epa.load("cfb").id_to_name or {}
    priors_by_year = {}
    rstats = {}
    for yr in seasons:
        pr = _cfbd.preseason(yr)
        priors_by_year[yr] = pr
        rp = [v["recruit_points"] for v in pr.values() if v.get("recruit_points")]
        import statistics
        rstats[yr] = (statistics.mean(rp) if rp else 0.0,
                      statistics.pstdev(rp) if len(rp) > 1 else 1.0)

    def _replay(use_priors):
        proj = Projector.fresh("cfb", {"pace": {"form_w": 0.22}})
        games = history.cfb_games(seasons, with_lines=False)
        err = 0.0; n = 0; cur = None
        for rec in games:
            if rec["season"] != cur:
                if cur is not None:
                    if use_priors:
                        rm, rs = rstats[rec["season"]]
                        _apply_cfb_priors(proj, rec["season"], priors_by_year[rec["season"]],
                                          id_to_name, rm, rs, ret_slope, rec_coef)
                    else:
                        proj.new_season(rec["season"])
                cur = rec["season"]
            pg = _pgame(rec)
            margin = rec["home_score"] - rec["away_score"]
            gp = min((proj.elo.teams.get(str(rec["home"]["id"]), {}) or {}).get("gp", 0),
                     (proj.elo.teams.get(str(rec["away"]["id"]), {}) or {}).get("gp", 0))
            if rec["season"] >= test_from and gp < early_gp:
                elo_m = proj.project(pg, anchor=False)["model_margin"]
                err += abs(elo_m - margin); n += 1
            proj.process_game(pg)
        return round(err / n, 3) if n else None, n

    base_mae, n = _replay(False)
    prior_mae, _ = _replay(True)
    return {"early_games_scored": n, "baseline_early_mae": base_mae,
            "priors_early_mae": prior_mae,
            "improvement": round((base_mae or 0) - (prior_mae or 0), 3)}


def objective(metrics: dict) -> float:
    """Scalar loss for the calibrator (lower is better): predictive accuracy
    first (margin MAE + Brier), which is robust; ATS ROI is too noisy to
    optimize directly and is reported, not chased."""
    mm = metrics.get("margin_mae")
    br = metrics.get("brier")
    if mm is None or br is None:
        return 1e9
    return mm + 12.0 * br
