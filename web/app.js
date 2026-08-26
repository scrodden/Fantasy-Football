"use strict";

// ===========================================================================
// Scoring settings (single source of truth; persisted to localStorage).
// Points are computed from RAW stat categories, so any league format works.
// ===========================================================================
const DEFAULTS = {
  pass_yds_per_pt: 25, pass_td: 4, pass_int: -2, pass_2pt: 2,
  rush_yds_per_pt: 10, rush_td: 6, rush_2pt: 2,
  ppr: 1, rec_yds_per_pt: 10, rec_td: 6, rec_2pt: 2,
  fum_lost: -2, misc_td: 6,
  pat: 1, fg_0_39: 3, fg_40_49: 4, fg_50p: 5, fg_miss: 0,
  d_sack: 1, d_int: 2, d_fum_rec: 2, d_td: 6, d_safe: 2, d_block: 2, d_2pt: 2,
};

// Editor layout: [key, label, step]
const SETTING_GROUPS = [
  ["Passing", [
    ["pass_yds_per_pt", "Yards per point", 1],
    ["pass_td", "TD", 1], ["pass_int", "Interception", 1], ["pass_2pt", "2-pt conv", 1],
  ]],
  ["Rushing", [
    ["rush_yds_per_pt", "Yards per point", 1],
    ["rush_td", "TD", 1], ["rush_2pt", "2-pt conv", 1],
  ]],
  ["Receiving", [
    ["ppr", "Points per reception", 0.5],
    ["rec_yds_per_pt", "Yards per point", 1],
    ["rec_td", "TD", 1], ["rec_2pt", "2-pt conv", 1],
  ]],
  ["Miscellaneous", [
    ["fum_lost", "Fumble lost", 1], ["misc_td", "Fumble / return TD", 1],
  ]],
  ["Kicking", [
    ["pat", "Extra point", 1],
    ["fg_0_39", "FG 0–39 yd", 1], ["fg_40_49", "FG 40–49 yd", 1], ["fg_50p", "FG 50+ yd", 1],
    ["fg_miss", "Missed FG", 1],
  ]],
  ["Team Defense / ST", [
    ["d_sack", "Sack", 1], ["d_int", "Interception", 1], ["d_fum_rec", "Fumble rec", 1],
    ["d_td", "Touchdown", 1], ["d_safe", "Safety", 1], ["d_block", "Block", 1], ["d_2pt", "2-pt return", 1],
  ]],
];

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("ff_scoring") || "{}");
    return Object.assign({}, DEFAULTS, saved);
  } catch (e) { return Object.assign({}, DEFAULTS); }
}
function saveSettings() { localStorage.setItem("ff_scoring", JSON.stringify(state.settings)); }

function loadMyTeam() {
  try { return new Set(JSON.parse(localStorage.getItem("ff_myteam") || "[]")); }
  catch (e) { return new Set(); }
}
function saveMyTeam() { localStorage.setItem("ff_myteam", JSON.stringify([...state.myTeam])); }

const state = {
  players: [],
  context: null,
  categories: [],
  settings: loadSettings(),
  pos: "ALL",
  search: "",
  riskOnly: false,
  trendingOnly: false,
  myOnly: false,
  sortKey: "value",
  sortDir: -1,
  baselines: {},
  view: "rankings",
  myTeam: loadMyTeam(),
  yahoo: { connected: false, leagues: [] },
};

async function loadYahooStatus() {
  try {
    const st = await (await fetch("/api/yahoo/status")).json();
    state.yahoo = { connected: !!st.connected && !st.error, leagues: st.leagues || [] };
  } catch (e) { state.yahoo = { connected: false, leagues: [] }; }
}
function yahooKeyFor(leagueKey) {
  const lg = LEAGUES[leagueKey];
  if (lg && lg.yahoo_key) return lg.yahoo_key;
  return (state.yahoo.leagues[0] || {}).league_key || null;
}

// Baseline pool size per position for the Value metric (top-N average).
const VALUE_POOL = { WR: 75 };
const VALUE_POOL_DEFAULT = 25;

function adpKey() {
  const p = state.settings.ppr;
  return p >= 0.75 ? "ppr" : p >= 0.25 ? "half_ppr" : "std";
}

const CONF_ORDER = { High: 3, Medium: 2, Low: 1, None: 0 };
const MATCH_DAMP = 0.7; // how strongly opponent matchup moves a projection
const NEXT_GAME_MULT = { Out: 0, IR: 0, PUP: 0, Sus: 0, DNR: 0, NA: 0, Doubtful: 0.25, Questionable: 0.9, COV: 0.5 };
const AVAIL = { IR: 0.12, PUP: 0.35, Sus: 0.55, DNR: 0.30, NA: 0.40, Out: 0.92, Doubtful: 0.95, Questionable: 1.0, COV: 0.9 };

// ---------- scoring engine ----------
function ptsAllowTier(avg) {
  if (avg <= 0) return 10;
  if (avg <= 6) return 7;
  if (avg <= 13) return 4;
  if (avg <= 20) return 1;
  if (avg <= 27) return 0;
  if (avg <= 34) return -1;
  return -4;
}

function scorePoints(cats, s, games) {
  if (!cats) return 0;
  const g = (k) => cats[k] || 0;
  let pts = 0;
  pts += (s.pass_yds_per_pt > 0 ? g("pass_yd") / s.pass_yds_per_pt : 0)
    + g("pass_td") * s.pass_td + g("pass_int") * s.pass_int + g("pass_2pt") * s.pass_2pt;
  pts += (s.rush_yds_per_pt > 0 ? g("rush_yd") / s.rush_yds_per_pt : 0)
    + g("rush_td") * s.rush_td + g("rush_2pt") * s.rush_2pt;
  pts += g("rec") * s.ppr + (s.rec_yds_per_pt > 0 ? g("rec_yd") / s.rec_yds_per_pt : 0)
    + g("rec_td") * s.rec_td + g("rec_2pt") * s.rec_2pt;
  pts += g("fum_lost") * s.fum_lost + g("fum_rec_td") * s.misc_td;
  const fgB = g("fgm_0_19") + g("fgm_20_29") + g("fgm_30_39") + g("fgm_40_49") + g("fgm_50p");
  if (fgB > 0) {
    pts += (g("fgm_0_19") + g("fgm_20_29") + g("fgm_30_39")) * s.fg_0_39
      + g("fgm_40_49") * s.fg_40_49 + g("fgm_50p") * s.fg_50p;
  } else {
    pts += g("fgm") * s.fg_0_39;
  }
  pts += g("xpm") * s.pat + g("fgmiss") * s.fg_miss;
  pts += g("sack") * s.d_sack + g("int") * s.d_int + g("fum_rec") * s.d_fum_rec
    + (g("def_td") + g("st_td") + g("def_st_td")) * s.d_td
    + g("safe") * s.d_safe + g("blk_kick") * s.d_block + g("def_2pt") * s.d_2pt;
  if ("pts_allow" in cats && games > 0) pts += ptsAllowTier(g("pts_allow") / games) * games;
  return pts;
}

// Baseline durability: expected share of games a healthy player actually plays,
// from position + historical games-played rate + an age penalty (RBs, and 32+).
const DUR_POS_BASE = { QB: 0.93, RB: 0.84, WR: 0.90, TE: 0.88, K: 0.97, DEF: 1.0 };
function durabilityRate(p) {
  const base = DUR_POS_BASE[p.position] != null ? DUR_POS_BASE[p.position] : 0.9;
  const played = (p.history || []).filter((h) => h.gp > 0);
  let rate = base;
  if (played.length) {
    const avg = played.reduce((sum, h) => sum + Math.min(h.gp, 17), 0) / played.length;
    rate = 0.6 * (avg / 17) + 0.4 * base;
  }
  const age = p.age || 0;
  if (p.position === "RB" && age >= 28) rate -= 0.04 * (age - 27);
  else if (age >= 32) rate -= 0.02 * (age - 31);
  return Math.max(0.6, Math.min(1.0, rate));
}

// ---------- projection model (ported from the Python reference) ----------
function projectPlayer(p, s, ctx) {
  const games = (ctx && ctx.reg_season_games) || 17;
  const played = p.history.filter((h) => h.gp > 0).map((h) => {
    const pts = scorePoints(h.cats, s, h.gp);
    return { year: h.year, gp: h.gp, pts: pts, ppg: pts / h.gp };
  });
  let baseline = 0, n = played.length, cv = 0;
  if (n > 0) {
    const wts = []; for (let i = n; i > 0; i--) wts.push(i);
    const tw = wts.reduce((a, b) => a + b, 0);
    baseline = played.reduce((sum, r, i) => sum + wts[i] * r.ppg, 0) / tw;
    const vals = played.map((r) => r.ppg);
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const variance = vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length;
    cv = baseline > 0 && vals.length > 1 ? Math.sqrt(variance) / baseline : 0;
  }
  const status = (p.injury && p.injury.status) || "";
  const wkPts = scorePoints(p.proj_week, s, 1);
  // Season market = ensemble of Sleeper + ESPN projections (each scored under your scoring).
  let snPts = scorePoints(p.proj_season, s, games);
  const espnPts = p.proj_season_espn ? scorePoints(p.proj_season_espn, s, games) : 0;
  if (espnPts > 0) snPts = snPts > 0 ? (snPts + espnPts) / 2 : espnPts;
  // Opponent matchup adjustment (dampened so one matchup can't dominate).
  const mNext = p.matchup && p.matchup.next ? p.matchup.next.mult : 1;
  const mRos = p.matchup && p.matchup.ros ? p.matchup.ros.mult : 1;
  const adjNext = 1 + MATCH_DAMP * (mNext - 1);
  const adjRos = 1 + MATCH_DAMP * (mRos - 1);
  const rawNext = (wkPts > 0 ? 0.55 * wkPts + 0.45 * baseline : baseline) * adjNext;
  const rosPpg = (snPts > 0 ? 0.5 * (snPts / games) + 0.5 * baseline : baseline) * adjRos;
  const remaining = (ctx && ctx.remaining_games) || 0;
  const dur = durabilityRate(p);
  const statusAvail = status in AVAIL ? AVAIL[status] : 1;
  // Reality check: unsigned free agents can't produce until signed; season-ending
  // injury types (ACL/Achilles/etc.) shouldn't ride a big historical baseline.
  let rosterFactor = 1;
  if (p.team === "FA") rosterFactor *= 0.15;
  const bodyPart = ((p.injury && p.injury.body_part) || "").toLowerCase();
  if (p.injury && p.injury.is_risk && /(acl|achilles|torn|ruptur|lisfranc)/.test(bodyPart)) rosterFactor *= 0.35;
  const nextGame = rawNext * (status in NEXT_GAME_MULT ? NEXT_GAME_MULT[status] : 1) * rosterFactor;
  const expGames = remaining * dur * statusAvail;   // durability + current injury
  const rosTotal = rosPpg * expGames * rosterFactor;
  return {
    baseline_ppg: baseline, next_game: nextGame, ros_ppg: rosPpg, ros_total: rosTotal,
    market_week: wkPts, seasons_used: n, adj_next: adjNext, adj_ros: adjRos,
    expected_games: expGames, durability: dur, remaining_games: remaining, roster_factor: rosterFactor,
    confidence: n >= 3 && cv < 0.35 ? "High" : n >= 2 && cv < 0.6 ? "Medium" : n >= 1 ? "Low" : "None",
    played: played,
  };
}

function recomputeAll() {
  for (const p of state.players) p._proj = projectPlayer(p, state.settings, state.context);
  computeValues();
  computeDerived();
}

// Overall value rank, ADP sleeper/reach, and positional tiers (drop-off based).
function computeDerived() {
  const sorted = [...state.players].sort((a, b) => b._value - a._value);
  sorted.forEach((p, i) => { p._valueRank = i + 1; });
  const k = adpKey();
  for (const p of state.players) {
    const adp = p.adp ? p.adp[k] : null;
    p._adp = adp;
    p._adpDiff = adp != null ? Math.round(adp - p._valueRank) : null;
  }
  const byPos = {};
  for (const p of state.players) (byPos[p.position] = byPos[p.position] || []).push(p);
  for (const pos in byPos) {
    const arr = byPos[pos].sort((a, b) => b._proj.ros_total - a._proj.ros_total);
    const top = arr.slice(0, 40);
    const gaps = [];
    for (let i = 1; i < top.length; i++) gaps.push(top[i - 1]._proj.ros_total - top[i]._proj.ros_total);
    const sortedGaps = [...gaps].sort((a, b) => a - b);
    const median = sortedGaps.length ? sortedGaps[Math.floor(sortedGaps.length / 2)] : 0;
    const threshold = Math.max(6, median * 1.6);
    let tier = 1;
    arr.forEach((p, i) => {
      if (i > 0 && (arr[i - 1]._proj.ros_total - p._proj.ros_total) > threshold) tier++;
      p._tier = tier;
    });
  }
}

// Value = a player's rest-of-season projection minus the average ROS projection
// of the top-N players at their position (N=25, or 75 for WR). Comparable across
// positions and recomputed whenever scoring changes.
function computeValues() {
  const byPos = {};
  for (const p of state.players) (byPos[p.position] = byPos[p.position] || []).push(p);
  state.baselines = {};
  for (const pos in byPos) {
    const arr = byPos[pos].slice().sort((a, b) => b._proj.ros_total - a._proj.ros_total);
    const n = Math.min(arr.length, VALUE_POOL[pos] || VALUE_POOL_DEFAULT);
    let sum = 0;
    for (let i = 0; i < n; i++) sum += arr[i]._proj.ros_total;
    const base = n ? sum / n : 0;
    state.baselines[pos] = { base: base, n: n };
    arr.forEach((p, i) => { p._value = p._proj.ros_total - base; p._posRank = i + 1; });
  }
}

// ---------- data load ----------
async function loadPlayers() {
  const res = await fetch("/api/players");
  if (!res.ok) throw new Error("Failed to load players");
  const data = await res.json();
  state.players = data.players;
  state.context = data.context;
  state.categories = data.categories || [];
  renderContext(data);
  recomputeAll();
  render();
}

function renderContext(data) {
  const c = data.context;
  const type = { pre: "Preseason", regular: "Regular Season", post: "Postseason", off: "Offseason" }[c.season_type] || c.season_type;
  const wk = c.season_type === "regular" ? ` · Upcoming: Week ${c.upcoming_week}` : ` · Draft season`;
  document.getElementById("context-line").textContent =
    `${c.season} ${type}${wk} · ${data.count} eligible players · history ${c.history_years[0]}–${c.history_years[c.history_years.length - 1]}`;
  const f = data.freshness || {};
  document.getElementById("freshness").textContent =
    `Players ${f.players || "?"} · Injuries ${f.injuries || "?"} · News ${f.news || "?"}`;
}

// ---------- scoring label + presets ----------
function scoringLabel() {
  const s = state.settings;
  const base = s.ppr === 1 ? "PPR" : s.ppr === 0.5 ? "Half-PPR" : s.ppr === 0 ? "Standard" : `${s.ppr}-PPR`;
  const custom = JSON.stringify(Object.assign({}, s, { ppr: DEFAULTS.ppr })) !== JSON.stringify(Object.assign({}, DEFAULTS));
  return custom ? base + " (custom)" : base;
}
function activePreset() {
  const changedBeyondPpr = Object.keys(DEFAULTS).some((k) => k !== "ppr" && state.settings[k] !== DEFAULTS[k]);
  if (changedBeyondPpr) return null;
  return state.settings.ppr === 1 ? "ppr" : state.settings.ppr === 0.5 ? "half_ppr" : state.settings.ppr === 0 ? "std" : null;
}
function syncPresetButtons() {
  const ap = activePreset();
  document.querySelectorAll("#scoring button").forEach((b) => b.classList.toggle("active", b.dataset.scoring === ap));
  const lbl = document.getElementById("scoring-label");
  if (lbl) lbl.textContent = scoringLabel();
  syncLeagueSwitch();
}

// ---------- accessors ----------
function sortValue(p) {
  const pr = p._proj;
  switch (state.sortKey) {
    case "name": return p.name.toLowerCase();
    case "position": return p.position;
    case "team": return p.team;
    case "age": return p.age || 0;
    case "status": return p.injury.is_risk ? 1 : 0;
    case "baseline_ppg": return pr.baseline_ppg;
    case "last_ppg": return pr.played.length ? pr.played[0].ppg : 0;
    case "next_game": return pr.next_game;
    case "exp_games": return pr.expected_games;
    case "ros_total": return pr.ros_total;
    case "value": return p._value != null ? p._value : -9999;
    case "tier": return -(p._tier || 99);   // tier 1 first
    case "adp_diff": return p._adpDiff != null ? p._adpDiff : -9999;
    case "snap": return p.usage && p.usage.snap_pct != null ? p.usage.snap_pct : -1;
    case "tgt_share": return p.usage && p.usage.target_share != null ? p.usage.target_share : -1;
    case "confidence": return CONF_ORDER[pr.confidence] || 0;
    default: return 0;
  }
}

function visiblePlayers() {
  const q = state.search.trim().toLowerCase();
  let list = state.players.filter((p) => {
    if (state.pos !== "ALL" && p.position !== state.pos) return false;
    if (state.riskOnly && !p.injury.is_risk) return false;
    if (state.trendingOnly && !(p.trending > 0)) return false;
    if (state.myOnly && !state.myTeam.has(p.id)) return false;
    if (q && !(p.name.toLowerCase().includes(q) || (p.team || "").toLowerCase().includes(q))) return false;
    return true;
  });
  list.sort((a, b) => {
    const va = sortValue(a), vb = sortValue(b);
    if (va < vb) return -1 * state.sortDir;
    if (va > vb) return 1 * state.sortDir;
    return 0;
  });
  return list;
}

// ---------- render table ----------
const STATUS_SHORT = { Questionable: "Q", Doubtful: "D", Out: "Out" };
const STATUS_CLASS = { Questionable: "st-Q", Doubtful: "st-D", Out: "st-O", IR: "st-IR", PUP: "st-PUP", Sus: "st-Sus", DNR: "st-DNR", NA: "st-NA" };

function statusBadge(inj) {
  if (!inj.status) return '<span class="status-badge st-ok">—</span>';
  const cls = STATUS_CLASS[inj.status] || "st-ok";
  const short = STATUS_SHORT[inj.status] || (inj.status.length > 4 ? inj.status.slice(0, 3) : inj.status);
  const title = inj.status + (inj.body_part ? " · " + inj.body_part : "");
  return `<span class="status-badge ${cls}" title="${esc(title)}">${esc(short)}</span>`;
}

function render() {
  const rows = visiblePlayers();
  document.getElementById("result-count").textContent = `${rows.length} shown`;
  const tbody = document.getElementById("rows");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="17" class="empty">No players match your filters.</td></tr>';
    return;
  }
  const frag = rows.slice(0, 600).map((p, i) => {
    const pr = p._proj;
    const lastPpg = pr.played.length ? pr.played[0].ppg : 0;
    const starOn = state.myTeam.has(p.id) ? "on" : "";
    const cons = p.consistency;
    const consDot = cons ? `<span class="cons-dot cons-${cons.rating}" title="${cons.rating}: floor ${cons.floor}, ceiling ${cons.ceiling} (2025 PPR)"></span>` : "";
    const trend = p.trending > 0 ? ' <span class="trend-badge" title="Trending add">🔥</span>' : "";
    let adpCell = "—";
    if (p._adpDiff != null) {
      const cls = p._adpDiff >= 12 ? "adp-sleeper" : p._adpDiff <= -12 ? "adp-reach" : "";
      adpCell = `<span class="${cls}">${p._adpDiff > 0 ? "+" : ""}${p._adpDiff}</span>`;
    }
    return `<tr data-id="${esc(p.id)}">
      <td class="rank">${i + 1}</td>
      <td class="left"><span class="star ${starOn}" data-star="${esc(p.id)}" title="Add to My Team">★</span><span class="pname">${esc(p.name)}</span>${p.is_rookie ? ' <span class="mini-rookie">R</span>' : ""}${consDot}${trend}
        <div class="pmeta">${p.bye_week ? "Bye " + p.bye_week : ""}${p.years_exp != null ? (p.bye_week ? " · " : "") + p.years_exp + "y" : ""}</div></td>
      <td><span class="pos-badge pos-${p.position}">${p.position}</span></td>
      <td>${esc(p.team)}</td>
      <td>${p.age ?? "—"}</td>
      <td>${statusBadge(p.injury)}</td>
      <td>${fmt(pr.baseline_ppg)}</td>
      <td>${lastPpg ? fmt(lastPpg) : "—"}</td>
      <td>${fmt(pr.next_game)}</td>
      <td class="${pr.durability < 0.8 ? "dur-low" : ""}" title="durability ${Math.round(pr.durability * 100)}%">${fmt(pr.expected_games, 0)}</td>
      <td>${fmt(pr.ros_total, 0)}</td>
      <td class="big ${p._value >= 0 ? "val-pos" : "val-neg"}">${p._value >= 0 ? "+" : ""}${fmt(p._value, 0)}</td>
      <td><span class="tier-badge">${p._tier ? "T" + p._tier : "—"}</span></td>
      <td>${adpCell}</td>
      <td>${p.usage && p.usage.snap_pct != null ? p.usage.snap_pct + "%" : "—"}</td>
      <td>${p.usage && p.usage.target_share != null && p.usage.targets ? p.usage.target_share + "%" : "—"}</td>
      <td class="conf-${pr.confidence}">${pr.confidence || "—"}</td>
    </tr>`;
  }).join("");
  tbody.innerHTML = frag + (rows.length > 600 ? `<tr><td colspan="17" class="muted" style="text-align:center;padding:.8rem">Showing top 600 of ${rows.length}. Refine with search or filters.</td></tr>` : "");
}

// ---------- drawer / player detail ----------
async function openPlayer(id) {
  const drawer = document.getElementById("drawer");
  const content = document.getElementById("drawer-content");
  const base = state.players.find((p) => p.id === id);
  // Render immediately from already-loaded raw data, then augment with news/college.
  if (base) content.innerHTML = renderPlayer(base, null);
  else content.innerHTML = '<p class="muted">Loading player…</p>';
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  try {
    const res = await fetch("/api/player/" + encodeURIComponent(id));
    const extra = await res.json();
    if (!extra.error) content.innerHTML = renderPlayer(base || extra, extra);
  } catch (e) { /* base render already shown */ }
}

// Insider chatter: keyword-filtered posts from beat reporters (Bluesky) + Reddit.
function insiderAgo(iso) {
  if (!iso) return "";
  const t = Date.parse(iso); if (isNaN(t)) return "";
  const m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return "just now"; if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60); if (h < 24) return h + "h ago";
  return Math.floor(h / 24) + "d ago";
}
function renderInsider(extra) {
  const items = (extra && extra.insider) || [];
  if (!extra) return "";
  if (!items.length) {
    return `<div class="section-h">Insider chatter <span class="muted" style="font-size:.72rem">· beat reporters (Bluesky)</span></div>
      <p class="muted">No injury/roster chatter from the monitored reporters right now. Edit <code>data/insiders.txt</code> to follow the beat writers you trust.</p>`;
  }
  const rows = items.map((it) => `<div class="insider-item bsky">
      <div class="insider-meta"><span class="insider-plat">🦋 ${esc(it.source)}</span>${it.pub ? ` <span class="muted">· ${insiderAgo(it.pub)}</span>` : ""}</div>
      <a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.text)}</a>
    </div>`).join("");
  return `<div class="section-h">Insider chatter <span class="muted" style="font-size:.72rem">· beat reporters (Bluesky), injury/roster-filtered</span></div>
    <div class="insider-list">${rows}</div>`;
}

function renderPlayer(p, extra) {
  const s = state.settings;
  const ctx = state.context;
  const pr = p._proj || projectPlayer(p, s, ctx);
  const inj = (extra && extra.injury) || p.injury;

  let injHtml;
  const detail = inj.detail || (extra && extra.injury && extra.injury.detail);
  if (inj.status || detail) {
    injHtml = `<div class="injury-box ${inj.is_risk ? "" : "ok"}">
      <div><span class="st">${esc(inj.status || "Active")}</span>${inj.body_part ? " · " + esc(inj.body_part) : ""}</div>
      ${detail ? `<div style="margin-top:.4rem">${esc(detail)}</div>` : `<div class="muted" style="margin-top:.3rem">No injury designation reported.</div>`}
    </div>`;
  } else {
    injHtml = `<div class="injury-box ok"><span class="st">Active</span><div class="muted" style="margin-top:.3rem">No injury designation reported.</div></div>`;
  }

  // 5-season table (points computed live under current scoring)
  const catFields = state.categories.length ? state.categories : ((extra && extra.category_fields) || []);
  const usedCats = catFields.filter(([f]) => p.history.some((h) => h.cats && h.cats[f] != null));
  const head = `<tr><th class="left">Season</th><th>G</th><th>Pts</th><th>PPG</th>${usedCats.map(([, l]) => `<th>${l}</th>`).join("")}</tr>`;
  const body = p.history.length ? p.history.map((h) => {
    const pts = scorePoints(h.cats, s, h.gp);
    return `<tr>
      <td class="left">${h.year}</td><td>${h.gp}</td>
      <td>${fmt(pts, 0)}</td><td>${h.gp ? fmt(pts / h.gp) : "—"}</td>
      ${usedCats.map(([f]) => `<td>${h.cats && h.cats[f] != null ? fmt(h.cats[f], 0) : "—"}</td>`).join("")}
    </tr>`;
  }).join("") : `<tr><td colspan="${4 + usedCats.length}" class="muted" style="text-align:center">No prior-season NFL data (rookie or new to the league).</td></tr>`;

  const collegeHtml = p.is_rookie ? renderCollege(p, extra) : "";
  const news = (extra && extra.news) || [];
  const newsHtml = news.length ? news.map((n) => `<div class="news-item">
      <a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a>
      ${n.description ? `<div class="desc">${esc(n.description)}</div>` : ""}
    </div>`).join("") : `<p class="muted">${extra ? "No recent headlines matched this player in the ESPN feed. Use the research links below." : "Loading news…"}</p>`;
  const links = ((extra && extra.research_links) || []).map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join("");

  const nextLabel = ctx && ctx.season_type === "regular" ? `Next Game (Wk ${ctx.upcoming_week})` : "Next Game (Wk 1)";
  const byeTxt = p.bye_week ? `Bye: Week ${p.bye_week}` : "Bye: TBD";

  return `
    <div class="dh"><h2>${esc(p.name)}</h2>
      <span class="pos-badge pos-${p.position}">${p.position}</span>
      ${p.is_rookie ? '<span class="rookie-badge">ROOKIE</span>' : ""}
      <span class="muted">${esc(p.team)}${p.number ? " · #" + p.number : ""}</span>
    </div>
    <div class="dsub">${p.age ? "Age " + p.age : ""}${p.years_exp != null ? " · " + p.years_exp + " yrs exp" : ""} · <strong style="color:var(--text)">${byeTxt}</strong> · Scoring: ${esc(scoringLabel())}</div>

    ${injHtml}

    ${pr.roster_factor < 0.6 ? `<div class="injury-box" style="border-color:var(--danger);margin-top:.5rem"><span class="st m-bad">⚠ Projection heavily discounted</span> — ${p.team === "FA" ? "currently unsigned (no NFL team)" : ""}${p.injury && /acl|achilles|torn|ruptur|lisfranc/i.test(p.injury.body_part || "") ? (p.team === "FA" ? " · " : "") + "season-affecting injury type" : ""}. His baseline reflects past production, not current availability — draft/start with caution.</div>` : ""}

    <div class="proj-cards">
      <div class="proj-card"><div class="label">${esc(nextLabel)}</div><div class="val">${fmt(pr.next_game)}</div><div class="sub">projected pts</div></div>
      <div class="proj-card"><div class="label">Rest of Season</div><div class="val">${fmt(pr.ros_total, 0)}</div><div class="sub">${fmt(pr.ros_ppg)} pts/gm · exp. ~${fmt(pr.expected_games, 0)}/${pr.remaining_games} G</div></div>
      <div class="proj-card"><div class="label">Positional Value</div><div class="val ${p._value >= 0 ? "val-pos" : "val-neg"}">${p._value >= 0 ? "+" : ""}${fmt(p._value, 0)}</div><div class="sub">${p.position}${p._posRank ? " #" + p._posRank : ""} · vs avg top-${(state.baselines[p.position] || {}).n || (VALUE_POOL[p.position] || VALUE_POOL_DEFAULT)}</div></div>
    </div>
    <div class="method" style="margin-top:0">
      <strong>Value</strong> = this player's rest-of-season projection (${fmt(pr.ros_total, 0)}) minus the average of the top ${(state.baselines[p.position] || {}).n || (VALUE_POOL[p.position] || VALUE_POOL_DEFAULT)} ${p.position}s (${fmt((state.baselines[p.position] || {}).base, 0)}). Positive = above a typical starter at the position; comparable across positions for draft/trade decisions.
    </div>

    ${renderMatchup(p, pr)}

    ${(p.position === "QB" || p.position === "RB") ? renderSituation(p) : ""}

    ${renderUsage(p)}

    ${p.consistency ? renderConsistency(p) : ""}

    ${renderDraftValue(p)}

    <div class="section-h">Season by season — ${esc(scoringLabel())}</div>
    <div class="mini-wrap"><table class="mini"><thead>${head}</thead><tbody>${body}</tbody></table></div>

    ${renderWeekly(p, extra)}

    ${collegeHtml}

    <div class="section-h">Recent News</div>
    ${newsHtml}

    ${renderInsider(extra)}

    <div class="section-h">Scour the web (live)</div>
    <div class="links">${links || '<span class="muted">Loading…</span>'}</div>

    <div class="method">
      <strong>How these projections work:</strong> a recency-weighted per-game average of up to five seasons
      (recent years weighted more), blended with a <strong>consensus market projection</strong> — Sleeper${p.proj_season_espn && Object.keys(p.proj_season_espn).length ? " + ESPN" : ""} averaged, scored under your rules${pr.market_week ? ` (this week ≈ ${fmt(pr.market_week)} pts)` : ""},
      then adjusted for injury status. Rest-of-season also applies a <strong>durability</strong> factor
      (${Math.round(pr.durability * 100)}% — from position, games-played history, and age), so it projects
      <strong>~${fmt(pr.expected_games, 0)} of ${pr.remaining_games}</strong> games rather than assuming full health.
      All points are recomputed live from raw stats using <em>your</em> scoring settings.
      Estimates, not guarantees — always check the news above.
    </div>
  `;
}

function renderWeekly(p, extra) {
  const s = state.settings;
  const pr = p._proj || projectPlayer(p, s, state.context);
  const log = (extra && extra.week_log) || [];
  const weeks = (p.matchup && p.matchup.weeks) || [];
  if (!log.length && !weeks.length) return "";
  const ctx = state.context;
  const rows = [];
  for (const g of log) {
    const pts = g.cats ? scorePoints(g.cats, s, 1) : null;
    const res = g.opp == null ? "Bye" : (g.played ? fmt(pts) : "DNP");
    rows.push(`<tr><td>${g.week}</td><td class="left">${g.opp ? "vs " + esc(g.opp) : "—"}</td><td>${res}</td><td class="muted">actual</td></tr>`);
  }
  for (const w of weeks) {
    const mult = 1 + MATCH_DAMP * (w.mult - 1);
    const proj = (ctx && ctx.season_type === "regular" && w.week === ctx.upcoming_week) ? pr.next_game : pr.ros_ppg * mult;
    const cls = w.mult >= 1.05 ? "m-good" : w.mult <= 0.95 ? "m-bad" : "";
    rows.push(`<tr><td>${w.week}</td><td class="left">vs <span class="${cls}">${esc(w.opp)}</span></td><td>${fmt(proj)}</td><td class="muted">proj</td></tr>`);
  }
  return `<div class="section-h">This season — week by week</div>
    <div class="mini-wrap"><table class="mini"><thead><tr><th>Wk</th><th class="left">Opp</th><th>Pts</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table></div>
    <div class="muted" style="font-size:.7rem;margin-top:.2rem">Past weeks show actual points under your scoring; upcoming weeks show matchup-adjusted projections (green/red = easy/tough opponent).</div>`;
}

function renderUsage(p) {
  const u = p.usage;
  if (!u) return "";
  const rows = [];
  if (u.snap_pct != null) rows.push(["Snap share", u.snap_pct + "%"]);
  if (u.target_share != null && u.targets) rows.push(["Target share", `${u.target_share}% · ${u.tgt_per_g}/g (${u.targets} total)`]);
  if (p.position === "RB" && u.carry_share != null && u.carries) rows.push(["Carry share", `${u.carry_share}% · ${u.carry_per_g}/g (${u.carries} total)`]);
  if (u.rz_touches) rows.push(["Red-zone touches", `${u.rz_touches} · ${u.rz_per_g}/g`]);
  if (u.adot != null && u.targets) rows.push(["aDOT (avg target depth)", u.adot + " yd"]);
  if (!rows.length) return "";
  return `<div class="section-h">Usage &amp; opportunity <span class="muted" style="text-transform:none">· ${u.gp} games, prior season</span></div>
    <div class="sit-card wide">${rows.map((r) => `<div class="sit-row"><span>${r[0]}</span><span>${r[1]}</span></div>`).join("")}
      <div class="muted" style="font-size:.7rem;margin-top:.3rem">Opportunity is more predictive than past points — high snap %, target/carry share, and red-zone volume signal a secure, valuable role.</div></div>`;
}

const RATING_CLS = { Steady: "m-good", Balanced: "", Volatile: "m-bad" };
function renderConsistency(p) {
  const c = p.consistency;
  return `<div class="section-h">Consistency <span class="muted" style="text-transform:none">· 2025 game log (PPR)</span></div>
    <div class="sit-cards">
      <div class="sit-card"><h4>Floor / Ceiling</h4>
        <div class="sit-row"><span>Floor (20th pct)</span><span>${c.floor}</span></div>
        <div class="sit-row"><span>Ceiling (85th pct)</span><span>${c.ceiling}</span></div>
        <div class="sit-row"><span>Avg / game</span><span>${c.ppg}</span></div>
      </div>
      <div class="sit-card"><h4>Profile</h4>
        <div class="sit-row"><span>Rating</span><span class="${RATING_CLS[c.rating] || ""}" style="font-weight:700">${c.rating}</span></div>
        <div class="sit-row"><span>Boom / Bust</span><span>${c.boom}% / ${c.bust}%</span></div>
        <div class="muted" style="font-size:.7rem;margin-top:.2rem">${c.games} games · boom = ≥1.5×avg, bust = ≤0.5×avg</div>
      </div>
    </div>`;
}

function renderDraftValue(p) {
  const inTeam = state.myTeam.has(p.id);
  let verdict = "";
  if (p._adp != null) {
    const d = p._adpDiff;
    const label = d >= 12 ? "Sleeper (undervalued)" : d <= -12 ? "Reach (overvalued)" : "Fairly valued";
    const cls = d >= 12 ? "adp-sleeper" : d <= -12 ? "adp-reach" : "";
    verdict = `<div class="sit-row"><span>Market ADP (${adpKey().replace("_", "-").toUpperCase()})</span><span>${fmt(p._adp, 1)}</span></div>
      <div class="sit-row"><span>Our overall value rank</span><span>#${p._valueRank}</span></div>
      <div class="sit-row"><span>Verdict</span><span class="${cls}">${label} (${d > 0 ? "+" : ""}${d})</span></div>`;
  } else {
    verdict = `<div class="sit-row muted"><span>Market ADP</span><span>undrafted / n/a</span></div>`;
  }
  const trend = p.trending > 0 ? `<div class="sit-row"><span>Trending</span><span class="trend-badge">🔥 ${p.trending.toLocaleString()} adds/24h</span></div>` : "";
  return `<div class="section-h">Draft value</div>
    <div class="sit-card wide">${verdict}${trend}
      <button class="btn ${inTeam ? "primary" : ""}" style="margin-top:.6rem" onclick="toggleMyTeam('${esc(p.id)}', this)">${inTeam ? "★ On my team — remove" : "☆ Add to my team"}</button>
    </div>`;
}

function matchDesc(rankObj) {
  if (!rankObj) return { txt: "—", cls: "" };
  const pct = rankObj.rank / rankObj.of; // rank 1 = toughest defense
  if (pct >= 0.78) return { txt: "Great matchup", cls: "m-good" };
  if (pct >= 0.56) return { txt: "Favorable", cls: "m-good" };
  if (pct >= 0.34) return { txt: "Neutral", cls: "" };
  if (pct >= 0.16) return { txt: "Tough", cls: "m-bad" };
  return { txt: "Very tough", cls: "m-bad" };
}
function pctSigned(mult) {
  const v = Math.round((mult - 1) * 100);
  return (v > 0 ? "+" : "") + v + "%";
}
function renderMatchup(p, pr) {
  const mu = p.matchup;
  if (!mu) return "";
  const pos = mu.pos;
  let rows = "";
  if (mu.next && mu.next.opp) {
    const d = matchDesc(mu.next.rank);
    const rk = mu.next.rank ? `${p.team === mu.next.opp ? "" : mu.next.opp} allows ${mu.next.rank.of - mu.next.rank.rank + 1}${ord(mu.next.rank.of - mu.next.rank.rank + 1)}-most to ${pos}s` : "";
    rows += `<div class="sit-row"><span>Next: <strong>vs ${esc(mu.next.opp)}</strong></span>
      <span class="${d.cls}">${d.txt} · ${pctSigned(pr.adj_next)}</span></div>
      ${rk ? `<div class="muted" style="font-size:.72rem">${esc(rk)}</div>` : ""}`;
  } else {
    rows += `<div class="sit-row muted"><span>Next opponent</span><span>TBD (bye or schedule pending)</span></div>`;
  }
  if (mu.ros) {
    const sos = mu.ros.mult >= 1.05 ? { txt: "Easy", cls: "m-good" } : mu.ros.mult <= 0.95 ? { txt: "Hard", cls: "m-bad" } : { txt: "Average", cls: "" };
    rows += `<div class="sit-row"><span>Rest-of-season schedule (${mu.ros.games} games)</span>
      <span class="${sos.cls}">${sos.txt} · ${pctSigned(pr.adj_ros)}</span></div>`;
  }
  return `<div class="section-h">Matchup — opponent defense vs ${pos}</div>
    <div class="sit-card wide">${rows}
      <div class="muted" style="font-size:.7rem;margin-top:.4rem">Based on fantasy points each defense allowed to ${pos}s last season (DvP), applied to your upcoming and remaining opponents. Already reflected in the projections above.</div>
    </div>`;
}
function ord(n) { const s = ["th", "st", "nd", "rd"], v = n % 100; return (s[(v - 20) % 10] || s[v] || s[0]); }

function gradeBadge(g) {
  if (!g) return '<span class="muted">n/a</span>';
  return `<span class="grade grade-${g.grade}">${g.grade}</span> <span class="muted">#${g.rank}/${g.of}</span>`;
}

function renderSituation(p) {
  const parts = [];
  const ol = p.oline;
  if (ol) {
    let rows = `<div class="sit-row"><span>Pass protection</span><span>${gradeBadge(ol.pass_block)}</span></div>`;
    if (p.position === "RB") rows += `<div class="sit-row"><span>Run blocking</span><span>${gradeBadge(ol.run_block)}</span></div>`;
    if (ol.continuity_pct != null) {
      rows += `<div class="sit-row"><span>Line continuity</span><span>${ol.returning}/5 return · ${ol.continuity_pct}%</span></div>`;
    }
    // projected starting five
    let five = "";
    if (ol.starters && ol.starters.length) {
      five = `<div class="ol-list">` + ol.starters.map((s) => `
        <div class="ol-man">
          <span class="ol-pos">${esc(s.depth || "OL")}</span>
          <span class="ol-name">${esc(s.name)}</span>
          ${s.rookie ? '<span class="ol-tag rookie">ROOKIE</span>' : s.new ? `<span class="ol-tag new">NEW${s.prior_team ? " ← " + esc(s.prior_team) : ""}</span>` : ""}
          <span class="ol-exp muted">${s.exp}y</span>
        </div>`).join("") + `</div>`;
    }
    const label = ol.personnel_based ? "2026 projected line" : "2025 line (roster data unavailable)";
    parts.push(`<div class="sit-card wide"><h4>Offensive line <span class="muted" style="text-transform:none">· ${label}</span></h4>${rows}${five}
      <div class="muted" style="font-size:.7rem;margin-top:.4rem">Grade = each projected starter's 2025 pass/run-block performance, ranked vs league. 2025 team allowed ${ol.sacks_allowed_pg} sacks/gm. Proxy, not individual grades.</div></div>`);
  }
  const wl = p.workload;
  if (wl) {
    parts.push(`<div class="sit-card"><h4>Backfield role</h4>
      <div class="sit-row"><span><strong>${esc(wl.label)}</strong></span><span>${wl.share_pct != null ? wl.share_pct + "% of carries" : "—"}</span></div>
      <div class="muted" style="font-size:.72rem;margin-top:.2rem">${wl.proj_carries ? "~" + wl.proj_carries + " projected carries · " : ""}based on ${esc(wl.basis)}</div></div>`);
  }
  if (!parts.length) return "";
  return `<div class="section-h">Situation</div><div class="sit-cards">${parts.join("")}</div>`;
}

function prettyKey(k) {
  return String(k).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
function renderCollege(p, extra) {
  const cs = extra && extra.college_stats;
  const collegeName = p.college ? esc(p.college) : "College";
  let inner;
  if (cs && cs.seasons && cs.seasons.length) {
    const keys = [];
    cs.seasons.forEach((row) => Object.keys(row).forEach((k) => {
      if (k !== "year" && k !== "team" && !keys.includes(k)) keys.push(k);
    }));
    const head = `<tr><th class="left">Season</th><th class="left">Team</th>${keys.map((k) => `<th>${esc(prettyKey(k))}</th>`).join("")}</tr>`;
    const rows = cs.seasons.map((row) => `<tr><td class="left">${esc(row.year)}</td><td class="left">${esc(row.team || "")}</td>${keys.map((k) => `<td>${row[k] != null ? esc(row[k]) : "—"}</td>`).join("")}</tr>`).join("");
    inner = `<div class="mini-wrap"><table class="mini"><thead>${head}</thead><tbody>${rows}</tbody></table></div>
      <div class="muted" style="margin-top:.3rem;font-size:.72rem">Source: ${esc(cs.source || "CollegeFootballData.com")}</div>`;
  } else if (extra) {
    const note = extra.cfbd_enabled
      ? "No college stat lines were found for this player."
      : "In-app college stat tables need a free CollegeFootballData API key (set CFBD_API_KEY — see the README). Meanwhile:";
    const links = ((extra && extra.college_links) || []).map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join("");
    inner = `<p class="muted" style="margin:.2rem 0 .5rem">${note}</p><div class="links">${links}</div>`;
  } else {
    inner = '<p class="muted">Loading college stats…</p>';
  }
  return `<div class="section-h">College — ${collegeName}</div>${inner}`;
}

function closeDrawer() {
  const d = document.getElementById("drawer");
  d.classList.remove("open");
  d.setAttribute("aria-hidden", "true");
}

// ---------- scoring settings panel ----------
function buildScoringPanel() {
  const host = document.getElementById("scoring-body");
  host.innerHTML = SETTING_GROUPS.map(([title, fields]) => `
    <div class="sgroup"><h4>${title}</h4>${fields.map(([key, label, step]) => `
      <label class="srow"><span>${label}</span>
        <input type="number" step="${step}" data-skey="${key}" value="${state.settings[key]}" />
      </label>`).join("")}</div>`).join("");
  host.querySelectorAll("input[data-skey]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const v = parseFloat(inp.value);
      state.settings[inp.dataset.skey] = isNaN(v) ? 0 : v;
      saveSettings(); recomputeAll(); render(); syncPresetButtons();
    });
  });
}
function openScoring() {
  buildScoringPanel();
  const m = document.getElementById("scoring-modal");
  m.classList.add("open"); m.setAttribute("aria-hidden", "false");
}
function closeScoring() {
  const m = document.getElementById("scoring-modal");
  m.classList.remove("open"); m.setAttribute("aria-hidden", "true");
}
function applyPreset(preset) {
  // Reset everything to defaults, then set reception value for the preset.
  state.settings = Object.assign({}, DEFAULTS);
  state.settings.ppr = preset === "ppr" ? 1 : preset === "half_ppr" ? 0.5 : 0;
  saveSettings(); recomputeAll(); render(); syncPresetButtons();
  const body = document.getElementById("scoring-body");
  if (body && body.children.length) buildScoringPanel();
}

// ===========================================================================
// League presets (shared by My Team lineup + Draft optimizer)
// ===========================================================================
const JOHNNYV_SCORING = {
  pass_yds_per_pt: 20, pass_td: 5, pass_int: -2, pass_2pt: 2,
  rush_yds_per_pt: 8, rush_td: 5, rush_2pt: 2,
  ppr: 0, rec_yds_per_pt: 8, rec_td: 5, rec_2pt: 2,
  fum_lost: -2, misc_td: 5,
  pat: 1, fg_0_39: 3, fg_40_49: 4, fg_50p: 5, fg_miss: -1,
  d_sack: 1, d_int: 2, d_fum_rec: 2, d_td: 5, d_safe: 2, d_block: 2, d_2pt: 2,
};
const DENNIS_SCORING = {
  pass_yds_per_pt: 25, pass_td: 6, pass_int: -1, pass_2pt: 2,
  rush_yds_per_pt: 10, rush_td: 6, rush_2pt: 2,
  ppr: 0.5, rec_yds_per_pt: 10, rec_td: 6, rec_2pt: 2,
  fum_lost: -2, misc_td: 6,
  pat: 1, fg_0_39: 3, fg_40_49: 4, fg_50p: 5, fg_miss: -1,
  d_sack: 1, d_int: 2, d_fum_rec: 2, d_td: 6, d_safe: 2, d_block: 2, d_2pt: 2,
};
const LEAGUES = {
  standard: {
    name: "Standard (12-team, PPR)", teams: 12, budget: 200, scoring: null, draft: "auction",
    start: { QB: 1, RB: 2, WR: 2, TE: 1, K: 1, DEF: 1 }, flex: 1, flexPos: ["RB", "WR", "TE"], bench: 6,
    playoff_spots: 6, reg_weeks: 14,
  },
  johnnyv: {
    name: "Johnny V League (auction)", teams: 10, budget: 200, scoring: JOHNNYV_SCORING, draft: "auction",
    start: { QB: 1, RB: 2, WR: 3, TE: 0, K: 1, DEF: 1 }, flex: 2, flexPos: ["RB", "WR", "TE"], bench: 7,
    playoff_spots: 4, reg_weeks: 15,
  },
  dennishsieh: {
    name: "Dennis Hsieh League (snake)", teams: 12, budget: 200, scoring: DENNIS_SCORING, draft: "snake",
    start: { QB: 1, RB: 2, WR: 2, TE: 1, K: 1, DEF: 1 }, flex: 1, flexPos: ["RB", "WR", "TE"], bench: 6,
    playoff_spots: 6, reg_weeks: 14,
  },
};

// Apply a league's scoring app-wide (so projections/rankings match that league).
function applyLeagueScoring(cfg) {
  if (!cfg) return;
  state.settings = Object.assign({}, cfg.scoring || DEFAULTS);
  saveSettings(); recomputeAll(); render(); syncPresetButtons();
  const body = document.getElementById("scoring-body");
  if (body && body.children.length) buildScoringPanel();
}

// Header league switcher: flip the entire app to a league's scoring in one click.
function populateLeagueSwitch() {
  const sel = document.getElementById("league-switch");
  if (!sel) return;
  sel.innerHTML = Object.entries(LEAGUES).map(([k, v]) => `<option value="${k}">${esc(v.name)}</option>`).join("") + '<option value="custom">Custom scoring</option>';
  syncLeagueSwitch();
}
function syncLeagueSwitch() {
  const sel = document.getElementById("league-switch");
  if (!sel) return;
  const cur = JSON.stringify(state.settings);
  let match = "custom";
  for (const [k, v] of Object.entries(LEAGUES)) {
    if (JSON.stringify(v.scoring || DEFAULTS) === cur) { match = k; break; }
  }
  sel.value = match;
}
function switchLeague(k) {
  const lg = LEAGUES[k];
  if (!lg) return;
  applyLeagueScoring(lg);
  state.myLeague = k; localStorage.setItem("ff_myleague", k);
  state.draftLeague = k;
  if (state.draftRoom) { state.draftRoom.league = k; saveDraftRoom(); }
  const rerender = { myteam: renderMyTeam, waivers: renderWaivers, analyze: renderAnalyze, draft: renderDraft, draftroom: renderDraftRoom };
  if (rerender[state.view]) rerender[state.view]();
  toast("Switched to " + lg.name + " scoring.");
}

// ===========================================================================
// My Team + Start/Sit
// ===========================================================================
function toggleMyTeam(id, btnEl) {
  if (state.myTeam.has(id)) state.myTeam.delete(id);
  else state.myTeam.add(id);
  saveMyTeam();
  render();
  if (btnEl) {
    const on = state.myTeam.has(id);
    btnEl.textContent = on ? "★ On my team — remove" : "☆ Add to my team";
    btnEl.classList.toggle("primary", on);
  }
  if (state.view === "myteam") renderMyTeam();
}

function optimalLineup(players, cfg) {
  // players: array with _proj; greedily fill required slots by next_game, then flex.
  const byPos = { QB: [], RB: [], WR: [], TE: [], K: [], DEF: [] };
  players.forEach((p) => { if (byPos[p.position]) byPos[p.position].push(p); });
  for (const pos in byPos) byPos[pos].sort((a, b) => b._proj.next_game - a._proj.next_game);
  const used = new Set();
  const starters = [];
  const take = (pos, slot) => {
    const p = byPos[pos].find((x) => !used.has(x.id));
    if (p) { used.add(p.id); starters.push({ slot, player: p }); }
    else starters.push({ slot, player: null });
  };
  const SLOT_LABEL = { QB: "QB", RB: "RB", WR: "WR", TE: "TE", K: "K", DEF: "DEF" };
  ["QB", "RB", "WR", "TE", "K", "DEF"].forEach((pos) => {
    for (let i = 0; i < (cfg.start[pos] || 0); i++) take(pos, SLOT_LABEL[pos]);
  });
  for (let i = 0; i < (cfg.flex || 0); i++) {
    let best = null;
    cfg.flexPos.forEach((pos) => {
      const p = byPos[pos].find((x) => !used.has(x.id));
      if (p && (!best || p._proj.next_game > best._proj.next_game)) best = p;
    });
    if (best) { used.add(best.id); starters.push({ slot: "FLEX", player: best }); }
    else starters.push({ slot: "FLEX", player: null });
  }
  const bench = players.filter((p) => !used.has(p.id)).sort((a, b) => b._proj.next_game - a._proj.next_game);
  return { starters, bench };
}

function lineupRow(slot, p, bench) {
  if (!p) return `<tr class="${bench ? "bench" : ""}"><td class="left"><span class="slot">${slot}</span></td><td class="left muted">— empty —</td><td></td><td></td><td></td></tr>`;
  const mu = p.matchup && p.matchup.next && p.matchup.next.opp ? "vs " + esc(p.matchup.next.opp) : "—";
  return `<tr class="${bench ? "bench" : ""}" style="cursor:pointer" onclick="openPlayer('${esc(p.id)}')">
    <td class="left"><span class="slot">${slot}</span></td>
    <td class="left">${esc(p.name)} <span class="pos-badge pos-${p.position}">${p.position}</span></td>
    <td>${esc(p.team)}</td>
    <td>${mu}</td>
    <td><strong>${fmt(p._proj.next_game)}</strong></td>
  </tr>`;
}

function pasteRosterBox() {
  return `<div class="hint" style="margin-bottom:1rem">
    <strong>📋 Load your roster:</strong> paste player names (one per line or comma-separated) and click Load — great right after your draft.
    <textarea id="roster-paste" rows="4" placeholder="Jahmyr Gibbs&#10;CeeDee Lamb&#10;Patrick Mahomes&#10;…"></textarea>
    <div style="margin-top:.5rem;display:flex;gap:.5rem;flex-wrap:wrap">
      <button id="load-roster" class="btn primary">Load into My Team</button>
      <button id="add-roster" class="btn">Add (keep current)</button>
      <button id="clear-roster" class="btn">Clear team</button>
    </div></div>`;
}

function wireRosterPaste() {
  const load = (replace) => {
    const txt = document.getElementById("roster-paste").value;
    if (!txt.trim()) { toast("Paste some player names first."); return; }
    const { matched, unmatched } = matchRosterNames(txt);
    if (replace) state.myTeam = new Set();
    matched.forEach((p) => state.myTeam.add(p.id));
    saveMyTeam(); render(); renderMyTeam();
    toast(`Loaded ${matched.length} player(s)${unmatched.length ? " · unmatched: " + unmatched.slice(0, 5).join(", ") + (unmatched.length > 5 ? "…" : "") : ""}`);
  };
  const lr = document.getElementById("load-roster");
  if (lr) lr.addEventListener("click", () => load(true));
  const ar = document.getElementById("add-roster");
  if (ar) ar.addEventListener("click", () => load(false));
  const cr = document.getElementById("clear-roster");
  if (cr) cr.addEventListener("click", () => { state.myTeam = new Set(); saveMyTeam(); render(); renderMyTeam(); toast("Team cleared."); });
}

// Find upcoming weeks where roster starters are on bye and you're short a position.
function byePlanner() {
  const roster = state.players.filter((p) => state.myTeam.has(p.id));
  const cfg = LEAGUES[state.myLeague] || LEAGUES.standard;
  const start = cfg.start || {};
  const startWk = state.context && state.context.season_type === "regular" ? state.context.upcoming_week : 1;
  const weeks = [];
  for (let w = startWk; w <= 18; w++) {
    const onBye = roster.filter((p) => p.bye_week === w);
    if (!onBye.length) continue;
    const byePositions = new Set(onBye.map((p) => p.position));
    const holes = [];
    for (const pos of ["QB", "RB", "WR", "TE", "K", "DEF"]) {
      const need = start[pos] || 0;
      if (!need || !byePositions.has(pos)) continue;  // only holes the bye actually causes
      const healthy = roster.filter((p) => p.position === pos && p.bye_week !== w).length;
      if (healthy < need) holes.push({ pos, deficit: need - healthy });
    }
    weeks.push({ week: w, onBye, holes });
  }
  return weeks;
}
function byeFillins(week, pos) {
  const mine = state.myTeam;
  return state.players
    .filter((p) => !mine.has(p.id) && p.position === pos && p.bye_week !== week && (p._valueRank > 90 || p.trending > 0))
    .sort((a, b) => b._proj.ros_ppg - a._proj.ros_ppg).slice(0, 4);
}
function renderByePlanner() {
  const weeks = byePlanner();
  if (!weeks.length) return "";
  const rows = weeks.map((w) => {
    const outList = w.onBye.map((p) => `${esc(p.name)} <span class="pos-badge pos-${p.position}">${p.position}</span>`).join(", ");
    const holeHtml = w.holes.length ? w.holes.map((h) => {
      const fills = byeFillins(w.week, h.pos);
      const fillHtml = fills.length
        ? fills.map((f) => `<span class="link" onclick="openPlayer('${esc(f.id)}')">${esc(f.name)}</span> <span class="muted">(${fmt(f._proj.ros_ppg)}/g${f.trending > 0 ? " 🔥" : ""})</span>`).join(", ")
        : '<span class="muted">no strong options</span>';
      return `<div class="wrec-why"><span class="m-bad">Need ${h.deficit} ${h.pos}</span> → fill-ins: ${fillHtml}</div>`;
    }).join("") : '<div class="muted wrec-why">Covered by your bench ✔</div>';
    return `<div class="wrec"><div class="wrec-line"><span class="slot">Wk ${w.week}</span> ${outList}</div>${holeHtml}</div>`;
  }).join("");
  return `<h3>🗓️ Bye-week planner</h3><div class="hint" style="padding:.7rem .9rem">${rows}
    <div class="muted" style="font-size:.72rem;margin-top:.5rem">Weeks with roster byes; a red flag means you're short at that position that week. Fill-ins are the best available players not on bye that week (ranked by per-game output).</div></div>`;
}

function renderMyTeam() {
  const host = document.getElementById("myteam-panel");
  const roster = state.players.filter((p) => state.myTeam.has(p.id));
  if (!roster.length) {
    host.innerHTML = `<h2>My Team</h2>${pasteRosterBox()}<div class="hint">No players yet. Paste your roster above, or go to <strong>Rankings</strong> and click the ☆ star next to any player. Then your optimal weekly lineup appears here.</div>`;
    wireRosterPaste();
    return;
  }
  const cfgKey = state.myLeague || "standard";
  const cfg = LEAGUES[cfgKey];
  const { starters, bench } = optimalLineup(roster, cfg);
  const startPts = starters.reduce((s, x) => s + (x.player ? x.player._proj.next_game : 0), 0);
  const wk = state.context && state.context.season_type === "regular" ? "Week " + state.context.upcoming_week : "Week 1";
  const leagueOpts = Object.entries(LEAGUES).map(([k, v]) => `<option value="${k}" ${k === cfgKey ? "selected" : ""}>${esc(v.name)}</option>`).join("");
  // bye conflicts this week not computed (no live week); show bye column instead
  host.innerHTML = `
    <h2>My Team — Optimal Lineup (${wk})</h2>
    ${pasteRosterBox()}
    <div class="league-bar">
      <label class="muted">League (lineup + scoring):
        <select id="myteam-league">${leagueOpts}</select></label>
      ${state.yahoo.connected ? '<button id="sync-yahoo-roster" class="btn">🔄 Sync roster from Yahoo</button>' : ""}
      <button id="roster-news-btn" class="btn">📰 Injury &amp; news report</button>
      <span class="muted">${roster.length} players on roster · scoring: ${esc(scoringLabel())}</span>
    </div>
    <table class="lineup"><thead><tr><th class="left">Slot</th><th class="left">Player</th><th>Tm</th><th>Matchup</th><th>Proj</th></tr></thead>
      <tbody>
        ${starters.map((x) => lineupRow(x.slot, x.player, false)).join("")}
        <tr class="totrow"><td class="left" colspan="4">Projected starting points (${wk})</td><td>${fmt(startPts)}</td></tr>
      </tbody></table>
    <h3>Bench / Sit</h3>
    <table class="lineup"><tbody>${bench.length ? bench.map((p) => lineupRow("BN", p, true)).join("") : '<tr><td class="muted">No bench players.</td></tr>'}</tbody></table>
    <div class="muted" style="margin-top:.6rem;font-size:.75rem">Optimal lineup by matchup-adjusted next-game projections. Star/unstar players in Rankings to change your roster.</div>

    ${renderByePlanner()}
    <div id="roster-news"></div>
  `;
  wireRosterPaste();
  const rnBtn = document.getElementById("roster-news-btn");
  if (rnBtn) rnBtn.addEventListener("click", loadRosterNews);
  const syncBtn = document.getElementById("sync-yahoo-roster");
  if (syncBtn) syncBtn.addEventListener("click", async () => {
    const lk = yahooKeyFor(cfgKey);
    if (!lk) { toast("No Yahoo league available."); return; }
    syncBtn.disabled = true; syncBtn.textContent = "Syncing…";
    try {
      const j = await (await fetch("/api/yahoo/roster", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league_key: lk }) })).json();
      if (!j.ok) { toast("Sync failed: " + (j.error || "")); return; }
      const names = (j.roster || []).map((r) => r.name).join("\n");
      const { matched, unmatched } = matchRosterNames(names);
      state.myTeam = new Set(matched.map((p) => p.id)); saveMyTeam();
      render(); renderMyTeam();
      toast(`Synced ${matched.length} from Yahoo${unmatched.length ? " · " + unmatched.length + " unmatched" : ""}.`);
    } catch (e) { toast("Sync failed."); }
    finally { syncBtn.disabled = false; syncBtn.textContent = "🔄 Sync roster from Yahoo"; }
  });
  const sel = document.getElementById("myteam-league");
  if (sel) sel.addEventListener("change", (e) => {
    state.myLeague = e.target.value;
    localStorage.setItem("ff_myleague", e.target.value);
    const lg = LEAGUES[e.target.value];
    if (lg && lg.scoring) { applyLeagueScoring(lg); toast("Lineup + scoring set to " + lg.name); }
    renderMyTeam();
  });
}

async function loadRosterNews() {
  const box = document.getElementById("roster-news");
  if (!box) return;
  box.innerHTML = '<div class="section-h">Injury &amp; news report</div><p class="muted">Loading…</p>';
  try {
    const j = await (await fetch("/api/roster_news", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [...state.myTeam] }) })).json();
    const players = (j.players || []).sort((a, b) => (b.injury.is_risk ? 1 : 0) - (a.injury.is_risk ? 1 : 0));
    box.innerHTML = `<div class="section-h">Injury &amp; news report</div>` + (players.length ? players.map((p) => `
      <div class="wrec">
        <div class="wrec-line"><span class="pos-badge pos-${p.position}">${p.position}</span>
          <strong class="link" onclick="openPlayer('${esc(p.id)}')">${esc(p.name)}</strong>
          ${statusBadge(p.injury)} ${p.injury.body_part ? '<span class="muted">' + esc(p.injury.body_part) + "</span>" : ""}</div>
        ${p.injury.detail ? `<div class="muted wrec-why">${esc(p.injury.detail)}</div>` : ""}
        ${(p.news || []).slice(0, 2).map((n) => `<div class="wrec-why">📰 <a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a></div>`).join("")}
        ${!p.injury.detail && !(p.news || []).length ? '<div class="muted wrec-why">No news — clear.</div>' : ""}
      </div>`).join("") : '<p class="muted">Add players to your roster first.</p>');
  } catch (e) { box.innerHTML = '<div class="section-h">Injury &amp; news report</div><p class="m-bad">Failed to load.</p>'; }
}

// ===========================================================================
// Waivers & Streamers
// ===========================================================================
function waiverRow(p) {
  const mu = p.matchup && p.matchup.next && p.matchup.next.opp ? matchDesc(p.matchup.next.rank) : null;
  const muTxt = p.matchup && p.matchup.next && p.matchup.next.opp ? `vs ${esc(p.matchup.next.opp)}${mu ? " · " + mu.txt : ""}` : "";
  return `<div class="wrow" onclick="openPlayer('${esc(p.id)}')">
    <span>${esc(p.name)} <span class="pos-badge pos-${p.position}">${p.position}</span> <span class="muted">${esc(p.team)}</span></span>
    <span class="r">${fmt(p._proj.next_game)} proj<br>${muTxt}${p.trending > 0 ? " · 🔥" + p.trending.toLocaleString() : ""}</span>
  </div>`;
}

// Recommend waiver pickups for the user's roster: for each available upgrade,
// pair it with the weakest same-position player it would replace, with rationale.
function recommendWaivers(faPool) {
  const roster = state.players.filter((p) => state.myTeam.has(p.id));
  if (!roster.length) return { roster: [], recs: [] };
  const rosterIds = new Set(roster.map((p) => p.id));
  // Candidate pool: real Yahoo free agents if pulled, else a waiver-caliber heuristic.
  let candidates;
  if (faPool && faPool.length) {
    const byName = {};
    for (const p of state.players) byName[normName(p.name)] = p;
    candidates = faPool.map((fa) => byName[normName(fa.name)]).filter(Boolean);
  } else {
    candidates = state.players.filter((p) => (p._valueRank > 110 || p.trending > 0));
  }
  const recs = [];
  for (const cand of candidates) {
    if (rosterIds.has(cand.id)) continue;
    const samePos = roster.filter((r) => r.position === cand.position);
    if (!samePos.length) continue;
    const drop = samePos.reduce((a, b) => (a._value < b._value ? a : b));
    const gain = cand._value - drop._value;
    if (gain > 6) recs.push({ cand, drop, gain });
  }
  // keep the single best candidate per drop-target, then sort by gain
  const bestByDrop = {};
  for (const r of recs.sort((a, b) => b.gain - a.gain)) {
    if (!bestByDrop[r.drop.id]) bestByDrop[r.drop.id] = r;
  }
  return { roster, recs: Object.values(bestByDrop).sort((a, b) => b.gain - a.gain).slice(0, 12) };
}

function waiverRationale(cand, drop) {
  const b = [];
  if (cand.matchup && cand.matchup.ros) {
    const m = cand.matchup.ros.mult;
    if (m >= 1.03) b.push("easy remaining schedule");
    else if (m <= 0.97) b.push("tough schedule but still an upgrade");
  }
  if (cand.position === "RB" && cand.workload && cand.workload.share_pct >= 45)
    b.push(`${cand.workload.label.toLowerCase()} (${cand.workload.share_pct}% of carries)`);
  if (cand._proj.durability >= 0.9) b.push("durable");
  if (cand.consistency) {
    if (cand.consistency.rating === "Steady") b.push("steady week-to-week");
    if (cand.consistency.ceiling) b.push(`ceiling ~${cand.consistency.ceiling}`);
  }
  if (cand.trending > 0) b.push(`trending (+${cand.trending.toLocaleString()} adds/24h)`);
  if (drop._proj.durability < 0.8) b.push(`${drop.name} is injury-prone`);
  if (drop.injury && drop.injury.is_risk) b.push(`${drop.name}: ${drop.injury.status}`);
  if (drop.bye_week && cand.bye_week && drop.bye_week !== cand.bye_week) b.push("different bye (helps coverage)");
  return b.slice(0, 4);
}

function renderRecsCard(faPool) {
  const { roster, recs } = recommendWaivers(faPool);
  if (!roster.length) {
    return `<div class="wcard" id="recs-card" style="grid-column:1/-1"><h3>🎯 Recommended pickups for your team</h3>
      <p class="muted">Add your roster first (My Team tab → paste roster, or star players) and I'll suggest targeted upgrades — who to add, who to drop, and why.</p></div>`;
  }
  const rows = recs.length ? recs.map((r) => `<div class="wrec">
      <div class="wrec-line">
        <span class="pos-badge pos-${r.cand.position}">${r.cand.position}</span>
        <strong class="link" onclick="openPlayer('${esc(r.cand.id)}')">${esc(r.cand.name)}</strong>
        <span class="muted">${esc(r.cand.team)}</span>
        <span class="arrow">▶ drop</span>
        <span class="link" onclick="openPlayer('${esc(r.drop.id)}')">${esc(r.drop.name)}</span>
        <span class="val-pos">+${fmt(r.gain, 0)} value</span>
      </div>
      <div class="muted wrec-why">${waiverRationale(r.cand, r.drop).map(esc).join(" · ")}</div>
    </div>`).join("") : '<p class="muted">No clear upgrades over your current roster right now.</p>';
  return `<div class="wcard" id="recs-card" style="grid-column:1/-1">
    <h3>🎯 Recommended pickups for your team</h3>
    ${rows}
    <div class="muted" style="font-size:.72rem;margin-top:.4rem">Ranked by rest-of-season value gained vs the weakest same-position player on your roster${faPool && faPool.length ? " (from your Yahoo free-agent pool)" : " (available-pool estimate — pull Yahoo free agents for the exact list)"}. Scoring: ${esc(scoringLabel())}.</div>
  </div>`;
}

function renderWaivers() {
  const host = document.getElementById("waivers-panel");
  const notMine = (p) => !state.myTeam.has(p.id);
  const trending = state.players.filter((p) => p.trending > 0 && notMine(p))
    .sort((a, b) => b.trending - a.trending).slice(0, 15);
  // Streamers = waiver-caliber (outside typical starter rank) best next-game matchup.
  const streamThresh = { QB: 12, TE: 12, K: 10, DEF: 10, RB: 40, WR: 50 };
  const streamers = (pos) => state.players
    .filter((p) => p.position === pos && notMine(p) && (p._posRank || 999) > streamThresh[pos])
    .sort((a, b) => b._proj.next_game - a._proj.next_game).slice(0, 5);
  const card = (title, arr) => `<div class="wcard"><h3>${title}</h3>${arr.length ? arr.map(waiverRow).join("") : '<p class="muted">None.</p>'}</div>`;

  let yahooBlock = "";
  if (state.yahoo.connected && state.yahoo.leagues.length) {
    const opts = state.yahoo.leagues.map((l) => `<option value="${esc(l.league_key)}">${esc(l.name)}</option>`).join("");
    yahooBlock = `<div class="wcard" style="grid-column:1/-1">
      <h3>🟣 Yahoo waiver wire — your league's actual free agents</h3>
      <div class="league-bar">
        <label class="muted">League: <select id="wv-league">${opts}</select></label>
        <button id="wv-fa" class="btn primary">Pull free agents</button>
        <button id="wv-tx" class="btn">Recent transactions</button>
      </div>
      <div id="wv-fa-out"></div><div id="wv-tx-out"></div></div>`;
  }

  host.innerHTML = `
    <h2>Waivers &amp; Streamers</h2>
    <p class="muted">Trending adds across Sleeper (last 24h) and the best streaming options by this week's matchup, excluding your team.${state.yahoo.connected ? " Plus your Yahoo league's real free-agent pool, ranked by your scoring." : ""}</p>
    <div class="wcards">
      ${renderRecsCard(null)}
      ${yahooBlock}
      ${card("🔥 Trending adds", trending)}
      ${card("QB streamers", streamers("QB"))}
      ${card("TE streamers", streamers("TE"))}
      ${card("DEF streamers", streamers("DEF"))}
      ${card("K streamers", streamers("K"))}
      ${card("Deep RB fliers", streamers("RB"))}
      ${card("Deep WR fliers", streamers("WR"))}
    </div>`;

  const faBtn = document.getElementById("wv-fa");
  if (faBtn) faBtn.addEventListener("click", () => pullYahooFreeAgents(document.getElementById("wv-league").value));
  const txBtn = document.getElementById("wv-tx");
  if (txBtn) txBtn.addEventListener("click", () => pullYahooTransactions(document.getElementById("wv-league").value));
}

async function pullYahooFreeAgents(leagueKey) {
  const out = document.getElementById("wv-fa-out");
  out.innerHTML = '<p class="muted">Pulling free agents…</p>';
  try {
    const j = await (await fetch("/api/yahoo/freeagents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league_key: leagueKey }) })).json();
    if (!j.ok) { out.innerHTML = `<p class="m-bad">${esc(j.error || "Failed")}</p>`; return; }
    const byNameOnly = {};
    for (const p of state.players) (byNameOnly[normName(p.name)] = byNameOnly[normName(p.name)] || []).push(p);
    const rows = (j.players || []).map((fa) => {
      const p = (byNameOnly[normName(fa.name)] || [])[0];
      return { fa, p, ros: p ? p._proj.ros_total : -1, val: p ? p._value : -9999 };
    }).sort((a, b) => b.ros - a.ros).slice(0, 40);
    out.innerHTML = `<table class="lineup"><thead><tr><th class="left">Free agent</th><th>Pos</th><th>Tm</th><th>Own%</th><th>Next</th><th>ROS</th><th>Value</th><th>Matchup</th></tr></thead><tbody>
      ${rows.map((r) => {
        const p = r.p;
        const mu = p && p.matchup && p.matchup.next && p.matchup.next.opp ? "vs " + esc(p.matchup.next.opp) : "";
        return `<tr ${p ? `style="cursor:pointer" onclick="openPlayer('${esc(p.id)}')"` : ""}>
          <td class="left">${esc(r.fa.name)}</td><td>${esc(r.fa.pos || (p ? p.position : ""))}</td>
          <td>${esc(r.fa.team || (p ? p.team : ""))}</td><td>${r.fa.pct_owned != null ? r.fa.pct_owned + "%" : "—"}</td>
          <td>${p ? fmt(p._proj.next_game) : "—"}</td><td>${p ? fmt(p._proj.ros_total, 0) : "—"}</td>
          <td class="${p && p._value >= 0 ? "val-pos" : "val-neg"}">${p ? (p._value >= 0 ? "+" : "") + fmt(p._value, 0) : "—"}</td>
          <td>${mu}</td></tr>`;
      }).join("")}</tbody></table>
      <div class="muted" style="font-size:.72rem;margin-top:.3rem">Yahoo's actual available players, ranked by your scoring's rest-of-season projection.</div>`;
    // refresh the recommendations card using the real free-agent pool
    const rc = document.getElementById("recs-card");
    if (rc) rc.outerHTML = renderRecsCard(j.players || []);
  } catch (e) { out.innerHTML = '<p class="m-bad">Pull failed.</p>'; }
}

async function pullYahooTransactions(leagueKey) {
  const out = document.getElementById("wv-tx-out");
  out.innerHTML = '<p class="muted">Loading transactions…</p>';
  try {
    const j = await (await fetch("/api/yahoo/transactions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league_key: leagueKey }) })).json();
    if (!j.ok) { out.innerHTML = `<p class="m-bad">${esc(j.error || "Failed")}</p>`; return; }
    const tx = j.transactions || [];
    out.innerHTML = `<div class="section-h">Recent transactions</div>` + (tx.length ? tx.map((t) => {
      const when = t.timestamp ? new Date(t.timestamp * 1000).toLocaleDateString() : "";
      const moves = (t.players || []).map((p) => `${p.move === "add" ? "➕" : p.move === "drop" ? "➖" : "↔"} ${esc(p.name)}`).join("  ");
      return `<div class="wrow"><span>${esc(t.type || "move")} ${moves}</span><span class="r">${when}</span></div>`;
    }).join("") : '<p class="muted">No recent transactions.</p>');
  } catch (e) { out.innerHTML = '<p class="m-bad">Failed.</p>'; }
}

// ===========================================================================
// Draft / Auction (server-side optimizer)
// ===========================================================================
// Compute a self-contained draft board under a given scoring (no global mutation).
function boardUnder(scoring) {
  const ctx = state.context;
  const byPos = {};
  for (const p of state.players) {
    const pts = projectPlayer(p, scoring, ctx).ros_total;
    (byPos[p.position] = byPos[p.position] || []).push({ p, pts });
  }
  const out = {};
  for (const pos in byPos) {
    const arr = byPos[pos].sort((a, b) => b.pts - a.pts);
    const n = Math.min(arr.length, pos === "WR" ? 75 : 25);
    const base = arr.slice(0, n).reduce((s, r) => s + r.pts, 0) / (n || 1);
    const top = arr.slice(0, 40), gaps = [];
    for (let i = 1; i < top.length; i++) gaps.push(top[i - 1].pts - top[i].pts);
    const sg = [...gaps].sort((a, b) => a - b);
    const med = sg.length ? sg[Math.floor(sg.length / 2)] : 0;
    const thr = Math.max(6, med * 1.6);
    let tier = 1;
    arr.forEach((r, i) => { if (i > 0 && arr[i - 1].pts - r.pts > thr) tier++; r.tier = tier; r.value = r.pts - base; });
    out[pos] = arr;
  }
  return out;
}

function renderSnakeBoard(cfg) {
  const board = boardUnder(cfg.scoring || DEFAULTS);
  const cols = ["QB", "RB", "WR", "TE", "K", "DEF"].map((pos) => {
    const arr = (board[pos] || []).slice(0, 20);
    let last = null;
    const rows = arr.map((r) => {
      const sep = r.tier !== last ? `<div class="tier-sep">Tier ${r.tier}</div>` : "";
      last = r.tier;
      const adp = r.p._adpDiff != null ? ` · ADP${r.p._adpDiff > 0 ? "+" : ""}${r.p._adpDiff}` : "";
      const star = state.myTeam.has(r.p.id) ? " ★" : "";
      return `${sep}<div class="wrow" onclick="openPlayer('${esc(r.p.id)}')">
        <span>${esc(r.p.name)}${star} <span class="muted">${esc(r.p.team)}</span> ${r.p.injury.is_risk ? statusBadge(r.p.injury) : ""}</span>
        <span class="r">${fmt(r.pts, 0)} · ${r.value >= 0 ? "+" : ""}${fmt(r.value, 0)}${adp}</span></div>`;
    }).join("");
    return `<div class="wcard"><h3>${pos}</h3>${rows || '<p class="muted">—</p>'}</div>`;
  }).join("");
  return `<p class="muted">Snake draft cheat sheet under this league's scoring — players by value with tier breaks (a tier drop = a good spot to trade down / wait). Columns show ROS pts · value · ADP edge.</p><div class="wcards">${cols}</div>`;
}

async function renderDraft() {
  const host = document.getElementById("draft-panel");
  const leagueKey = state.draftLeague || "johnnyv";
  const cfg = LEAGUES[leagueKey];
  const isAuction = cfg.draft !== "snake";
  const leagueOpts = Object.entries(LEAGUES).map(([k, v]) => `<option value="${k}" ${k === leagueKey ? "selected" : ""}>${esc(v.name)}</option>`).join("");
  const startList = Object.entries(cfg.start).filter(([, n]) => n > 0).map(([p, n]) => `${n}${p}`).join(" · ") + (cfg.flex ? ` · ${cfg.flex} FLEX` : "");
  host.innerHTML = `
    <h2>Draft ${isAuction ? "/ Auction Optimizer" : "Board (snake)"}</h2>
    <div class="league-bar">
      <label class="muted">League: <select id="draft-league">${leagueOpts}</select></label>
      ${isAuction ? '<button id="run-optimize" class="btn primary">🧮 Optimize roster</button>' : ""}
      ${isAuction ? '<button id="value-sheet" class="btn">💵 Value sheet</button>' : ""}
      ${cfg.yahoo_key ? '<button id="pull-yahoo" class="btn" title="Fetch this league\'s live Yahoo draft values (AAV/ADP)">↻ Pull Yahoo values</button>' : ""}
    </div>
    <div class="settings-grid">
      <div class="kv"><span>Teams</span>${cfg.teams}</div>
      <div class="kv"><span>Draft type</span>${isAuction ? "Auction ($" + cfg.budget + ")" : "Snake"}</div>
      <div class="kv"><span>Starters</span>${startList}</div>
      <div class="kv"><span>Bench</span>${cfg.bench}</div>
      <div class="kv"><span>Scoring</span>${esc(cfg.name.split(" (")[0])}</div>
      <div class="kv"><span>${isAuction ? "Cost rule" : "Draft"}</span>${isAuction ? "AAV +$5 if >$8" : "Best available by value"}</div>
    </div>
    <div id="draft-result">${isAuction
      ? '<p class="muted">Click <strong>Optimize roster</strong> to build the point-maximizing team under the budget. Uses real Yahoo AAV if <code>yahoo_aav.csv</code> is present, else an estimate.</p>'
      : renderSnakeBoard(cfg)}</div>`;
  document.getElementById("draft-league").addEventListener("change", (e) => { state.draftLeague = e.target.value; renderDraft(); });
  const opt = document.getElementById("run-optimize");
  if (opt) opt.addEventListener("click", runOptimize);
  const vs = document.getElementById("value-sheet");
  if (vs) vs.addEventListener("click", () => loadValueSheet(state.draftLeague || "johnnyv"));
  const pull = document.getElementById("pull-yahoo");
  if (pull) pull.addEventListener("click", async () => {
    pull.disabled = true; pull.textContent = "Pulling…";
    try {
      const j = await (await fetch("/api/yahoo/draftvalues", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league_key: cfg.yahoo_key }) })).json();
      if (j.ok) {
        toast(`Pulled ${j.count} Yahoo values (${j.with_cost} with auction $).`);
        if (isAuction) runOptimize();
      } else toast("Pull failed: " + (j.error || "unknown"));
    } catch (e) { toast("Pull failed."); }
    finally { pull.disabled = false; pull.textContent = "↻ Pull Yahoo values"; }
  });
}

async function loadValueSheet(league) {
  const out = document.getElementById("draft-result");
  out.innerHTML = '<p class="muted">Computing auction dollar values…</p>';
  try {
    const j = await (await fetch("/api/auction_values", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league }) })).json();
    if (j.error) { out.innerHTML = `<p class="m-bad">${esc(j.error)}</p>`; return; }
    state.valueSheet = j; renderValueSheet();
  } catch (e) { out.innerHTML = '<p class="m-bad">Failed.</p>'; }
}
function renderValueSheet() {
  const j = state.valueSheet;
  if (!j) return;
  const out = document.getElementById("draft-result");
  const q = (state.valueSheetQ || "").toLowerCase();
  let players = j.players;
  if (q) players = players.filter((p) => p.name.toLowerCase().includes(q) || (p.team || "").toLowerCase().includes(q));
  players = players.slice(0, 250);
  const yh = j.source === "yahoo";
  out.innerHTML = `
    <div class="settings-grid" style="margin-top:0">
      <div class="kv"><span>Values</span>${yh ? "Model + real Yahoo AAV" : "Model estimate (no Yahoo)"}</div>
      <div class="kv"><span>League</span>${esc(j.league)}</div>
      <div class="kv"><span>Pool</span>$${j.budget} × ${j.teams} teams</div>
    </div>
    <input id="vs-search" type="search" placeholder="Search players…" value="${esc(state.valueSheetQ || "")}" style="max-width:320px" />
    <div class="table-wrap"><table class="lineup"><thead><tr><th class="left">Player</th><th>Pos</th><th>Tm</th><th>Proj</th><th>Max bid</th>${yh ? "<th>Market AAV</th><th>Verdict</th>" : ""}</tr></thead><tbody>
    ${players.map((p) => {
      let verdict = "";
      if (p.market_aav != null) { const d = p.model_value - p.market_aav; verdict = d >= 3 ? '<span class="val-pos">Value</span>' : d <= -3 ? '<span class="val-neg">Pricey</span>' : '<span class="muted">Fair</span>'; }
      return `<tr style="cursor:pointer" onclick="openPlayer('${esc(p.id)}')"><td class="left">${esc(p.name)}</td><td><span class="pos-badge pos-${p.pos}">${p.pos}</span></td><td>${esc(p.team)}</td><td>${p.pts}</td><td><strong>$${p.model_value}</strong></td>${yh ? `<td>$${p.market_aav == null ? "—" : p.market_aav}</td><td>${verdict}</td>` : ""}</tr>`;
    }).join("")}
    </tbody></table></div>
    <div class="muted" style="font-size:.72rem;margin-top:.3rem"><strong>Max bid</strong> = the model's dollar value (value-based drafting: $${j.budget}×${j.teams} distributed by value over replacement, under ${esc(j.league)} scoring). Treat it as your ceiling — don't pay more without a specific reason.${yh ? ' Verdict compares to real Yahoo AAV; "Value" = usually goes for less than it\'s worth.' : " Import your Yahoo AAV (yahoo_aav.csv) to also see market prices."}</div>`;
  const s = document.getElementById("vs-search");
  s.addEventListener("input", (e) => { state.valueSheetQ = e.target.value; renderValueSheet(); const el = document.getElementById("vs-search"); el.focus(); el.setSelectionRange(el.value.length, el.value.length); });
}

async function runOptimize() {
  const btn = document.getElementById("run-optimize");
  const out = document.getElementById("draft-result");
  btn.disabled = true; btn.textContent = "Optimizing…";
  out.innerHTML = '<p class="muted">Crunching projections, auction values, and the budget knapsack…</p>';
  try {
    const res = await fetch("/api/optimize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ league: state.draftLeague || "johnnyv" }),
    });
    const j = await res.json();
    if (j.error) { out.innerHTML = `<p class="m-bad">Error: ${esc(j.error)}</p>`; return; }
    out.innerHTML = renderRoster(j);
  } catch (e) {
    out.innerHTML = '<p class="m-bad">Optimize failed (is the server running?).</p>';
  } finally { btn.disabled = false; btn.textContent = "🧮 Optimize roster"; }
}

function rosterTable(rows) {
  return `<table class="lineup"><thead><tr><th class="left">Slot</th><th class="left">Player</th><th>Tm</th><th>Bye</th><th>Proj</th><th>AAV</th><th>Cost</th></tr></thead><tbody>
    ${rows.map((r) => `<tr style="cursor:pointer" ${r.id ? `onclick="openPlayer('${esc(r.id)}')"` : ""}>
      <td class="left"><span class="slot">${esc(r.slot)}</span></td>
      <td class="left">${esc(r.name)} <span class="pos-badge pos-${r.pos}">${r.pos}</span></td>
      <td>${esc(r.team)}</td><td>${r.bye || "—"}</td>
      <td><strong>${fmt(r.pts, 0)}</strong></td><td>$${r.aav}</td><td>$${r.cost}</td>
    </tr>`).join("")}</tbody></table>`;
}

function renderRoster(j) {
  return `
    <div class="settings-grid" style="margin-top:0">
      <div class="kv"><span>Source</span>${j.aav_source === "yahoo" ? "Real Yahoo AAV" : "Estimated (VBD)"}</div>
      <div class="kv"><span>Total cost</span>$${j.total_cost} / $${j.budget}</div>
      <div class="kv"><span>Starter proj pts</span>${fmt(j.starter_points, 0)}</div>
      <div class="kv"><span>Flex</span>${esc(j.flex_used || "—")}</div>
    </div>
    <h3>Starters</h3>${rosterTable(j.starters)}
    <h3>Bench</h3>${rosterTable(j.bench)}
    <div class="muted" style="margin-top:.6rem;font-size:.75rem">${esc(j.note || "")}</div>`;
}

// ===========================================================================
// Teams
// ===========================================================================
function renderTeamGrid() {
  const grid = document.getElementById("team-grid");
  if (grid.children.length) return; // build once
  const teams = [...new Set(state.players.map((p) => p.team).filter((t) => t && t !== "FA"))].sort();
  grid.innerHTML = teams.map((t) => `<button class="team-btn" data-team="${esc(t)}">${esc(t)}</button>`).join("");
  grid.addEventListener("click", (e) => {
    const b = e.target.closest("[data-team]");
    if (b) {
      grid.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      openTeamPage(b.dataset.team);
    }
  });
}

function teamPosBlock(title, players) {
  if (!players.length) return "";
  const rows = players.map((p) => {
    const mu = p.matchup && p.matchup.next && p.matchup.next.opp ? "vs " + esc(p.matchup.next.opp) : "";
    return `<tr style="cursor:pointer" onclick="openPlayer('${esc(p.id)}')">
      <td class="left">${esc(p.name)}${p.is_rookie ? ' <span class="mini-rookie">R</span>' : ""}${state.myTeam.has(p.id) ? " ★" : ""}</td>
      <td>${statusBadge(p.injury)}</td>
      <td>${mu}</td>
      <td>${fmt(p._proj.next_game)}</td>
      <td><strong>${fmt(p._proj.ros_total, 0)}</strong></td>
      <td class="${p._value >= 0 ? "val-pos" : "val-neg"}">${p._value >= 0 ? "+" : ""}${fmt(p._value, 0)}</td>
    </tr>`;
  }).join("");
  return `<h3>${title}</h3><table class="lineup"><thead><tr><th class="left">Player</th><th>Status</th><th>Next</th><th>Next Pts</th><th>ROS</th><th>Value</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderRbDepth(rbs) {
  if (!rbs.length) return "";
  const ordered = rbs.slice().sort((a, b) =>
    ((b.workload && b.workload.proj_carries) || b._proj.ros_total) - ((a.workload && a.workload.proj_carries) || a._proj.ros_total));
  const starter = ordered[0], nextUp = ordered[1];
  const rows = ordered.map((p, i) => `<div class="sit-row">
    <span>${i === 0 ? "🥇" : i === 1 ? "🥈" : "&nbsp;&nbsp;"} ${esc(p.name)} ${p.injury.is_risk ? statusBadge(p.injury) : ""}</span>
    <span>${p.workload ? esc(p.workload.label) + (p.workload.share_pct != null ? " · " + p.workload.share_pct + "%" : "") : "—"}</span></div>`).join("");
  return `<div class="section-h">Backfield depth &amp; next-man-up</div>
    <div class="sit-card wide">
      ${starter ? `<div style="margin-bottom:.4rem"><strong>Starter:</strong> ${esc(starter.name)} — ${starter.workload ? esc(starter.workload.label) : ""}</div>` : ""}
      ${nextUp ? `<div style="margin-bottom:.5rem"><strong>Next up if starter is injured:</strong> <span class="val-pos">${esc(nextUp.name)}</span> ${nextUp.workload && nextUp.workload.share_pct != null ? "(currently " + nextUp.workload.share_pct + "% of carries)" : ""}</div>` : ""}
      ${rows}
      <div class="muted" style="font-size:.7rem;margin-top:.4rem">Order by projected carry share. The next-man-up is the most likely bump in value if the starter misses time.</div>
    </div>`;
}

async function openTeamPage(abbr) {
  const host = document.getElementById("team-page");
  const tp = state.players.filter((p) => p.team === abbr);
  const byPos = (pos) => tp.filter((p) => p.position === pos).sort((a, b) => b._proj.ros_total - a._proj.ros_total);
  host.innerHTML = `<p class="muted">Loading ${esc(abbr)}…</p>`;
  let meta = {};
  try { meta = await (await fetch("/api/team/" + encodeURIComponent(abbr))).json(); } catch (e) { meta = { abbr }; }

  const defRows = ["QB", "RB", "WR", "TE"].map((pos) => {
    const d = meta.defense && meta.defense[pos];
    if (!d) return "";
    const tough = d.rank <= d.of * 0.33 ? "m-good" : d.rank >= d.of * 0.67 ? "m-bad" : "";
    return `<div class="sit-row"><span>vs ${pos}</span><span class="${tough}">${d.rank}${ord(d.rank)} toughest of ${d.of}</span></div>`;
  }).join("");

  // roster player list for the news filter dropdown
  const rosterPlayers = tp.slice().sort((a, b) => b._proj.ros_total - a._proj.ros_total);
  const playerOpts = rosterPlayers.map((p) => `<option value="${esc(p.name)}">${esc(p.name)} (${p.position})</option>`).join("");

  host.innerHTML = `
    <div class="team-head">
      <h2>${esc(meta.name || abbr)}</h2>
      <div class="muted">${meta.division ? esc(meta.division) + " · " : ""}${meta.coach ? "HC " + esc(meta.coach) + (meta.coach_exp ? " (" + meta.coach_exp + " yrs)" : "") : ""}</div>
    </div>
    <div class="settings-grid">
      <div class="kv"><span>${meta.record_prev_year || ""} record</span>${meta.record_prev || "—"}</div>
      <div class="kv"><span>Points/gm (${meta.record_prev_year || ""})</span>${meta.points_for_pg != null ? meta.points_for_pg : "—"}</div>
      <div class="kv"><span>Pts allowed/gm</span>${meta.points_against_pg != null ? meta.points_against_pg : "—"}</div>
      <div class="kv"><span>Current record</span>${meta.record_now || "—"}</div>
    </div>

    <div class="section-h">Defensive profile <span class="muted" style="text-transform:none">· vs position, from 2025 (no free scheme label)</span></div>
    <div class="sit-card wide">${defRows || '<span class="muted">n/a</span>'}
      <div class="muted" style="font-size:.7rem;margin-top:.4rem">"Toughest" = fewest fantasy points allowed to that position last year. Scheme labels (3-4/4-3, man/zone) aren't available from free data.</div>
    </div>

    ${renderRbDepth(byPos("RB"))}

    <div class="two-col">
      <div>${teamPosBlock("Quarterbacks", byPos("QB"))}${teamPosBlock("Running Backs", byPos("RB"))}${teamPosBlock("Tight Ends", byPos("TE"))}</div>
      <div>${teamPosBlock("Wide Receivers", byPos("WR"))}${teamPosBlock("Kicker", byPos("K"))}${teamPosBlock("Defense", byPos("DEF"))}</div>
    </div>

    <div class="section-h">Team news feed <span class="muted" style="text-transform:none">· local &amp; national (Google News)</span></div>
    <div class="league-bar" style="margin-bottom:.6rem">
      <label class="muted">Filter news: <select id="team-news-filter">
        <option value="">Whole team</option>${playerOpts}
      </select></label>
    </div>
    <div id="team-news">${renderNewsList(meta.news || [])}</div>

    ${renderInsider(meta)}`;

  const nf = document.getElementById("team-news-filter");
  if (nf) nf.addEventListener("change", async (e) => {
    const player = e.target.value;
    const box = document.getElementById("team-news");
    box.innerHTML = '<p class="muted">Loading news…</p>';
    try {
      const q = player ? player + " " + (meta.name || abbr) + " NFL" : (meta.name || abbr) + " NFL injury depth chart";
      const j = await (await fetch("/api/news?q=" + encodeURIComponent(q))).json();
      box.innerHTML = renderNewsList(j.news || []);
    } catch (err) { box.innerHTML = '<p class="muted">Could not load news.</p>'; }
  });
}

function renderNewsList(news) {
  if (!news.length) return '<p class="muted">No recent items.</p>';
  return news.map((n) => `<div class="news-item">
      <a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>
      <div class="desc">${esc(n.source || "")}${n.pub ? " · " + esc(new Date(n.pub).toLocaleDateString()) : ""}</div>
    </div>`).join("");
}

function renderTeams() {
  renderTeamGrid();
  if (!document.getElementById("team-page").innerHTML.trim()) {
    const grid = document.getElementById("team-grid");
    const first = grid.querySelector("[data-team]");
    if (first) { first.classList.add("active"); openTeamPage(first.dataset.team); }
  }
}

// ===========================================================================
// Draft Room — live pick tracker (auction budget/max-bid + snake tiers)
// ===========================================================================
function loadDraftRoom() {
  try {
    const d = JSON.parse(localStorage.getItem("ff_draftroom") || "{}");
    return { league: d.league || "johnnyv", picks: d.picks || {}, search: "", pos: "ALL" };
  } catch (e) { return { league: "johnnyv", picks: {}, search: "", pos: "ALL" }; }
}
function saveDraftRoom() {
  localStorage.setItem("ff_draftroom", JSON.stringify({ league: state.draftRoom.league, picks: state.draftRoom.picks }));
}
function draftPick(id, who, price) {
  state.draftRoom.picks[id] = { who, price: price || 0 };
  if (who === "me") { state.myTeam.add(id); saveMyTeam(); }
  saveDraftRoom(); renderDraftRoom();
}
function undoPick(id) {
  const was = state.draftRoom.picks[id];
  delete state.draftRoom.picks[id];
  if (was && was.who === "me") { state.myTeam.delete(id); saveMyTeam(); }
  saveDraftRoom(); renderDraftRoom();
}
function resetDraftRoom() { state.draftRoom.picks = {}; saveDraftRoom(); renderDraftRoom(); }

// Live Yahoo draft auto-sync: poll draftresults, map picks to our players by
// name, record each as "me" (your team) or "other". Dormant until Yahoo API
// access is granted; the toggle only appears when a Yahoo league is linked.
function startDraftSync(leagueKey) {
  stopDraftSync();
  if (!leagueKey) { toast("No linked Yahoo league for this preset."); return; }
  state.draftSync = { on: true, key: leagueKey, timer: null };
  const nameIndex = () => {
    const idx = {};
    for (const p of state.players) { const k = normName(p.name); (idx[k] = idx[k] || []).push(p); }
    return idx;
  };
  const poll = async () => {
    try {
      const j = await (await fetch("/api/yahoo/draftresults", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league_key: leagueKey }) })).json();
      if (!j.ok) { toast("Yahoo sync: " + (j.error || "unavailable")); stopDraftSync(); if (state.view === "draftroom") renderDraftRoom(); return; }
      const idx = nameIndex();
      let changed = false;
      for (const pk of (j.picks || [])) {
        if (!pk.name) continue;
        const cands = idx[normName(pk.name)] || [];
        const cand = pk.team ? (cands.find((c) => (c.team || "") === pk.team) || cands[0]) : cands[0];
        if (!cand || state.draftRoom.picks[cand.id]) continue;
        state.draftRoom.picks[cand.id] = { who: pk.mine ? "me" : "other", price: parseFloat(pk.cost) || 0 };
        if (pk.mine) state.myTeam.add(cand.id);
        changed = true;
      }
      if (changed) { saveDraftRoom(); saveMyTeam(); if (state.view === "draftroom") renderDraftRoom(); }
    } catch (e) { /* transient — keep polling */ }
  };
  poll();
  state.draftSync.timer = setInterval(poll, 6000);
  toast("Yahoo auto-sync started — drafting live.");
}
function stopDraftSync() {
  if (state.draftSync && state.draftSync.timer) clearInterval(state.draftSync.timer);
  state.draftSync = { on: false };
}

// Recommend the best pick right now: value + positional need + tier scarcity
// + (auction) affordability.
function draftRecommendation(availAll, cfg, myRoster, isAuction, vals, maxBid) {
  const filled = {};
  myRoster.forEach((r) => { filled[r.p.position] = (filled[r.p.position] || 0) + 1; });
  const openStart = {};
  ["QB", "RB", "WR", "TE", "K", "DEF"].forEach((pos) => { openStart[pos] = Math.max(0, (cfg.start[pos] || 0) - (filled[pos] || 0)); });
  const flexUsed = cfg.flexPos.reduce((s, pos) => s + Math.max(0, (filled[pos] || 0) - (cfg.start[pos] || 0)), 0);
  const flexOpen = Math.max(0, (cfg.flex || 0) - flexUsed);
  const tierCount = {};
  availAll.forEach((r) => { const k = r.p.position + ":" + r.tier; tierCount[k] = (tierCount[k] || 0) + 1; });
  let best = null;
  for (const r of availAll) {
    if (isAuction && vals[r.p.id] != null && vals[r.p.id] > maxBid) continue;  // can't afford
    let score = r.value, why = [];
    if (openStart[r.p.position] > 0) { score += 15; why.push("fills your " + r.p.position + " starting need"); }
    else if (cfg.flexPos.includes(r.p.position) && flexOpen > 0) { score += 6; why.push("flex value"); }
    if ((tierCount[r.p.position + ":" + r.tier] || 0) <= 2) { score += 6; why.push("last of " + r.p.position + " tier " + r.tier); }
    if (isAuction && vals[r.p.id] != null) why.push("~$" + vals[r.p.id] + " value");
    else why.push((r.value >= 0 ? "+" : "") + fmt(r.value, 0) + " value");
    if (!best || score > best.score) best = { r, score, why };
  }
  return best;
}

function renderDraftRoom() {
  const host = document.getElementById("draftroom-panel");
  const dr = state.draftRoom;
  const cfg = LEAGUES[dr.league] || LEAGUES.johnnyv;
  const isAuction = cfg.draft !== "snake";
  const vals = (state.drValues && state.drValues[dr.league]) || {};
  const board = boardUnder(cfg.scoring || DEFAULTS);
  let all = [];
  for (const pos in board) for (const r of board[pos]) all.push(r);
  all.sort((a, b) => b.value - a.value);
  const picks = dr.picks;
  const myRoster = all.filter((r) => picks[r.p.id] && picks[r.p.id].who === "me");
  const spent = Object.values(picks).filter((x) => x.who === "me").reduce((s, x) => s + (x.price || 0), 0);
  const totalSlots = Object.values(cfg.start).reduce((a, b) => a + b, 0) + cfg.flex + cfg.bench;
  const filled = myRoster.length, remSlots = totalSlots - filled;
  const remaining = cfg.budget - spent, maxBid = Math.max(1, remaining - Math.max(0, remSlots - 1));
  const need = { QB: 0, RB: 0, WR: 0, TE: 0, K: 0, DEF: 0 }; Object.assign(need, cfg.start);
  const have = {}; myRoster.forEach((r) => { have[r.p.position] = (have[r.p.position] || 0) + 1; });

  const q = (dr.search || "").toLowerCase();
  let avail = all.filter((r) => !picks[r.p.id]);
  if (dr.pos !== "ALL") avail = avail.filter((r) => r.p.position === dr.pos);
  if (q) avail = avail.filter((r) => r.p.name.toLowerCase().includes(q) || (r.p.team || "").toLowerCase().includes(q));
  const shown = avail.slice(0, 150);

  const leagueOpts = Object.entries(LEAGUES).map(([k, v]) => `<option value="${k}" ${k === dr.league ? "selected" : ""}>${esc(v.name)}</option>`).join("");
  const posBtns = ["ALL", "QB", "RB", "WR", "TE", "K", "DEF"].map((p) => `<button class="pos-filter-btn ${dr.pos === p ? "active" : ""}" data-drpos="${p}">${p}</button>`).join("");
  const needHtml = ["QB", "RB", "WR", "TE", "K", "DEF"].filter((p) => need[p]).map((p) => {
    const h = have[p] || 0, n = need[p];
    return `<span class="need-badge ${h >= n ? "ok" : "gap"}">${p} ${h}/${n}</span>`;
  }).join("");
  const rosterHtml = myRoster.length ? myRoster.slice().sort((a, b) => b.value - a.value).map((r) => `<div class="wrow" onclick="undoPick('${esc(r.p.id)}')" title="click to undo">
      <span><span class="pos-badge pos-${r.p.position}">${r.p.position}</span> ${esc(r.p.name)}</span>
      <span class="r">${isAuction ? "$" + (picks[r.p.id].price || 0) + " " : ""}↩</span></div>`).join("") : '<p class="muted">No picks yet.</p>';

  const availRows = shown.map((r, i) => {
    const id = r.p.id;
    const tierBreak = (i > 0 && shown[i - 1].tier !== r.tier && dr.pos !== "ALL") ? `<tr><td colspan="5" class="tier-sep">Tier ${r.tier}</td></tr>` : "";
    const actions = isAuction
      ? `<input class="dr-bid" id="bid-${esc(id)}" type="number" min="1" placeholder="$" /> <button class="btn dr-btn" data-act="me" data-id="${esc(id)}">Mine</button> <button class="btn dr-btn" data-act="other" data-id="${esc(id)}">Sold</button>`
      : `<button class="btn dr-btn" data-act="me" data-id="${esc(id)}">Mine</button> <button class="btn dr-btn" data-act="other" data-id="${esc(id)}">Taken</button>`;
    return `${tierBreak}<tr>
      <td class="left"><span class="pos-badge pos-${r.p.position}">${r.p.position}</span> <span class="link" onclick="openPlayer('${esc(id)}')">${esc(r.p.name)}</span> <span class="muted">${esc(r.p.team)}</span></td>
      <td>T${r.tier}</td><td>${fmt(r.pts, 0)}</td><td class="${r.value >= 0 ? "val-pos" : "val-neg"}">${r.value >= 0 ? "+" : ""}${fmt(r.value, 0)}</td>
      ${isAuction ? `<td>${vals[id] != null ? "<strong>$" + vals[id] + "</strong>" : ""}</td>` : ""}
      <td class="dr-actions">${actions}</td></tr>`;
  }).join("");

  const availAll = all.filter((r) => !picks[r.p.id]);
  const rec = myRoster.length || Object.keys(picks).length ? draftRecommendation(availAll, cfg, myRoster, isAuction, vals, maxBid) : (availAll[0] ? { r: availAll[0], why: ["top overall value"] } : null);
  const recHtml = rec ? `<div class="rec-banner">🎯 <strong>Recommended pick:</strong> <span class="link" onclick="openPlayer('${esc(rec.r.p.id)}')">${esc(rec.r.p.name)}</span> <span class="pos-badge pos-${rec.r.p.position}">${rec.r.p.position}</span> <span class="muted">${esc(rec.r.p.team)}</span> — ${rec.why.join(" · ")}${isAuction && vals[rec.r.p.id] != null ? " · max bid $" + vals[rec.r.p.id] : ""}</div>` : "";

  const wasSearch = document.activeElement && document.activeElement.id === "dr-search";
  host.innerHTML = `
    <h2>Draft Room <span class="muted" style="font-size:.8rem">· live pick tracker</span></h2>
    ${recHtml}
    <div class="league-bar">
      <label class="muted">League: <select id="dr-league">${leagueOpts}</select></label>
      <button id="dr-reset" class="btn">Reset draft</button>
      ${state.yahoo && state.yahoo.connected && yahooKeyFor(dr.league) ? `<button id="dr-sync" class="btn ${state.draftSync && state.draftSync.on ? "sync-on" : ""}">${state.draftSync && state.draftSync.on ? "🟢 Yahoo auto-sync ON — stop" : "🔴 Auto-sync from Yahoo draft"}</button>` : ""}
    </div>
    <div class="settings-grid">
      ${isAuction ? `<div class="kv"><span>Budget left</span>$${remaining} / $${cfg.budget}</div><div class="kv"><span>Max bid</span>$${maxBid}</div>` : ""}
      <div class="kv"><span>My picks</span>${filled} / ${totalSlots}</div>
      <div class="kv"><span>Type</span>${isAuction ? "Auction" : "Snake"}</div>
    </div>
    <div class="dr-grid">
      <div class="dr-avail">
        <div class="controls" style="position:static;padding:.4rem 0;border:none">
          <input id="dr-search" type="search" placeholder="Search available…" value="${esc(dr.search)}" />
          <div class="pos-filter">${posBtns}</div>
        </div>
        <div class="table-wrap"><table class="lineup"><thead><tr><th class="left">Best available (${avail.length})</th><th>Tier</th><th>Pts</th><th>Value</th>${isAuction ? "<th>Max $</th>" : ""}<th>Draft</th></tr></thead>
          <tbody>${availRows || '<tr><td class="muted">None.</td></tr>'}</tbody></table></div>
      </div>
      <div class="dr-roster">
        <h3>My roster</h3>
        <div class="dr-needs">${needHtml}</div>
        ${rosterHtml}
      </div>
    </div>
    <div class="muted" style="font-size:.72rem;margin-top:.5rem">Click <b>Mine</b> when you draft a player (auction: type the price first), <b>${isAuction ? "Sold" : "Taken"}</b> when someone else does. Click a rostered player to undo. Your "Mine" picks flow into My Team / Start-Sit. Saved locally — safe to refresh.</div>`;

  document.getElementById("dr-league").addEventListener("change", (e) => {
    state.draftRoom.league = e.target.value; saveDraftRoom();
    const lg = LEAGUES[e.target.value]; if (lg && lg.scoring) applyLeagueScoring(lg);
    renderDraftRoom();
  });
  document.getElementById("dr-reset").addEventListener("click", () => { if (confirm("Reset all draft picks?")) resetDraftRoom(); });
  const syncBtn = document.getElementById("dr-sync");
  if (syncBtn) syncBtn.addEventListener("click", () => {
    if (state.draftSync && state.draftSync.on) stopDraftSync();
    else startDraftSync(yahooKeyFor(dr.league));
    renderDraftRoom();
  });
  const s = document.getElementById("dr-search");
  s.addEventListener("input", (e) => { state.draftRoom.search = e.target.value; renderDraftRoom(); });
  if (wasSearch) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  host.querySelectorAll("[data-drpos]").forEach((b) => b.addEventListener("click", () => { state.draftRoom.pos = b.dataset.drpos; renderDraftRoom(); }));
  host.querySelectorAll(".dr-btn").forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.id, who = b.dataset.act;
    let price = 0;
    if (isAuction) {
      const inp = document.getElementById("bid-" + id);
      price = inp ? parseFloat(inp.value) || 0 : 0;
      if (who === "me" && !price) { toast("Enter the price you paid first."); if (inp) inp.focus(); return; }
    }
    draftPick(id, who, price);
  }));
  if (isAuction && !vals.__loaded && !(state.drValues && state.drValues[dr.league])) loadDrValues(dr.league);
}

async function loadDrValues(league) {
  try {
    const j = await (await fetch("/api/auction_values", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league }) })).json();
    const map = { __loaded: true };
    (j.players || []).forEach((p) => { map[p.id] = p.model_value; });
    state.drValues = state.drValues || {};
    state.drValues[league] = map;
    if (state.view === "draftroom" && state.draftRoom.league === league) renderDraftRoom();
  } catch (e) { /* leave $Val blank */ }
}

// ===========================================================================
// Analyze: Trade Analyzer + Playoff Strength of Schedule
// ===========================================================================
function playerById(id) { return state.players.find((p) => p.id === id); }
function tradeSum(set) {
  let v = 0, pts = 0;
  set.forEach((id) => { const p = playerById(id); if (p) { v += p._value; pts += p._proj.ros_total; } });
  return { v, pts };
}
function tradeChip(id, side) {
  const p = playerById(id);
  if (!p) return "";
  return `<span class="chip" onclick="tradeRemove('${esc(id)}','${side}')"><span class="pos-badge pos-${p.position}">${p.position}</span> ${esc(p.name)} <span class="${p._value >= 0 ? "val-pos" : "val-neg"}">${p._value >= 0 ? "+" : ""}${fmt(p._value, 0)}</span> ✕</span>`;
}
function tradeAdd(id, side) {
  (side === "give" ? state.trade.get : state.trade.give).delete(id);
  state.trade[side].add(id);
  renderAnalyze();
}
function tradeRemove(id, side) { state.trade[side].delete(id); renderAnalyze(); }
function tradeClear() { state.trade.give = new Set(); state.trade.get = new Set(); renderAnalyze(); }

function renderTrade() {
  const t = state.trade, q = (t.q || "").toLowerCase();
  const give = tradeSum(t.give), get = tradeSum(t.get);
  const diff = get.v - give.v;
  let verdict = "Add players to each side to compare.";
  if (t.give.size || t.get.size) {
    const ad = Math.abs(diff);
    verdict = ad < 8 ? `<span class="m-good">Fair trade</span> (within ${fmt(ad, 0)} value)`
      : diff > 0 ? `<span class="m-good">You win this — +${fmt(diff, 0)} value</span>`
      : `<span class="m-bad">You lose — ${fmt(-diff, 0)} value</span>`;
    if (t.give.size > t.get.size) verdict += ` · consolidating ${t.give.size}→${t.get.size} (frees roster spots; check your depth)`;
    else if (t.get.size > t.give.size) verdict += ` · adding depth (${t.get.size} for ${t.give.size})`;
  }
  let playoffDelta = "";
  const myIds = new Set([...state.myTeam]);
  if (myIds.size >= 5 && (t.give.size || t.get.size)) {
    const cfg = LEAGUES[state.myLeague] || LEAGUES.standard;
    const myR = state.players.filter((p) => myIds.has(p.id));
    const postIds = new Set([...myIds]);
    t.give.forEach((id) => postIds.delete(id));
    t.get.forEach((id) => postIds.add(id));
    const postR = state.players.filter((p) => postIds.has(p.id));
    const cur = simulatePlayoff(myR, cfg, 2000), post = simulatePlayoff(postR, cfg, 2000);
    const d = post.odds - cur.odds;
    const dcls = d > 0 ? "m-good" : d < 0 ? "m-bad" : "";
    playoffDelta = `<div class="hint" style="margin-top:.6rem"><strong>Playoff impact (${esc(cfg.name)}):</strong> ${cur.odds}% → <span class="${dcls}">${post.odds}% (${d > 0 ? "+" : ""}${d}%)</span> · proj record ${Math.round(cur.expWins)}–${cfg.reg_weeks - Math.round(cur.expWins)} → ${Math.round(post.expWins)}–${cfg.reg_weeks - Math.round(post.expWins)}</div>`;
  }
  let results = "";
  if (q) {
    const matches = state.players.filter((p) => p.name.toLowerCase().includes(q) || (p.team || "").toLowerCase().includes(q)).slice(0, 8);
    results = `<div class="trade-results">${matches.map((p) => `<div class="wrow"><span><span class="pos-badge pos-${p.position}">${p.position}</span> ${esc(p.name)} <span class="muted">${esc(p.team)}</span> <span class="${p._value >= 0 ? "val-pos" : "val-neg"}">${p._value >= 0 ? "+" : ""}${fmt(p._value, 0)}</span></span><span><button class="btn dr-btn" data-tradeadd="${esc(p.id)}" data-side="give">Give</button> <button class="btn dr-btn" data-tradeadd="${esc(p.id)}" data-side="get">Get</button></span></div>`).join("")}</div>`;
  }
  return `<h2>Trade Analyzer</h2>
    <p class="muted">Search players and add them to each side. Valued by rest-of-season Value under your scoring (${esc(scoringLabel())}).</p>
    <input id="trade-search" type="search" placeholder="Search players to add…" value="${esc(t.q || "")}" style="max-width:340px" />
    ${results}
    <div class="two-col" style="margin-top:1rem">
      <div><h3>You give ${t.give.size ? `<span class="muted">(${fmt(give.v, 0)} val · ${fmt(give.pts, 0)} pts)</span>` : ""}</h3><div class="chips">${[...t.give].map((id) => tradeChip(id, "give")).join("") || '<span class="muted">empty</span>'}</div></div>
      <div><h3>You get ${t.get.size ? `<span class="muted">(${fmt(get.v, 0)} val · ${fmt(get.pts, 0)} pts)</span>` : ""}</h3><div class="chips">${[...t.get].map((id) => tradeChip(id, "get")).join("") || '<span class="muted">empty</span>'}</div></div>
    </div>
    <div class="hint" style="margin-top:.8rem"><strong>Verdict:</strong> ${verdict} ${(t.give.size || t.get.size) ? '<button class="btn" onclick="tradeClear()" style="margin-left:.5rem">Clear</button>' : ""}</div>
    ${playoffDelta}`;
}

function renderPlayoffSoS() {
  const pos = state.playoffPos || "RB";
  const posBtns = ["QB", "RB", "WR", "TE"].map((p) => `<button class="pos-filter-btn ${pos === p ? "active" : ""}" data-poff="${p}">${p}</button>`).join("");
  const arr = state.players.filter((p) => p.position === pos && p.matchup && p.matchup.playoff)
    .sort((a, b) => b._value - a._value).slice(0, 40)
    .sort((a, b) => b.matchup.playoff.mult - a.matchup.playoff.mult);
  const rows = arr.map((p) => {
    const pf = p.matchup.playoff, opps = (pf.opps || []).join(", ");
    const cls = pf.mult >= 1.05 ? "m-good" : pf.mult <= 0.95 ? "m-bad" : "";
    const rating = pf.mult >= 1.08 ? "Great" : pf.mult >= 1.03 ? "Good" : pf.mult <= 0.92 ? "Very tough" : pf.mult <= 0.97 ? "Tough" : "Neutral";
    return `<tr style="cursor:pointer" onclick="openPlayer('${esc(p.id)}')"><td class="left">${esc(p.name)} <span class="muted">${esc(p.team)}</span></td><td>${fmt(p._value, 0)}</td><td class="left">${esc(opps) || "—"}</td><td class="${cls}">${rating} (${pctSigned(pf.mult)})</td></tr>`;
  }).join("");
  return `<h2 style="margin-top:1.6rem">Playoff Strength of Schedule <span class="muted" style="font-size:.8rem">· Weeks 15–17</span></h2>
    <p class="muted">Who has the easiest (and toughest) fantasy-playoff matchups — target the easy schedules for a title run.</p>
    <div class="pos-filter" style="margin-bottom:.6rem">${posBtns}</div>
    <div class="table-wrap"><table class="lineup"><thead><tr><th class="left">Player</th><th>Value</th><th class="left">Wk 15–17 opponents</th><th>Playoff SoS</th></tr></thead><tbody>${rows || '<tr><td class="muted">No schedule data yet.</td></tr>'}</tbody></table></div>`;
}

// Standard normal CDF (Abramowitz-Stegun approximation).
function normCdf(z) {
  const t = 1 / (1 + 0.2316419 * Math.abs(z));
  const d = 0.3989423 * Math.exp(-z * z / 2);
  let p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
  return z > 0 ? 1 - p : p;
}
// Team weekly mean/std from a set of players used as starters.
function teamStrength(players) {
  let mean = 0, varSum = 0;
  for (const p of players) {
    const ppg = p._proj.ros_ppg || 0;
    mean += ppg;
    const cv = p.consistency ? p.consistency.cv : 0.5;
    varSum += Math.pow((cv || 0.5) * ppg, 2);
  }
  return { mean, std: Math.sqrt(varSum) || 1 };
}
function benchmarkStrength(cfg) {
  const byPos = {};
  for (const p of state.players) (byPos[p.position] = byPos[p.position] || []).push(p);
  for (const pos in byPos) byPos[pos].sort((a, b) => b._proj.ros_ppg - a._proj.ros_ppg);
  const used = new Set();
  let mean = 0, varSum = 0;
  const take = (pos, n) => {
    const pool = (byPos[pos] || []).filter((p) => !used.has(p.id)).slice(0, cfg.teams * n);
    pool.forEach((p) => used.add(p.id));
    const avg = pool.length ? pool.reduce((s, p) => s + p._proj.ros_ppg, 0) / pool.length : 0;
    mean += n * avg; varSum += n * Math.pow(0.42 * avg, 2);  // typical starter variance
  };
  ["QB", "RB", "WR", "TE", "K", "DEF"].forEach((pos) => { if (cfg.start[pos]) take(pos, cfg.start[pos]); });
  // flex: best remaining RB/WR/TE
  const flexPool = cfg.flexPos.flatMap((pos) => (byPos[pos] || []).filter((p) => !used.has(p.id)))
    .sort((a, b) => b._proj.ros_ppg - a._proj.ros_ppg).slice(0, cfg.teams * (cfg.flex || 0));
  const favg = flexPool.length ? flexPool.reduce((s, p) => s + p._proj.ros_ppg, 0) / flexPool.length : 0;
  mean += (cfg.flex || 0) * favg; varSum += (cfg.flex || 0) * Math.pow(0.42 * favg, 2);
  return { mean, std: Math.sqrt(varSum) || 1 };
}
function simulatePlayoff(roster, cfg, sims) {
  sims = sims || 4000;
  const { starters } = optimalLineup(roster, cfg);
  const mine = teamStrength(starters.map((x) => x.player).filter(Boolean));
  const bench = benchmarkStrength(cfg);
  const denom = Math.sqrt(mine.std * mine.std + bench.std * bench.std) || 1;
  const winProb = normCdf((mine.mean - bench.mean) / denom);
  const games = cfg.reg_weeks, K = cfg.playoff_spots, teams = cfg.teams;
  let playoff = 0, winsSum = 0;
  for (let s = 0; s < sims; s++) {
    let uw = 0; for (let g = 0; g < games; g++) if (Math.random() < winProb) uw++;
    winsSum += uw;
    let better = 0;
    for (let t = 0; t < teams - 1; t++) { let ow = 0; for (let g = 0; g < games; g++) if (Math.random() < 0.5) ow++; if (ow > uw) better++; }
    if (better < K) playoff++;
  }
  return { odds: Math.round(playoff / sims * 100), expWins: winsSum / sims, winProb, mean: mine.mean, benchMean: bench.mean };
}
function renderPlayoffOdds() {
  const roster = state.players.filter((p) => state.myTeam.has(p.id));
  const cfg = LEAGUES[state.myLeague] || LEAGUES.standard;
  if (roster.length < 5) {
    return `<h2 style="margin-top:1.6rem">Playoff Odds <span class="muted" style="font-size:.8rem">· ${esc(cfg.name)}</span></h2>
      <p class="muted">Add your roster (My Team tab) to simulate your season and estimate playoff odds.</p>`;
  }
  const sim = simulatePlayoff(roster, cfg);
  const odds = sim.odds, expWins = sim.expWins, winProb = sim.winProb;
  const games = cfg.reg_weeks, K = cfg.playoff_spots, teams = cfg.teams;
  const mine = { mean: sim.mean }, bench = { mean: sim.benchMean };
  const cls = odds >= 66 ? "m-good" : odds <= 33 ? "m-bad" : "";
  return `<h2 style="margin-top:1.6rem">Playoff Odds <span class="muted" style="font-size:.8rem">· ${esc(cfg.name)}</span></h2>
    <div class="settings-grid">
      <div class="kv"><span>Playoff probability</span><span class="${cls}" style="font-size:1.2rem;font-weight:800">${odds}%</span></div>
      <div class="kv"><span>Proj. record</span>${Math.round(expWins)}–${games - Math.round(expWins)}</div>
      <div class="kv"><span>Weekly avg</span>${fmt(mine.mean, 0)} pts (vs league ${fmt(bench.mean, 0)})</div>
      <div class="kv"><span>Weekly win %</span>${Math.round(winProb * 100)}%</div>
    </div>
    <div class="muted" style="font-size:.72rem;margin-top:.3rem">Monte-Carlo of ${SIMS.toLocaleString()} seasons: your optimal-lineup weekly mean/variance vs league-average opponents, ${K} of ${teams} make the playoffs over ${games} weeks. Without your league's real rosters/schedule (needs Yahoo), opponents are modeled as average — treat as directional.</div>`;
}

function renderAnalyze() {
  const host = document.getElementById("analyze-panel");
  const wasSearch = document.activeElement && document.activeElement.id === "trade-search";
  host.innerHTML = renderTrade() + renderPlayoffSoS() + renderPlayoffOdds();
  const ts = document.getElementById("trade-search");
  if (ts) {
    ts.addEventListener("input", (e) => { state.trade.q = e.target.value; renderAnalyze(); });
    if (wasSearch) { ts.focus(); ts.setSelectionRange(ts.value.length, ts.value.length); }
  }
  host.querySelectorAll("[data-tradeadd]").forEach((b) => b.addEventListener("click", () => tradeAdd(b.dataset.tradeadd, b.dataset.side)));
  host.querySelectorAll("[data-poff]").forEach((b) => b.addEventListener("click", () => { state.playoffPos = b.dataset.poff; renderAnalyze(); }));
}

function switchView(view) {
  if (view !== "draftroom" && state.draftSync && state.draftSync.on) stopDraftSync();
  state.view = view;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((s) => s.classList.remove("active"));
  const el = document.getElementById("view-" + view);
  if (el) el.classList.add("active");
  if (view === "myteam") renderMyTeam();
  else if (view === "waivers") renderWaivers();
  else if (view === "analyze") renderAnalyze();
  else if (view === "teams") renderTeams();
  else if (view === "draft") renderDraft();
  else if (view === "draftroom") renderDraftRoom();
  else if (view === "betting" && window.Betting) window.Betting.show();
  else if (view === "survivor" && window.Survivor) window.Survivor.show();
}

// ===========================================================================
// Yahoo connect wizard
// ===========================================================================
function openYahoo() {
  document.getElementById("yahoo-modal").classList.add("open");
  document.getElementById("yahoo-modal").setAttribute("aria-hidden", "false");
  renderYahoo();
}
function closeYahoo() {
  document.getElementById("yahoo-modal").classList.remove("open");
  document.getElementById("yahoo-modal").setAttribute("aria-hidden", "true");
}

async function renderYahoo() {
  const body = document.getElementById("yahoo-body");
  body.innerHTML = '<p class="muted">Checking Yahoo connection…</p>';
  let st = {};
  try { st = await (await fetch("/api/yahoo/status")).json(); } catch (e) { st = {}; }

  if (!st.configured) {
    body.innerHTML = `
      <p><strong>Step 1 of 3 — Register a free Yahoo app</strong> (one time, ~3 min).</p>
      <ol class="yahoo-steps">
        <li>Open <a href="https://developer.yahoo.com/apps/create/" target="_blank" rel="noopener">developer.yahoo.com/apps/create</a> and sign in.</li>
        <li><b>Application Name:</b> anything (e.g., "My FF Assistant").</li>
        <li><b>Application Type:</b> choose <b>Confidential Client</b>.</li>
        <li><b>Redirect URI(s):</b> enter <code>https://localhost:8787/callback</code> (required, but not actually used — we use Yahoo's copy-paste code flow).</li>
        <li><b>API Permissions:</b> Yahoo may <i>not</i> show a "Fantasy Sports" option — that's fine, Fantasy access is granted automatically. Leave the boxes unchecked (or, if it won't let you create the app without one, check <b>OpenID Connect Permissions</b>). Ignore "TW Auction".</li>
        <li>Click <b>Create App</b>. Yahoo shows a <b>Client ID (Consumer Key)</b> and <b>Client Secret (Consumer Secret)</b>.</li>
        <li>Paste them below (stored only on this computer, in <code>yahoo_config.json</code>).</li>
      </ol>
      <label class="yrow"><span>Client ID</span><input id="y-id" type="text" placeholder="Consumer Key" /></label>
      <label class="yrow"><span>Client Secret</span><input id="y-secret" type="password" placeholder="Consumer Secret" /></label>
      <div class="modal-foot"><button id="y-save" class="btn primary">Save &amp; continue</button></div>`;
    document.getElementById("y-save").addEventListener("click", async () => {
      const id = document.getElementById("y-id").value.trim();
      const secret = document.getElementById("y-secret").value.trim();
      if (!id || !secret) { toast("Enter both Client ID and Secret."); return; }
      await fetch("/api/yahoo/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: id, client_secret: secret }) });
      renderYahoo();
    });
    return;
  }

  if (!st.connected) {
    let authUrl = "";
    try { authUrl = (await (await fetch("/api/yahoo/authurl")).json()).auth_url; } catch (e) {}
    body.innerHTML = `
      <p><strong>Step 2 of 3 — Authorize on Yahoo</strong></p>
      <ol class="yahoo-steps">
        <li>Click the button to open Yahoo's login/permission page in a new tab.</li>
        <li>Sign in and click <b>Agree</b>.</li>
        <li>Yahoo sends you back to a local page that says <b>“✓ Connected”</b> — the app captures everything automatically (no code to copy).</li>
        <li>If your browser warns the local page <em>“is not private”</em>, that's just the app's own self-signed certificate on your computer — click <b>Advanced → Continue to localhost</b>.</li>
        <li>Come back to this tab and click <b>Continue</b>.</li>
      </ol>
      <div class="modal-foot" style="justify-content:flex-start;border:none;padding-top:0">
        <a class="btn primary" href="${esc(authUrl)}" target="_blank" rel="noopener">Open Yahoo authorization ↗</a>
      </div>
      <div class="modal-foot">
        <button id="y-back" class="btn">Re-enter app keys</button>
        <button id="y-continue" class="btn primary">Continue →</button>
      </div>`;
    document.getElementById("y-continue").addEventListener("click", renderYahoo);
    document.getElementById("y-back").addEventListener("click", async () => {
      await fetch("/api/yahoo/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: "", client_secret: "" }) });
      renderYahoo();
    });
    return;
  }

  // connected
  const leagues = st.leagues || [];
  body.innerHTML = `
    <p><strong>Step 3 of 3 — Import a league</strong> ✅ Connected to Yahoo.</p>
    ${st.error ? `<p class="m-bad">${esc(st.error)}</p>` : ""}
    <p class="muted">Importing pulls the league's exact scoring &amp; roster (creates a preset) and loads <em>your</em> drafted team into My Team / Start-Sit.</p>
    <div class="ylist">
      ${leagues.length ? leagues.map((l) => `<div class="wrow"><span>${esc(l.name)} <span class="muted">${l.num_teams || ""}-team</span></span>
        <span><button class="btn primary y-import" data-key="${esc(l.league_key)}">Import</button></span></div>`).join("")
      : '<p class="muted">No NFL leagues found on this account for the current season.</p>'}
    </div>
    <div class="modal-foot"><button id="y-disconnect" class="btn">Disconnect Yahoo</button></div>`;
  body.querySelectorAll(".y-import").forEach((b) => b.addEventListener("click", () => importYahooLeague(b.dataset.key, b)));
  document.getElementById("y-disconnect").addEventListener("click", async () => {
    await fetch("/api/yahoo/disconnect", { method: "POST" }); renderYahoo();
  });
}

function normName(s) {
  return String(s || "").toLowerCase().replace(/[^a-z ]/g, "").replace(/\b(jr|sr|ii|iii|iv|v)\b/g, "").replace(/\s+/g, " ").trim();
}

// Match pasted player names (one per line or comma-separated) to our players.
function matchRosterNames(text) {
  const names = text.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  const byNameOnly = {};
  for (const p of state.players) (byNameOnly[normName(p.name)] = byNameOnly[normName(p.name)] || []).push(p);
  const matched = [], unmatched = [];
  for (const nm of names) {
    const arr = byNameOnly[normName(nm)];
    if (arr && arr.length) matched.push(arr[0]); else unmatched.push(nm);
  }
  return { matched, unmatched };
}

async function importYahooLeague(leagueKey, btn) {
  btn.disabled = true; btn.textContent = "Importing…";
  try {
    const r = await (await fetch("/api/yahoo/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ league_key: leagueKey }) })).json();
    if (!r.ok) { toast("Import failed: " + (r.error || "")); btn.disabled = false; btn.textContent = "Import"; return; }
    const s = r.settings;
    const key = "yahoo_" + leagueKey.replace(/[^a-z0-9]/gi, "");
    LEAGUES[key] = {
      name: s.name + (s.draft === "auction" ? " (auction)" : " (snake)"),
      teams: s.teams || 12, budget: s.budget || 200, scoring: s.scoring, draft: s.draft,
      start: s.roster.start, flex: s.roster.flex, flexPos: s.roster.flex_pos, bench: s.roster.bench,
      yahoo_key: leagueKey,   // enables "Pull Yahoo values" (live AAV/ADP)
    };
    // populate My Team from the imported roster
    const byName = {};
    for (const p of state.players) byName[normName(p.name) + "|" + p.position] = p;
    const byNameOnly = {};
    for (const p of state.players) (byNameOnly[normName(p.name)] = byNameOnly[normName(p.name)] || []).push(p);
    let matched = 0; const unmatched = [];
    state.myTeam = new Set();
    for (const rp of (r.roster || [])) {
      let hit = byName[normName(rp.name) + "|" + (rp.pos || "")];
      if (!hit) { const arr = byNameOnly[normName(rp.name)] || []; hit = arr[0]; }
      if (hit) { state.myTeam.add(hit.id); matched++; } else unmatched.push(rp.name);
    }
    saveMyTeam();
    state.myLeague = key; localStorage.setItem("ff_myleague", key);
    state.draftLeague = key;
    applyLeagueScoring(LEAGUES[key]);
    populateLeagueSwitch();
    closeYahoo();
    switchView("myteam");
    toast(`Imported ${s.name}: ${matched} players → My Team${unmatched.length ? ", " + unmatched.length + " unmatched" : ""}.`);
  } catch (e) { toast("Import failed."); btn.disabled = false; btn.textContent = "Import"; }
}

// ---------- export / print ----------
function csvq(s) { s = String(s == null ? "" : s); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
function exportCSV() {
  const rows = visiblePlayers();
  const hdr = ["Rank", "Player", "Pos", "Team", "Bye", "Age", "Status", "BasePPG", "LastYr", "NextGm", "ROS", "Value", "Tier", "vsADP", "ExpG", "Conf"];
  const lines = [hdr.join(",")];
  rows.forEach((p, i) => {
    const pr = p._proj, last = pr.played.length ? pr.played[0].ppg : "";
    lines.push([i + 1, csvq(p.name), p.position, p.team, p.bye_week || "", p.age || "", p.injury.status || "",
      fmt(pr.baseline_ppg), last ? fmt(last) : "", fmt(pr.next_game), fmt(pr.ros_total, 0), fmt(p._value, 0),
      p._tier ? "T" + p._tier : "", p._adpDiff != null ? p._adpDiff : "", fmt(pr.expected_games, 0), pr.confidence].join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob), a = document.createElement("a");
  a.href = url; a.download = `ff_cheatsheet_${scoringLabel().replace(/[^a-z0-9]+/gi, "_")}.csv`;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  toast("Exported " + rows.length + " players to CSV.");
}

// ---------- helpers ----------
function fmt(x, dec = 1) {
  if (x == null || isNaN(x)) return "—";
  return Number(x).toFixed(dec);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

// ---------- events ----------
function setSort(key) {
  if (state.sortKey === key) state.sortDir *= -1;
  else { state.sortKey = key; state.sortDir = (key === "name" || key === "team" || key === "position") ? 1 : -1; }
  document.querySelectorAll("thead th").forEach((th) => {
    th.classList.remove("active-sort");
    const b = th.textContent.replace(/[▲▼]/g, "").trim();
    if (th.dataset.sort === key) { th.classList.add("active-sort"); th.textContent = b + (state.sortDir < 0 ? " ▼" : " ▲"); }
    else if (th.dataset.sort) th.textContent = b;
  });
  render();
}

function init() {
  document.getElementById("scoring").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (b) applyPreset(b.dataset.scoring);
  });
  populateLeagueSwitch();
  document.getElementById("league-switch").addEventListener("change", (e) => {
    if (e.target.value !== "custom") switchLeague(e.target.value);
  });
  document.getElementById("pos-filter").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.pos = b.dataset.pos;
    document.querySelectorAll("#pos-filter button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  });
  document.getElementById("search").addEventListener("input", (e) => { state.search = e.target.value; render(); });
  document.getElementById("risk-only").addEventListener("change", (e) => { state.riskOnly = e.target.checked; render(); });
  document.getElementById("trending-only").addEventListener("change", (e) => { state.trendingOnly = e.target.checked; render(); });
  document.getElementById("export-csv").addEventListener("click", exportCSV);
  document.getElementById("print-sheet").addEventListener("click", () => { switchView("rankings"); setTimeout(() => window.print(), 100); });
  document.getElementById("myteam-only").addEventListener("change", (e) => { state.myOnly = e.target.checked; render(); });
  document.querySelectorAll("thead th[data-sort]").forEach((th) => th.addEventListener("click", () => setSort(th.dataset.sort)));
  document.getElementById("rows").addEventListener("click", (e) => {
    const star = e.target.closest("[data-star]");
    if (star) { toggleMyTeam(star.dataset.star); return; }  // star toggle, don't open drawer
    const tr = e.target.closest("tr[data-id]"); if (tr) openPlayer(tr.dataset.id);
  });
  document.getElementById("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (b) switchView(b.dataset.view);
  });
  state.myLeague = localStorage.getItem("ff_myleague") || "standard";
  state.draftLeague = "johnnyv";
  state.draftRoom = loadDraftRoom();
  state.drValues = {};
  state.trade = { give: new Set(), get: new Set(), q: "" };
  state.playoffPos = "RB";
  document.querySelectorAll("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.getElementById("open-scoring").addEventListener("click", openScoring);
  document.querySelectorAll("[data-close-scoring]").forEach((el) => el.addEventListener("click", closeScoring));
  document.getElementById("open-yahoo").addEventListener("click", openYahoo);
  document.querySelectorAll("[data-close-yahoo]").forEach((el) => el.addEventListener("click", closeYahoo));
  document.getElementById("reset-scoring").addEventListener("click", () => applyPreset("ppr"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); closeScoring(); closeYahoo(); } });

  document.getElementById("refresh").addEventListener("click", async () => {
    const btn = document.getElementById("refresh");
    btn.disabled = true; btn.textContent = "↻ Refreshing…";
    try {
      const res = await fetch("/api/refresh", { method: "POST" });
      const j = await res.json();
      if (j.ok) { toast("Data refreshed — " + j.count + " players."); await loadPlayers(); }
      else toast("Refresh failed.");
    } catch (e) { toast("Refresh failed."); }
    finally { btn.disabled = false; btn.textContent = "↻ Refresh data"; syncPresetButtons(); }
  });

  syncPresetButtons();
  loadYahooStatus();
  loadPlayers().then(syncPresetButtons).catch(() => {
    document.getElementById("rows").innerHTML =
      '<tr><td colspan="17" class="empty">Still loading NFL data from the server (first launch downloads ~30 MB). This can take 10–30s — <a href="#" onclick="location.reload();return false" style="color:var(--blue)">reload</a> in a moment.</td></tr>';
  });
}

document.addEventListener("DOMContentLoaded", init);
