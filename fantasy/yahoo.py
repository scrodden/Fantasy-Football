"""Yahoo Fantasy Sports API integration (free, OAuth2).

Uses the out-of-band (oob) OAuth flow so no HTTPS redirect is needed on
localhost: the user authorizes on Yahoo, Yahoo shows a short code, the user
pastes it back, and we exchange it for tokens. Tokens are stored locally in
cache/yahoo_tokens.json; the app credentials in yahoo_config.json.

Only the Python standard library is used.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import util

CONFIG_FILE = os.path.join(util.BASE_DIR, "yahoo_config.json")
TOKENS_FILE = os.path.join(util.CACHE_DIR, "yahoo_tokens.json")
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
# Auto-capture redirect (must match the URI registered in the Yahoo app).
REDIRECT = f"https://localhost:{os.environ.get('FF_PORT', '8787')}/callback"


# ---------------------------------------------------------------------------
# Config + token storage
# ---------------------------------------------------------------------------
def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def get_config():
    return _read(CONFIG_FILE)


def save_config(client_id, client_secret):
    _write(CONFIG_FILE, {"client_id": client_id.strip(), "client_secret": client_secret.strip()})


def is_configured():
    c = get_config()
    return bool(c.get("client_id") and c.get("client_secret"))


def is_connected():
    t = _read(TOKENS_FILE)
    return bool(t.get("refresh_token"))


def auth_url():
    c = get_config()
    q = urllib.parse.urlencode({
        "client_id": c.get("client_id", ""),
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "language": "en-us",
    })
    return f"{AUTH_URL}?{q}"


# ---------------------------------------------------------------------------
# OAuth token exchange
# ---------------------------------------------------------------------------
def _post_token(params):
    c = get_config()
    # Yahoo accepts client credentials either via HTTP Basic auth or in the body;
    # include both for maximum compatibility.
    basic = base64.b64encode(f"{c['client_id']}:{c['client_secret']}".encode()).decode()
    body = dict(params)
    body.setdefault("client_id", c["client_id"])
    body.setdefault("client_secret", c["client_secret"])
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(f"Yahoo token error {exc.code}: {detail[:400]}")


def exchange_code(code):
    tok = _post_token({
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
        "code": code.strip(),
    })
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _write(TOKENS_FILE, tok)
    return tok


def _refresh():
    t = _read(TOKENS_FILE)
    if not t.get("refresh_token"):
        raise RuntimeError("Not connected to Yahoo.")
    tok = _post_token({
        "grant_type": "refresh_token",
        "redirect_uri": REDIRECT,
        "refresh_token": t["refresh_token"],
    })
    tok.setdefault("refresh_token", t["refresh_token"])
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _write(TOKENS_FILE, tok)
    return tok


def _access_token():
    t = _read(TOKENS_FILE)
    if not t.get("access_token"):
        raise RuntimeError("Not connected to Yahoo.")
    if time.time() >= t.get("expires_at", 0):
        t = _refresh()
    return t["access_token"]


def disconnect():
    for p in (TOKENS_FILE,):
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# API calls (JSON), with one auto-refresh on 401
# ---------------------------------------------------------------------------
def _get(path):
    url = f"{API}/{path}{'&' if '?' in path else '?'}format=json"
    for attempt in range(2):
        token = _access_token()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                _refresh()
                continue
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError(f"Yahoo API {exc.code}: {detail or exc.reason}")
    return {}


# ---------------------------------------------------------------------------
# Yahoo JSON is deeply nested with numeric-keyed dicts; these walk it safely.
# ---------------------------------------------------------------------------
def _collect(node, key):
    """Recursively collect all values stored under `key` anywhere in the tree."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            else:
                found.extend(_collect(v, key))
    elif isinstance(node, list):
        for v in node:
            found.extend(_collect(v, key))
    return found


def _flatten_list_dict(node):
    """Yahoo often encodes a list of {field:val} fragments as
    {"0":{...},"1":{...},"count":N} or a list of such dicts. Merge into one dict."""
    out = {}
    items = []
    if isinstance(node, list):
        items = node
    elif isinstance(node, dict):
        items = [v for k, v in node.items() if k != "count"]
    for it in items:
        if isinstance(it, dict):
            out.update(it)
        elif isinstance(it, list):
            for sub in it:
                if isinstance(sub, dict):
                    out.update(sub)
    return out


# ---------------------------------------------------------------------------
# High-level: leagues, settings, rosters
# ---------------------------------------------------------------------------
def get_leagues():
    """Return the user's current-season NFL leagues: [{league_key,name,num_teams}]."""
    data = _get("users;use_login=1/games;game_keys=nfl/leagues")
    out = []
    for lg in _collect(data, "league"):
        merged = _flatten_list_dict(lg) if not isinstance(lg, dict) or "league_key" not in lg else lg
        key = merged.get("league_key")
        if key:
            out.append({
                "league_key": key,
                "name": merged.get("name", key),
                "num_teams": merged.get("num_teams"),
                "scoring_type": merged.get("scoring_type"),
                "season": merged.get("season"),
            })
    # de-dupe by key
    seen, uniq = set(), []
    for lg in out:
        if lg["league_key"] not in seen:
            seen.add(lg["league_key"]); uniq.append(lg)
    return uniq


# Yahoo NFL stat display-name keywords -> our scoring key. Yardage keys hold
# points-per-yard (we invert to yards-per-point).
def _map_scoring(stat_name_values):
    s = {
        "pass_yds_per_pt": 25, "pass_td": 4, "pass_int": -2, "pass_2pt": 2,
        "rush_yds_per_pt": 10, "rush_td": 6, "rush_2pt": 2,
        "ppr": 0, "rec_yds_per_pt": 10, "rec_td": 6, "rec_2pt": 2,
        "fum_lost": -2, "misc_td": 6,
        "pat": 1, "fg_0_39": 3, "fg_40_49": 4, "fg_50p": 5, "fg_miss": 0,
        "d_sack": 1, "d_int": 2, "d_fum_rec": 2, "d_td": 6, "d_safe": 2, "d_block": 2, "d_2pt": 2,
    }
    fg_missed = []
    for name, val in stat_name_values:
        n = name.lower()
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if "passing yards" in n: s["pass_yds_per_pt"] = round(1 / v) if v else 25
        elif "passing touchdown" in n: s["pass_td"] = v
        elif "interception" in n and "return" not in n and "def" not in n: s["pass_int"] = v
        elif "rushing yards" in n: s["rush_yds_per_pt"] = round(1 / v) if v else 10
        elif "rushing touchdown" in n: s["rush_td"] = v
        elif n.strip() in ("receptions", "reception"): s["ppr"] = v
        elif "reception yards" in n or "receiving yards" in n: s["rec_yds_per_pt"] = round(1 / v) if v else 10
        elif "reception touchdown" in n or "receiving touchdown" in n: s["rec_td"] = v
        elif "return touchdown" in n: s["misc_td"] = v
        elif "2-point" in n or "two point" in n: s["pass_2pt"] = s["rush_2pt"] = s["rec_2pt"] = v
        elif "fumbles lost" in n or "fumble lost" in n: s["fum_lost"] = v
        elif "field goals 0-19" in n or "field goals 20-29" in n or "field goals 30-39" in n: s["fg_0_39"] = v
        elif "field goals 40-49" in n: s["fg_40_49"] = v
        elif "field goals 50" in n: s["fg_50p"] = v
        elif "field goals missed" in n: fg_missed.append(v)
        elif "point after" in n and "miss" not in n: s["pat"] = v
        elif "sack" in n: s["d_sack"] = v
        elif ("interception" in n and ("def" in n or "team" in n)): s["d_int"] = v
        elif "fumble recovery" in n: s["d_fum_rec"] = v
        elif n.strip() == "touchdown" or "defensive touchdown" in n: s["d_td"] = v
        elif "safety" in n or "safeties" in n: s["d_safe"] = v
        elif "block" in n: s["d_block"] = v
    if fg_missed:
        s["fg_miss"] = round(sum(fg_missed) / len(fg_missed), 1)
    return s


def get_league_settings(league_key):
    """Return {name, teams, scoring(dict), roster{start,flex,bench}} for a league."""
    data = _get(f"league/{league_key}/settings")
    # league meta
    metas = _collect(data, "name")
    name = metas[0] if metas else league_key
    teams = None
    for nt in _collect(data, "num_teams"):
        teams = nt; break

    # scoring: stat_categories gives names; stat_modifiers gives values, both by stat_id
    id_name, id_val = {}, {}
    for stat in _collect(data, "stat"):
        st = stat if isinstance(stat, dict) else _flatten_list_dict(stat)
        sid = st.get("stat_id")
        if sid is None:
            continue
        if "name" in st:
            id_name[str(sid)] = st.get("name")
        if "value" in st:
            id_val[str(sid)] = st.get("value")
    name_values = [(id_name.get(sid, ""), val) for sid, val in id_val.items()]
    scoring = _map_scoring(name_values)

    # roster positions
    start = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "K": 0, "DEF": 0}
    flex = 0
    bench = 0
    for rp in _collect(data, "roster_position"):
        r = rp if isinstance(rp, dict) else _flatten_list_dict(rp)
        pos = r.get("position")
        try:
            cnt = int(r.get("count", 1))
        except (TypeError, ValueError):
            cnt = 1
        if pos in start:
            start[pos] += cnt
        elif pos in ("W/R/T", "W/R", "R/W/T", "FLEX", "Q/W/R/T"):
            flex += cnt
        elif pos == "BN":
            bench += cnt
        # IR and others ignored for drafting
    is_auction = False
    for v in _collect(data, "is_auction_draft"):
        if str(v) == "1":
            is_auction = True
    for v in _collect(data, "draft_type"):
        if str(v).lower() == "auction":
            is_auction = True
    return {
        "name": name, "teams": teams, "budget": 200,
        "draft": "auction" if is_auction else "snake",
        "scoring": scoring,
        "roster": {"start": start, "flex": flex, "flex_pos": ["RB", "WR", "TE"], "bench": bench},
    }


def get_user_team(league_key):
    """Return {team_key, name} for the logged-in user's team in a league."""
    data = _get(f"league/{league_key}/teams;use_login=1")
    for tm in _collect(data, "team"):
        merged = _flatten_list_dict(tm) if not (isinstance(tm, dict) and "team_key" in tm) else tm
        if merged.get("team_key"):
            return {"team_key": merged.get("team_key"), "name": merged.get("name")}
    return None


def get_draft_results(league_key):
    """Live/complete draft picks: [{pick, cost, name, team, mine}].
    Poll this during a live Yahoo draft to auto-sync the Draft Room."""
    data = _get(f"league/{league_key}/draftresults")
    raw, keys = [], []
    for dr in _collect(data, "draft_result"):
        m = dr if (isinstance(dr, dict) and "player_key" in dr) else _flatten_list_dict(dr)
        pk = m.get("player_key")
        raw.append({"pick": m.get("pick"), "cost": m.get("cost"), "player_key": pk, "team_key": m.get("team_key")})
        if pk:
            keys.append(pk)
    names = {}
    for i in range(0, len(keys), 25):
        chunk = ",".join(keys[i:i + 25])
        pdata = _get(f"league/{league_key}/players;player_keys={chunk}")
        for pl in _collect(pdata, "player"):
            pm = pl if (isinstance(pl, dict) and "player_key" in pl) else _flatten_list_dict(pl)
            nm = pm.get("name")
            if isinstance(nm, dict):
                nm = nm.get("full")
            if pm.get("player_key") and nm:
                names[pm["player_key"]] = {"name": nm, "team": (pm.get("editorial_team_abbr") or "").upper()}
    team = get_user_team(league_key)
    my = team.get("team_key") if team else None
    out = []
    for r in raw:
        info = names.get(r["player_key"], {})
        out.append({"pick": r["pick"], "cost": r["cost"], "name": info.get("name"),
                    "team": info.get("team"), "mine": r["team_key"] == my})
    return out


def get_free_agents(league_key, count=150, position=None):
    """Available players (free agents + waivers) for waiver scouring.
    Returns [{name, pos, team, pct_owned}]."""
    out = []
    posparam = f";position={position}" if position else ""
    for start in range(0, count, 25):
        data = _get(f"league/{league_key}/players;status=A{posparam};sort=OR;start={start};count=25;out=percent_owned")
        players = _collect(data, "player")
        got = 0
        for pl in players:
            merged = _flatten_list_dict(pl) if not (isinstance(pl, dict) and "player_key" in pl) else pl
            nm = merged.get("name")
            if isinstance(nm, dict):
                nm = nm.get("full")
            if not nm:
                continue
            po = merged.get("percent_owned")
            po = _flatten_list_dict(po) if po is not None else {}
            out.append({
                "name": nm,
                "pos": merged.get("display_position") or merged.get("primary_position"),
                "team": (merged.get("editorial_team_abbr") or "").upper(),
                "pct_owned": po.get("value") if isinstance(po, dict) else None,
            })
            got += 1
        if got == 0:
            break
    return out


def get_transactions(league_key, count=25):
    """Recent league add/drop/trade transactions."""
    data = _get(f"league/{league_key}/transactions;count={count}")
    out = []
    for tx in _collect(data, "transaction"):
        merged = _flatten_list_dict(tx) if not (isinstance(tx, dict) and "transaction_key" in tx) else tx
        players = []
        for pl in _collect(tx, "player"):
            pm = _flatten_list_dict(pl) if not (isinstance(pl, dict) and "player_key" in pl) else pl
            nm = pm.get("name")
            if isinstance(nm, dict):
                nm = nm.get("full")
            td = pm.get("transaction_data")
            td = _flatten_list_dict(td) if td is not None else {}
            if nm:
                players.append({"name": nm, "move": td.get("type"),
                                "source": td.get("source_type"), "dest": td.get("destination_type")})
        out.append({
            "type": merged.get("type"), "status": merged.get("status"),
            "timestamp": merged.get("timestamp"), "players": players,
        })
    return out


def get_draft_analysis(league_key, max_players=400):
    """Pull Yahoo's pre-draft analysis per player for a league.

    Returns [{name, average_cost, average_pick, average_round, percent_drafted}].
    `average_cost` is the auction AAV (populated for auction leagues).
    """
    out = []
    for start in range(0, max_players, 25):
        data = _get(f"league/{league_key}/players;start={start};count=25;out=draft_analysis")
        players = _collect(data, "player")
        got = 0
        for pl in players:
            merged = _flatten_list_dict(pl) if not (isinstance(pl, dict) and "player_key" in pl) else pl
            nm = merged.get("name")
            if isinstance(nm, dict):
                nm = nm.get("full")
            if not nm:
                continue
            da = merged.get("draft_analysis")
            da = _flatten_list_dict(da) if da is not None else {}
            out.append({
                "name": nm,
                "average_cost": da.get("average_cost"),
                "average_pick": da.get("average_pick"),
                "average_round": da.get("average_round"),
                "percent_drafted": da.get("percent_drafted"),
            })
            got += 1
        if got == 0:
            break
    return out


def get_team_roster(team_key):
    """Return the roster as [{name, pos, team}]."""
    data = _get(f"team/{team_key}/roster")
    out = []
    for pl in _collect(data, "player"):
        merged = _flatten_list_dict(pl) if not (isinstance(pl, dict) and "player_key" in pl) else pl
        nm = merged.get("name")
        if isinstance(nm, dict):
            nm = nm.get("full")
        pos = merged.get("display_position") or merged.get("primary_position")
        team = merged.get("editorial_team_abbr")
        if nm:
            out.append({"name": nm, "pos": pos, "team": (team or "").upper()})
    return out
