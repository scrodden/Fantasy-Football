"""Auto-calibration: fit the model's parameters to history instead of guessing.

Coordinate descent over the calibratable parameters, minimizing the backtest
objective (margin MAE + Brier) on a training span, then validating on a
held-out season so we can see whether the tuning generalized or just overfit.

The winning parameters are written back into the live model's saved state, so
the app immediately projects with calibrated numbers.
"""

from __future__ import annotations

import copy

from betting import backtest
from betting.model import Projector, PACE_DEFAULTS
from betting.elo import DEFAULTS as ELO_DEFAULTS

# What to search, and the candidate grid for each (coordinate descent).
SEARCH = {
    "nfl": [
        ("elo", "k", [14, 18, 20, 24, 28]),
        ("elo", "hfa", [15, 30, 40, 48, 60, 75]),
        ("elo", "points_per_elo", [22, 25, 28, 31]),
        ("elo", "revert", [0.20, 0.33, 0.45, 0.60]),
        ("elo", "score_sd", [11.5, 12.5, 13.2, 14.0]),
        ("pace", "alpha", [0.06, 0.10, 0.14, 0.18]),
        ("pace", "home_pts", [0.5, 1.0, 1.5, 2.0]),
        ("pace", "form_w", [0.0, 0.15, 0.30, 0.45]),
    ],
    "cfb": [
        ("elo", "k", [30, 42, 52, 62]),
        ("elo", "hfa", [40, 55, 65, 80]),
        ("elo", "points_per_elo", [24, 27, 30, 33]),
        ("elo", "revert", [0.20, 0.30, 0.45, 0.60]),
        ("elo", "score_sd", [15, 16.5, 18, 20]),
        ("pace", "alpha", [0.08, 0.14, 0.20]),
        ("pace", "home_pts", [1.0, 1.4, 2.0]),
        ("pace", "form_w", [0.0, 0.22, 0.40]),
    ],
}


def _defaults(league: str) -> dict:
    e, p = ELO_DEFAULTS[league], PACE_DEFAULTS[league]
    return {"elo": {"k": e["k"], "hfa": e["hfa"],
                    "points_per_elo": e["points_per_elo"], "revert": e["revert"],
                    "score_sd": e["score_sd"]},
            "pace": {"alpha": p["alpha"], "home_pts": p["home_pts"],
                     "form_w": p["form_w"]}}


def _eval(league, seasons, test_from, overrides):
    m = backtest.run(league, seasons, test_from=test_from, overrides=overrides)
    return backtest.objective(m), m


def calibrate(league: str, seasons: list[int], *, passes: int = 2,
              holdout: int | None = None) -> dict:
    """Search for the best parameters. ``holdout`` (default: last season) is
    excluded from tuning and reported separately as an out-of-sample check."""
    seasons = sorted(seasons)
    if holdout is None:
        holdout = seasons[-1]
    train = [s for s in seasons if s != holdout]
    test_from = train[1] if len(train) > 1 else train[0]

    cur = _defaults(league)
    base_obj, base_m = _eval(league, train, test_from, cur)
    best_obj = base_obj
    trace = [{"step": "baseline", "objective": round(base_obj, 4),
              "margin_mae": base_m["margin_mae"], "brier": base_m["brier"]}]

    for _ in range(passes):
        for section, key, grid in SEARCH[league]:
            best_val = cur[section][key]
            for val in grid:
                trial = copy.deepcopy(cur)
                trial[section][key] = val
                obj, _m = _eval(league, train, test_from, trial)
                if obj < best_obj - 1e-6:
                    best_obj, best_val = obj, val
            cur[section][key] = best_val
        trace.append({"step": "pass", "objective": round(best_obj, 4)})

    # Final metrics on train and on the untouched holdout season.
    _, train_m = _eval(league, train, test_from, cur)
    holdout_m = backtest.run(league, seasons, test_from=holdout, overrides=cur)

    return {
        "league": league, "seasons": seasons, "holdout": holdout,
        "params": cur,
        "baseline": {"margin_mae": base_m["margin_mae"], "brier": base_m["brier"],
                     "objective": round(base_obj, 4)},
        "tuned_train": {"margin_mae": train_m["margin_mae"], "brier": train_m["brier"],
                        "objective": round(best_obj, 4)},
        "tuned_holdout": {"margin_mae": holdout_m["margin_mae"],
                          "brier": holdout_m["brier"], "su_pct": holdout_m["su_pct"],
                          "spread_all_roi": holdout_m["spread_all"]["roi"],
                          "value_ats": holdout_m["value"]["ats_pct"]},
        "trace": trace,
    }


# Signals the backtest validated as helpful (see docs). QB and recent-form
# were measured to *hurt*, so they stay off until we have real passing-EPA data.
GOOD_SIGNALS = {"weather": True, "travel": True, "qb": False, "epa": True}


def apply_to_live(league: str, params: dict, signals: dict | None = None) -> dict:
    """Write calibrated parameters + validated signal flags into live state."""
    proj = Projector(league)          # loads current (seeded) state
    for k, v in params.get("elo", {}).items():
        setattr(proj.elo, k, v)
    for k, v in params.get("pace", {}).items():
        setattr(proj.pace, k, v)
    proj.elo.meta["signals"] = signals if signals is not None else dict(GOOD_SIGNALS)
    proj.elo.meta["calibrated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    proj.save()
    try:
        from betting import train
        train._board_cache_clear(league)
    except Exception:
        pass
    return {"ok": True, "league": league, "applied": params,
            "signals": proj.elo.meta["signals"]}
