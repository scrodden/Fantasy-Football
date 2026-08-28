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
# The win-probability engine (Elo + schedule/odds) powers the Survivor planner.
# The betting/gambling surface has been removed from the app; this stays only as
# an internal model that Survivor consumes.
from betting import train as bet_train, survivor
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
        # Always revalidate so a fresh app.js/survivor.js/style.css is picked up
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


def _ratings_autopilot():
    """Keep the NFL win-probability model current for the Survivor planner:
    ingest each week's finished games into the ratings (on startup and every
    20 min) so the plan reflects the latest results with no manual step. If the
    model was never seeded, cold-start it from the last two seasons first.
    Best-effort, quiet."""
    import time as _time
    import datetime as _dt
    while True:
        try:
            proj = BetProjector("nfl")
            if proj.elo.meta.get("n_games", 0) == 0:
                yr = _dt.date.today().year
                base = yr if _dt.date.today().month >= 8 else yr - 1
                bet_train.seed("nfl", [base - 2, base - 1])
            upd = bet_train.update_from_results("nfl")
            if upd.get("new_finals_learned"):
                sys.stderr.write(f"[ratings] learned {upd['new_finals_learned']} new finals\n")
        except Exception as exc:
            sys.stderr.write(f"[ratings autopilot] {exc}\n")
        _time.sleep(1200)  # every 20 minutes


def main():
    threading.Thread(target=_warm, daemon=True).start()
    threading.Thread(target=_ratings_autopilot, daemon=True).start()
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
