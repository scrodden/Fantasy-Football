/* Survivor / Eliminator pool planner -- self-contained module.
 *
 * Each week pick one team to win; a team can be used only once all season.
 * Talks to POST /api/survivor, which returns market-anchored win probabilities
 * for every remaining game and the season-long optimal team-to-week assignment
 * (so you don't burn a strong team you'll need later). Hooks into the app's tab
 * switcher via window.Survivor.show().
 */
(function () {
  "use strict";

  var state = {
    board: null,
    loading: false,
    error: null,
    picks: [],        // [{week, team}] locked picks, in order
    extraUsed: [],    // teams marked used with no specific week
    horizon: 0,       // 0 = rest of season
    showWeeks: false,
    showGrid: false,
  };

  var LS_PICKS = "ff_survivor_picks";
  var LS_EXTRA = "ff_survivor_extra";

  function panel() { return document.getElementById("survivor-panel"); }
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function pct(x) { return x == null ? "—" : (100 * x).toFixed(1) + "%"; }

  function load() {
    try { state.picks = JSON.parse(localStorage.getItem(LS_PICKS)) || []; } catch (e) { state.picks = []; }
    try { state.extraUsed = JSON.parse(localStorage.getItem(LS_EXTRA)) || []; } catch (e) { state.extraUsed = []; }
  }
  function savePicks() { localStorage.setItem(LS_PICKS, JSON.stringify(state.picks)); }
  function saveExtra() { localStorage.setItem(LS_EXTRA, JSON.stringify(state.extraUsed)); }

  function usedTeams() {
    var s = {};
    state.picks.forEach(function (p) { s[p.team] = 1; });
    state.extraUsed.forEach(function (t) { s[t] = 1; });
    return Object.keys(s);
  }
  function startWeek() {
    if (!state.picks.length) return null; // auto-detect on the server
    var mx = 0; state.picks.forEach(function (p) { if (p.week > mx) mx = p.week; });
    return mx + 1;
  }

  function api(path, body) {
    var ctrl = new AbortController();
    var to = setTimeout(function () { ctrl.abort(); }, 90000);
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}), signal: ctrl.signal })
      .then(function (r) { clearTimeout(to); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); },
        function (e) { clearTimeout(to); throw (e && e.name === "AbortError") ? new Error("timed out (first load fetches the full schedule; try again)") : e; });
  }

  function reload() {
    state.loading = true; state.error = null; render();
    api("/api/survivor", { used: usedTeams(), start_week: startWeek(), horizon: state.horizon || null })
      .then(function (b) { state.board = b; state.loading = false; render(); })
      .catch(function (e) { state.loading = false; state.error = e && e.message || String(e); render(); });
  }

  var initialized = false;
  function show() {
    if (!initialized) { load(); initialized = true; reload(); return; }
    render();
    if (!state.board && !state.loading) reload();
  }

  // ---- actions -------------------------------------------------------
  function lockPick(week, team) {
    state.picks = state.picks.filter(function (p) { return p.week !== week; });
    state.picks.push({ week: week, team: team });
    state.picks.sort(function (a, b) { return a.week - b.week; });
    savePicks();
    reload();
  }
  function undoPick(week) {
    state.picks = state.picks.filter(function (p) { return p.week !== week; });
    savePicks();
    reload();
  }
  function toggleExtra(team) {
    var i = state.extraUsed.indexOf(team);
    if (i >= 0) state.extraUsed.splice(i, 1); else state.extraUsed.push(team);
    saveExtra();
    reload();
  }
  function resetAll() {
    if (!confirm("Clear all your survivor picks and used teams?")) return;
    state.picks = []; state.extraUsed = []; savePicks(); saveExtra(); reload();
  }

  // ---- render --------------------------------------------------------
  function render() {
    var p = panel(); if (!p) return;
    if (state.error) {
      p.innerHTML = head() +
        '<div class="bet-err"><p><strong>Couldn’t build the survivor plan.</strong> ' + esc(state.error) + "</p>" +
        '<p class="muted">If the server was just updated, stop and relaunch <code>run.bat</code>, then reload. The first build fetches all 18 weeks (~20s) and is cached after.</p>' +
        '<button id="sv-retry" class="btn">↻ Retry</button></div>';
      var rb = p.querySelector("#sv-retry"); if (rb) rb.onclick = reload;
      return;
    }
    if (state.loading || !state.board) {
      p.innerHTML = head() + '<p class="muted">⏳ Projecting every remaining game and solving the season-long plan…</p>';
      wire();
      return;
    }
    var b = state.board;
    p.innerHTML = head() + recBanner(b) + pickLedger(b) + optionsTable(b) + planTable(b) + weeksSection(b) + gridSection(b) + method();
    wire();
  }

  function head() {
    return '<div class="sv-head">' +
      '<h2>🏆 Survivor Pool Planner</h2>' +
      '<p class="muted">Pick one team to win each week — but each team only once all season. This plans the whole season at once so you don’t burn a strong team you’ll need later.</p>' +
      '</div>';
  }

  function recBanner(b) {
    var r = b.recommended;
    if (!r) return '<div class="rec-banner">All planned weeks have a pick recorded. 🎉</div>';
    var alt = (b.this_week.options || [])[1];
    var altTxt = alt ? ' Nearly as good: <strong>' + esc(alt.team) + '</strong> ' + esc(alt.opp) + ' (' + pct(alt.win_prob) + ').' : "";
    return '<div class="rec-banner sv-rec">' +
      '🎯 <strong>Week ' + b.this_week.week + ' pick: ' + esc(r.team) + '</strong> ' + esc(r.opp) +
      ' — <strong>' + pct(r.win_prob) + '</strong> to win.' +
      '<div class="muted" style="margin-top:.3rem;font-size:.82rem">Chosen to maximize your chance of surviving the whole season, not just this week.' + altTxt + '</div>' +
      '</div>';
  }

  function pickLedger(b) {
    if (!state.picks.length) return "";
    var rows = state.picks.slice().sort(function (a, c) { return a.week - c.week; }).map(function (p) {
      return '<span class="sv-pick-chip">Wk ' + p.week + ': <strong>' + esc(p.team) + '</strong> <button class="sv-x" data-undo="' + p.week + '" title="undo">✕</button></span>';
    }).join("");
    return '<div class="sv-ledger"><span class="muted">Your locked picks:</span> ' + rows + '</div>';
  }

  function optionsTable(b) {
    var opts = (b.this_week && b.this_week.options) || [];
    if (!opts.length) return "";
    var rows = opts.map(function (o, i) {
      var rec = i === 0;
      return '<tr class="' + (rec ? "sv-best" : "") + '">' +
        '<td class="left"><span class="pos-badge pos-DEF">' + esc(o.team) + '</span> <span class="muted">' + esc(o.opp) + '</span>' + (rec ? ' <span class="sv-tag">best</span>' : "") + '</td>' +
        '<td><strong>' + pct(o.win_prob) + '</strong></td>' +
        '<td>' + pct(o.season_survival) + '</td>' +
        '<td class="' + (o.cost_vs_best > 0.0005 ? "val-neg" : "val-pos") + '">' + (o.cost_vs_best > 0.0005 ? "−" + (100 * o.cost_vs_best).toFixed(2) + "%" : "—") + '</td>' +
        '<td><button class="btn sv-lock" data-lock-team="' + esc(o.team) + '" data-lock-week="' + b.this_week.week + '">✓ Lock</button></td>' +
        '</tr>';
    }).join("");
    return '<div class="section-h">Week ' + b.this_week.week + ' — your best options</div>' +
      '<p class="muted" style="font-size:.78rem">“Season survival” = your chance of surviving the whole rest of the year if you pick this team now and follow the optimal plan afterward. “Cost” = how much that season-survival drops vs. the best option.</p>' +
      '<div class="table-wrap"><table class="lineup"><thead><tr><th class="left">Team</th><th>Win % this wk</th><th>Season survival</th><th>Cost</th><th>Pick</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  function planTable(b) {
    var rows = (b.plan || []).map(function (pk) {
      var picked = state.picks.some(function (x) { return x.week === pk.week && x.team === pk.team; });
      return '<tr>' +
        '<td>Wk ' + pk.week + (pk.week === b.this_week.week ? ' <span class="sv-tag">now</span>' : "") + '</td>' +
        '<td class="left"><span class="pos-badge pos-DEF">' + esc(pk.team || "—") + '</span> <span class="muted">' + esc(pk.opp || "") + '</span>' + (picked ? ' ✓' : "") + '</td>' +
        '<td>' + pct(pk.win_prob) + '</td>' +
        '<td class="muted">' + pct(pk.cum_survival) + '</td>' +
        '</tr>';
    }).join("");
    var surv = b.plan_survival != null ? '<span class="muted">Full-season survival if you follow this plan: <strong>' + pct(b.plan_survival) + '</strong> (surviving all ' + b.weeks_planned + ' weeks is rare — most pools are won earlier).</span>' : "";
    return '<div class="section-h">Optimal season plan <span class="muted" style="font-size:.72rem;text-transform:none">· weeks ' + b.start_week + '–' + b.end_week + '</span></div>' +
      '<div class="table-wrap"><table class="lineup"><thead><tr><th>Week</th><th class="left">Suggested team</th><th>Win %</th><th>Cumulative survival</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<p style="margin-top:.4rem">' + surv + '</p>';
  }

  function weeksSection(b) {
    var btn = '<button id="sv-toggle-weeks" class="btn">' + (state.showWeeks ? "▾ Hide" : "▸ Show") + ' every game’s win probability</button>';
    if (!state.showWeeks) return '<div class="section-h">Weekly slates</div>' + btn;
    var blocks = (b.weeks || []).map(function (w) {
      var games = w.games.slice().sort(function (a, c) { return c.fav_wp - a.fav_wp; }).map(function (g) {
        var noOdds = g.has_odds ? "" : ' <span class="muted" title="model-only, no market line yet">~</span>';
        return '<div class="sv-game"><span><span class="pos-badge pos-DEF">' + esc(g.fav) + '</span> ' + pct(g.fav_wp) + noOdds + '</span>' +
          '<span class="muted">' + esc(g.away) + " @ " + esc(g.home) + '</span></div>';
      }).join("");
      return '<div class="sv-week"><div class="sv-week-h">Week ' + w.week + '</div>' + games + '</div>';
    }).join("");
    return '<div class="section-h">Weekly slates <span class="muted" style="font-size:.72rem;text-transform:none">· favorite & win prob; ~ = model-only (no line yet)</span></div>' + btn +
      '<div class="sv-weeks">' + blocks + '</div>';
  }

  function gridSection(b) {
    var btn = '<button id="sv-toggle-grid" class="btn">' + (state.showGrid ? "▾ Hide" : "▸ Show") + ' “teams already used” editor</button>';
    if (!state.showGrid) return '<div class="section-h">Already used teams</div><p class="muted" style="font-size:.8rem">Locking picks above marks teams used automatically. Use this only to mark teams you burned before you started using this planner.</p>' + btn;
    var pickedTeams = {}; state.picks.forEach(function (p) { pickedTeams[p.team] = p.week; });
    var chips = (b.teams || []).map(function (t) {
      var inPick = pickedTeams[t.abbr];
      var extra = state.extraUsed.indexOf(t.abbr) >= 0;
      var cls = inPick ? "sv-chip used locked" : (extra ? "sv-chip used" : "sv-chip");
      var title = inPick ? "locked as your Week " + inPick + " pick" : (extra ? "marked used" : "available");
      return '<button class="' + cls + '" data-extra="' + esc(t.abbr) + '" ' + (inPick ? "disabled" : "") + ' title="' + esc(title) + '">' + esc(t.abbr) + (inPick ? " ✓" : "") + '</button>';
    }).join("");
    return '<div class="section-h">Already used teams</div>' + btn + '<div class="sv-grid">' + chips + '</div>';
  }

  function method() {
    var b = state.board || {};
    var m = b.model || {};
    return '<div class="method" style="margin-top:1rem"><strong>How this works:</strong> every remaining game gets a win probability from the same model as the Betting tab — team ratings (Elo + opponent-adjusted scoring) anchored to the live sportsbook line where one exists. The season is then solved as an assignment problem: one distinct team per week, maximizing the product of weekly win probabilities (your survival odds). That’s why the Week-' + (b.this_week ? b.this_week.week : "1") + ' pick isn’t always the biggest favorite — a big favorite is often worth saving for a week when your other options are weak. Recompute weekly as lines and results move. <span class="muted">Model: ' + (m.teams_rated || 0) + ' teams, ' + (m.n_games_learned || 0) + ' games learned. Free data only (ESPN).</span></div>';
  }

  function wire() {
    var p = panel(); if (!p) return;
    var h = document.getElementById("sv-horizon");
    if (h) h.onchange = function () { state.horizon = parseInt(h.value, 10) || 0; reload(); };
    var reset = document.getElementById("sv-reset");
    if (reset) reset.onclick = resetAll;
    var tw = document.getElementById("sv-toggle-weeks");
    if (tw) tw.onclick = function () { state.showWeeks = !state.showWeeks; render(); };
    var tg = document.getElementById("sv-toggle-grid");
    if (tg) tg.onclick = function () { state.showGrid = !state.showGrid; render(); };
    p.querySelectorAll(".sv-lock").forEach(function (btn) {
      btn.onclick = function () { lockPick(parseInt(btn.dataset.lockWeek, 10), btn.dataset.lockTeam); };
    });
    p.querySelectorAll("[data-undo]").forEach(function (btn) {
      btn.onclick = function () { undoPick(parseInt(btn.dataset.undo, 10)); };
    });
    p.querySelectorAll("[data-extra]").forEach(function (btn) {
      btn.onclick = function () { if (!btn.disabled) toggleExtra(btn.dataset.extra); };
    });
  }

  // The horizon control lives in the header so it's always present.
  var _head = head;
  head = function () {
    var opts = [[0, "Rest of season"], [6, "Next 6 weeks"], [4, "Next 4 weeks"], [3, "Next 3 weeks"]]
      .map(function (o) { return '<option value="' + o[0] + '"' + (state.horizon === o[0] ? " selected" : "") + '>' + o[1] + "</option>"; }).join("");
    return _head() +
      '<div class="league-bar" style="margin-bottom:.6rem">' +
      '<label class="muted">Plan horizon: <select id="sv-horizon">' + opts + '</select></label>' +
      '<button id="sv-reset" class="btn">Reset picks</button>' +
      '</div>';
  };

  window.Survivor = { show: show };
})();
