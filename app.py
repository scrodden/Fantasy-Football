#!/usr/bin/env python3
"""Fantasy Football Draft & Start/Sit Assistant -- local web server.

Zero third-party dependencies: only the Python 3 standard library.

Run:      python app.py
Then open http://localhost:8787  (opens automatically).

Data comes from free, no-auth public APIs (Sleeper, ESPN) and is cached under
./cache so repeat launches are fast. Use the "Refresh data" button (or restart)
to pull the latest injuries, news, and projections.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fantasy import auction, model, sources, util, yahoo
from betting import data as bet_data, train as bet_train, strategies as bet_strat, survivor
from betting.model import Projector as BetProjector

HOST = os.environ.get("FF_HOST", "127.0.0.1")
PORT = int(os.environ.get("FF_PORT", "8787"))
CERT_FILE = os.path.join(util.BASE_DIR, "certs", "localhost-cert.pem")
KEY_FILE = os.path.join(util.BASE_DIR, "certs", "localhost-key.pem")
USE_HTTPS = (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)
             and os.environ.get("FF_FORCE_HTTP") != "1")


# ---------------------------------------------------------------------------
# API payload shaping
# ---------------------------------------------------------------------------
def _list_row(p: dict) -> dict:
    """Everything the browser needs to score & project this player, minus the
    long injury comment (that ships only with the detail view)."""
    return {
        "id": p["id"],
        "name": p["name"],
        "position": p["position"],
        "team": p["team"],
        "age": p["age"],
        "years_exp": p["years_exp"],
        "number": p["number"],
        "bye_week": p.get("bye_week"),
        "is_rookie": p.get("is_rookie", False),
        "college": p.get("college"),
        "oline": p.get("oline"),
        "workload": p.get("workload"),
        "matchup": p.get("matchup"),
        "consistency": p.get("consistency"),
        "usage": p.get("usage"),
        "adp": p.get("adp"),
        "trending": p.get("trending", 0),
        "injury": {
            "status": p["injury"]["status"],
            "body_part": p["injury"]["body_part"],
            "is_risk": p["injury"]["is_risk"],
        },
        "history": p["history"],
        "proj_week": p["proj_week"],
        "proj_season": p["proj_season"],
        "proj_season_espn": p.get("proj_season_espn") or {},
    }


def api_players() -> dict:
    data = model.build_dataset()
    return {
        "context": data["context"],
        "generated_at": data["generated_at"],
        "freshness": data["freshness"],
        "count": data["count"],
        "categories": model.CATEGORY_FIELDS,
        "players": [_list_row(p) for p in data["players"]],
    }


def api_player(pid: str):
    p = model.get_player(pid)
    if not p:
        return None
    detail = dict(p)
    detail["news"] = sources.news_for_player(p["name"], p["team"])
    detail["insider"] = sources.get_insider_feed(player=p["name"], team=p.get("team"))
    detail["research_links"] = sources.research_links(p["name"])
    detail["category_fields"] = model.CATEGORY_FIELDS
    detail["context"] = model.build_dataset()["context"]
    if p.get("is_rookie"):
        detail["college_stats"] = sources.get_college_stats(p["name"])
        detail["college_links"] = sources.college_links(p["name"], p.get("college"))
        detail["cfbd_enabled"] = sources.cfbd_enabled()
    detail["week_log"] = _player_week_log(p, detail["context"])
    return detail


def _player_week_log(p, ctx):
    """Actual week-by-week stat lines for the current season (completed weeks)."""
    if ctx.get("season_type") != "regular":
        return []
    try:
        sched = sources.get_schedule().get(str(ctx["season"]), {}).get(p["team"], {})
    except Exception:
        sched = {}
    out = []
    for w in range(1, ctx["upcoming_week"]):
        opp = sched.get(w) or sched.get(str(w))
        raw = sources.get_week_stats(ctx["season"], w).get(p["id"])
        if not raw:
            out.append({"week": w, "opp": opp, "cats": None, "played": False})
            continue
        gp = raw.get("gp") or raw.get("gms_active") or 0
        out.append({"week": w, "opp": opp, "cats": model._extract_cats(raw), "played": bool(gp)})
    return out


def api_team(abbr: str):
    abbr = abbr.upper()
    data = model.build_dataset()
    ctx = data["context"]
    prev = ctx["history_years"][-1]
    # defense profile from DvP (how tough this team's D is vs each position)
    try:
        dvp = sources.get_dvp(prev)
        _mult, rank = model._matchup_tables(dvp)
    except Exception:
        rank = {}
    def_profile = {}
    for pos in ("QB", "RB", "WR", "TE"):
        rk = rank.get(abbr, {}).get(pos)
        if rk:
            # rank 1 = toughest; present as "Nth toughest"
            def_profile[pos] = {"rank": rk["rank"], "of": rk["of"]}
    rec = sources.get_team_records(prev).get(abbr)
    pa_pg = round(rec["pa"] / rec["g"], 1) if rec and rec.get("g") else None
    detail = sources.get_team_detail(abbr)
    name = sources.TEAM_NAMES.get(abbr, abbr)
    news = sources.get_team_news(name + " NFL injury depth chart", limit=20)
    return {
        "abbr": abbr, "name": name,
        "record_prev": (f"{rec['w']}-{rec['l']}" + (f"-{rec['t']}" if rec.get('t') else "")) if rec else None,
        "record_prev_year": prev,
        "points_for_pg": round(rec["pf"] / rec["g"], 1) if rec and rec.get("g") else None,
        "points_against_pg": pa_pg,
        "coach": detail.get("coach"), "coach_exp": detail.get("coach_exp"),
        "division": detail.get("division") or sources.TEAM_DIVISION.get(abbr),
        "record_now": detail.get("record"),
        "defense": def_profile,
        "news": news,
        "insider": sources.get_insider_feed(team=abbr),
    }


def api_refresh() -> dict:
    try:
        yr = int(sources.get_state().get("season", 0))
    except Exception:
        yr = 0
    prefixes = ["state", "players", "espn_injuries", "espn_news", "proj_",
                "espn_proj", "trending_add"]
    if yr:
        prefixes.append(f"stats_{yr}")  # current-season stats (feeds the baseline)
    for prefix in prefixes:
        util.cache_clear(prefix)
    data = model.build_dataset(force=True)
    return {"ok": True, "count": data["count"], "freshness": data["freshness"],
            "generated_at": data["generated_at"]}


# ---------------------------------------------------------------------------
# Betting model API
# ---------------------------------------------------------------------------
def _bet_league(qs_or_req) -> str:
    lg = (qs_or_req.get("league") if isinstance(qs_or_req, dict)
          else qs_or_req).lower() if qs_or_req else "nfl"
    return lg if lg in bet_data.LEAGUES else "nfl"


def api_betting_board(league: str) -> dict:
    return bet_train.build_board(league)


def api_betting_accuracy(league: str) -> dict:
    return bet_train.accuracy(league)


def api_betting_rankings(league: str) -> dict:
    proj = BetProjector(league)
    return {"league": league, "label": bet_data.LEAGUE_LABEL[league],
            "as_of": proj.elo.meta.get("last_game_date"),
            "teams": proj.elo.rankings()}


def _bet_seasons() -> list:
    """The seasons we have history for (nflverse/ESPN cover 2021+)."""
    import datetime as _d
    y = _d.date.today().year
    start = 2021
    end = y if _d.date.today().month >= 9 else y - 1
    return list(range(start, end + 1))


def api_betting_backtest(league: str) -> dict:
    from betting import backtest
    seasons = _bet_seasons()
    proj = BetProjector(league)
    ov = {"elo": {"k": proj.elo.k, "hfa": proj.elo.hfa,
                  "points_per_elo": proj.elo.points_per_elo,
                  "revert": proj.elo.revert, "score_sd": proj.elo.score_sd},
          "pace": {"alpha": proj.pace.alpha, "home_pts": proj.pace.home_pts,
                   "form_w": proj.pace.form_w},
          "signals": proj.elo.meta.get("signals") or {}}
    r = backtest.run(league, seasons, overrides=ov)
    r["using_live_params"] = True
    r["signals"] = proj.elo.meta.get("signals") or {}
    r["calibrated_at"] = proj.elo.meta.get("calibrated_at")
    return r


def api_betting_rebuild(league: str) -> dict:
    """Full cold-start rebuild (rarely needed): seed from the last two completed
    seasons, calibrate, seed CFB preseason priors, and sync EPA. This DISCARDS
    any in-season learning, so it's an advanced/repair action only."""
    import datetime as _d
    from betting import calibrate
    upcoming = _d.date.today().year if _d.date.today().month >= 8 else _d.date.today().year - 1
    seed_years = [upcoming - 2, upcoming - 1]
    out = {"league": league, "seeded": bet_train.seed(league, seed_years)}
    rep = calibrate.calibrate(league, _bet_seasons())
    out["calibrated"] = calibrate.apply_to_live(league, rep["params"])
    out["calibration"] = {"baseline_mae": rep["baseline"]["margin_mae"],
                          "tuned_mae": rep["tuned_train"]["margin_mae"]}
    # CFB: apply preseason priors for the upcoming season.
    if league == "cfb":
        try:
            from betting import cfbd
            if cfbd.enabled():
                proj = BetProjector(league)
                out["priors"] = cfbd.apply_season_priors(proj, upcoming)
                proj.save()
        except Exception as exc:
            out["priors_error"] = str(exc)
    # Rebuild EPA ratings.
    try:
        from betting import epa as _epa, cfbd as _cfbd
        if league == "nfl":
            _epa.sync(); _epa.sync_qb()
        elif _cfbd.enabled():
            _epa.sync_cfb()
        out["epa_synced"] = True
    except Exception as exc:
        out["epa_error"] = str(exc)
    bet_train._board_cache_clear(league)
    return out


def api_betting_calibrate(league: str) -> dict:
    from betting import calibrate
    seasons = _bet_seasons()
    rep = calibrate.calibrate(league, seasons)
    applied = calibrate.apply_to_live(league, rep["params"])
    rep["applied"] = applied
    return rep


def api_betting_status() -> dict:
    """Whether each league's model has been seeded, plus Odds API config."""
    import os as _os
    from betting import cfbd as _cfbd
    out = {"odds_api_configured": bool(bet_data.odds_api_key()),
           "cfbd_configured": _cfbd.enabled(), "leagues": {}}
    for lg in bet_data.LEAGUES:
        proj = BetProjector(lg)
        out["leagues"][lg] = {
            "label": bet_data.LEAGUE_LABEL[lg],
            "seeded": proj.elo.meta.get("n_games", 0) > 0,
            "games_learned": proj.elo.meta.get("n_games", 0),
            "teams_rated": len(proj.elo.teams),
            "season": proj.elo.meta.get("season"),
        }
    return out


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FantasyFootball/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (self.path or ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers --
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_callback_page(self, ok, msg):
        color = "#3fb950" if ok else "#f85149"
        title = "✓ Connected" if ok else "⚠ Problem"
        safe = msg.replace("<", "&lt;").replace(">", "&gt;")
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Yahoo</title>"
            "<style>body{font-family:-apple-system,Segoe UI,sans-serif;background:#0e1116;"
            "color:#e6edf3;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            ".box{background:#161b22;border:1px solid #2a323d;border-radius:12px;padding:2rem 2.4rem;"
            "max-width:520px;text-align:center}h1{margin:.2rem 0}p{color:#c9d4df}"
            ".hint{color:#8b98a5;font-size:.9rem;margin-top:1rem}</style></head><body><div class='box'>"
            f"<h1 style='color:{color}'>{title}</h1><p>{safe}</p>"
            "<p class='hint'>Return to the Fantasy Football Assistant tab and click “Continue”.</p>"
            "</div></body></html>"
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _send_file(self, relpath, content_type):
        path = os.path.join(util.WEB_DIR, relpath)
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Always revalidate so a fresh app.js/betting.js/style.css is picked up
        # after an update instead of a stale browser-cached copy.
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # -- routing --
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._send_file("index.html", "text/html; charset=utf-8")
            if path == "/style.css":
                return self._send_file("style.css", "text/css; charset=utf-8")
            if path == "/app.js":
                return self._send_file("app.js", "application/javascript; charset=utf-8")
            if path == "/betting.js":
                return self._send_file("betting.js", "application/javascript; charset=utf-8")
            if path == "/survivor.js":
                return self._send_file("survivor.js", "application/javascript; charset=utf-8")
            if path == "/api/players":
                return self._send_json(api_players())
            if path == "/api/news":
                from urllib.parse import parse_qs, urlparse
                q = (parse_qs(urlparse(self.path).query).get("q", [None])[0] or "").strip()
                if q:
                    return self._send_json({"news": sources.get_team_news(q, limit=20)})
                return self._send_json({"news": sources.get_news()})
            if path.startswith("/api/player/"):
                pid = path.rsplit("/", 1)[-1]
                detail = api_player(pid)
                if detail is None:
                    return self._send_json({"error": "player not found"}, 404)
                return self._send_json(detail)
            if path.startswith("/api/team/"):
                return self._send_json(api_team(path.rsplit("/", 1)[-1]))
            if path == "/callback":
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                code = qs.get("code", [None])[0]
                err = qs.get("error", [None])[0]
                ok, msg = False, "No authorization code received."
                if err:
                    msg = "Yahoo returned an error: " + err
                elif code:
                    try:
                        yahoo.exchange_code(code)
                        ok, msg = True, "Connected to Yahoo. You can close this tab and return to the app."
                    except Exception as exc:
                        msg = "Token exchange failed: " + str(exc)
                return self._send_callback_page(ok, msg)
            if path == "/api/yahoo/authurl":
                return self._send_json({"auth_url": yahoo.auth_url()})
            if path.startswith("/api/betting/"):
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                league = _bet_league(qs.get("league", ["nfl"])[0])
                if path == "/api/betting/status":
                    return self._send_json(api_betting_status())
                if path == "/api/betting/board":
                    return self._send_json(api_betting_board(league))
                if path == "/api/betting/explain":
                    gid = qs.get("game_id", [""])[0]
                    return self._send_json(bet_train.explain_game(league, gid))
                if path == "/api/betting/accuracy":
                    return self._send_json(api_betting_accuracy(league))
                if path == "/api/betting/rankings":
                    return self._send_json(api_betting_rankings(league))
                if path == "/api/betting/strategies":
                    # Opportunistically lock/settle so the tracker is current.
                    try:
                        bet_strat.maintain(league)
                    except Exception as exc:
                        sys.stderr.write(f"[betting maintain] {exc}\n")
                    return self._send_json(bet_strat.report(league))
                if path == "/api/betting/backtest":
                    return self._send_json(api_betting_backtest(league))
            if path == "/api/yahoo/status":
                st = {"configured": yahoo.is_configured(), "connected": yahoo.is_connected()}
                if st["connected"]:
                    try:
                        st["leagues"] = yahoo.get_leagues()
                    except Exception as exc:
                        st["error"] = str(exc)
                return self._send_json(st)
            self.send_error(404, "Not found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # never crash the server on one bad request
            sys.stderr.write(f"[GET {path}] error: {exc}\n")
            try:
                self._send_json({"error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/refresh":
                return self._send_json(api_refresh())
            if path == "/api/optimize":
                req = self._read_json()
                league = req.get("league", "johnnyv")
                return self._send_json(auction.optimize_roster(league))
            if path == "/api/auction_values":
                req = self._read_json()
                return self._send_json(auction.league_values(req.get("league", "johnnyv")))
            if path == "/api/survivor":
                req = self._read_json()
                return self._send_json(survivor.build_survivor(
                    used=req.get("used") or [],
                    start_week=req.get("start_week"),
                    horizon=req.get("horizon")))
            if path == "/api/betting/seed":
                req = self._read_json()
                league = _bet_league(req)
                years = req.get("years") or [2024, 2025]
                return self._send_json(bet_train.seed(league, years))
            if path == "/api/betting/update":
                req = self._read_json()
                league = _bet_league(req)
                out = bet_train.update_from_results(league)
                out["strategies"] = bet_strat.maintain(league)
                return self._send_json(out)
            if path == "/api/betting/calibrate":
                req = self._read_json()
                league = _bet_league(req)
                return self._send_json(api_betting_calibrate(league))
            if path == "/api/betting/rebuild":
                req = self._read_json()
                league = _bet_league(req)
                return self._send_json(api_betting_rebuild(league))
            if path == "/api/betting/lock":
                req = self._read_json()
                league = _bet_league(req)
                return self._send_json(bet_strat.lock_due_bets(league))
            if path == "/api/betting/config":
                req = self._read_json()
                cfg = bet_data.load_config()
                key = (req.get("odds_api_key") or "").strip()
                if key:
                    cfg["odds_api_key"] = key
                elif "odds_api_key" in req:  # explicit clear
                    cfg.pop("odds_api_key", None)
                ckey = (req.get("cfbd_api_key") or "").strip()
                if ckey:
                    cfg["cfbd_api_key"] = ckey
                elif "cfbd_api_key" in req:
                    cfg.pop("cfbd_api_key", None)
                bet_data.save_config(cfg)
                from betting import cfbd as _cfbd
                return self._send_json({"ok": True,
                                        "odds_api_configured": bool(bet_data.odds_api_key()),
                                        "cfbd_configured": _cfbd.enabled()})
            if path == "/api/yahoo/config":
                req = self._read_json()
                yahoo.save_config(req.get("client_id", ""), req.get("client_secret", ""))
                return self._send_json({"ok": True, "auth_url": yahoo.auth_url()})
            if path == "/api/yahoo/connect":
                req = self._read_json()
                yahoo.exchange_code(req.get("code", ""))
                return self._send_json({"ok": True, "leagues": yahoo.get_leagues()})
            if path == "/api/yahoo/disconnect":
                yahoo.disconnect()
                return self._send_json({"ok": True})
            if path == "/api/yahoo/import":
                req = self._read_json()
                lk = req.get("league_key")
                settings = yahoo.get_league_settings(lk)
                team = yahoo.get_user_team(lk)
                roster = yahoo.get_team_roster(team["team_key"]) if team else []
                return self._send_json({"ok": True, "settings": settings,
                                        "team": team, "roster": roster})
            if path == "/api/roster_news":
                req = self._read_json()
                ids = (req.get("ids") or [])[:40]
                out = []
                for pid in ids:
                    p = model.get_player(pid)
                    if not p:
                        continue
                    out.append({
                        "id": pid, "name": p["name"], "position": p["position"], "team": p["team"],
                        "injury": {"status": p["injury"]["status"], "body_part": p["injury"]["body_part"],
                                   "detail": p["injury"]["detail"], "is_risk": p["injury"]["is_risk"]},
                        "news": sources.news_for_player(p["name"], p["team"]),
                    })
                return self._send_json({"ok": True, "players": out})
            if path == "/api/yahoo/roster":
                req = self._read_json()
                lk = req.get("league_key")
                team = yahoo.get_user_team(lk)
                roster = yahoo.get_team_roster(team["team_key"]) if team else []
                return self._send_json({"ok": True, "team": team, "roster": roster})
            if path == "/api/yahoo/freeagents":
                req = self._read_json()
                fa = yahoo.get_free_agents(req.get("league_key"), position=req.get("position"))
                return self._send_json({"ok": True, "players": fa})
            if path == "/api/yahoo/transactions":
                req = self._read_json()
                tx = yahoo.get_transactions(req.get("league_key"))
                return self._send_json({"ok": True, "transactions": tx})
            if path == "/api/yahoo/draftresults":
                req = self._read_json()
                picks = yahoo.get_draft_results(req.get("league_key"))
                return self._send_json({"ok": True, "picks": picks})
            if path == "/api/yahoo/draftvalues":
                import csv as _csv
                req = self._read_json()
                rows = yahoo.get_draft_analysis(req.get("league_key"))
                with_cost = 0
                with open(auction.YAHOO_AAV_FILE, "w", newline="", encoding="utf-8") as fh:
                    w = _csv.writer(fh)
                    w.writerow(["Player", "Avg $", "Avg Pick", "% Drafted"])
                    for r in rows:
                        cost = r.get("average_cost")
                        if cost not in (None, "", "0.00", "0"):
                            with_cost += 1
                        w.writerow([r["name"], cost or "", r.get("average_pick") or "",
                                    r.get("percent_drafted") or ""])
                return self._send_json({"ok": True, "count": len(rows), "with_cost": with_cost})
            self.send_error(404, "Not found")
        except Exception as exc:
            sys.stderr.write(f"[POST {path}] error: {exc}\n")
            self._send_json({"error": str(exc)}, 500)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def _warm():
    try:
        print("Loading NFL data (first launch downloads ~30 MB, ~10-30s)...")
        data = model.build_dataset(force=False)
        ctx = data["context"]
        print(
            f"Ready: {data['count']} eligible players | "
            f"season {ctx['season']} ({ctx['season_type']}), "
            f"history {ctx['history_years'][0]}-{ctx['history_years'][-1]}."
        )
    except Exception as exc:
        print(f"[warm] initial load failed (will retry on request): {exc}")


def _betting_autopilot():
    """While the app is up, keep the model and tracker current: ingest each
    week's finished games into the ratings (so the Betting AND Survivor tabs
    reflect the latest results without any manual step), lock each game's bets
    ~24h before kickoff, and settle finished games. Best-effort, quiet."""
    import time as _time
    while True:
        for lg in bet_data.LEAGUES:
            try:
                # 1. Learn from any newly-finished games (sharpens win probs).
                upd = bet_train.update_from_results(lg)
                if upd.get("new_finals_learned"):
                    sys.stderr.write(f"[betting {lg}] learned {upd['new_finals_learned']} new finals\n")
                # 2. Lock due bets + settle finished ones for the tracker.
                res = bet_strat.maintain(lg)
                locked = res["lock"].get("locked", 0)
                settled = res["settle"].get("settled", 0)
                if locked or settled:
                    sys.stderr.write(f"[betting {lg}] locked {locked}, settled {settled}\n")
            except Exception as exc:
                sys.stderr.write(f"[betting autopilot {lg}] {exc}\n")
        _time.sleep(1200)  # every 20 minutes


def main():
    threading.Thread(target=_warm, daemon=True).start()
    threading.Thread(target=_betting_autopilot, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    scheme = "http"
    if USE_HTTPS:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    host = "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST
    url = f"{scheme}://{host}:{PORT}"
    print("=" * 60)
    print("  Fantasy Football Assistant")
    print(f"  Serving at {url}")
    if USE_HTTPS:
        print("  (HTTPS via a local self-signed cert — your browser will show a")
        print("   one-time 'not private' warning; click Advanced -> Continue.)")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
