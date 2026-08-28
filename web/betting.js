/* Betting Edges tab -- self-contained module.
 *
 * Projects a winner and score for every NFL and FBS college game this week,
 * compares them to the sportsbook line, and surfaces the +EV bets. Talks to
 * the /api/betting/* routes; hooks into the app's tab switcher via
 * window.Betting.show().
 */
(function () {
  "use strict";

  var state = {
    league: "nfl",
    board: null,
    accuracy: null,
    status: null,
    loading: false,
    filters: { market: "all", conf: "all", minEdge: 0, onlyEdges: false },
    view: "edges", // "edges" | "games" | "ratings" | "strategies"
    ratings: null,
    strategies: null,
    lab: null,
    labBusy: null,
    expanded: {},
    stratDetail: null,
  };

  var panel = function () { return document.getElementById("betting-panel"); };

  // ---- data ----------------------------------------------------------
  // In the cloud (GitHub Pages) there's no Python backend, so the scheduled job
  // pre-computes every response as a static JSON file and we read those.
  var STATIC = (typeof window !== "undefined" && window.BETTING_STATIC);

  function staticApi(path, opts) {
    if (opts && opts.method === "POST") {
      return Promise.reject(new Error("This action runs automatically in the cloud — nothing to click here."));
    }
    var q = {};
    (((path.split("?")[1]) || "").split("&")).forEach(function (kv) {
      var p = kv.split("="); if (p[0]) q[p[0]] = decodeURIComponent(p[1] || "");
    });
    var lg = q.league || "nfl";
    var f;
    if (path.indexOf("/status") >= 0) f = "status.json";
    else if (path.indexOf("/board") >= 0) f = "board_" + lg + ".json";
    else if (path.indexOf("/strategies") >= 0) f = "strategies_" + lg + ".json";
    else if (path.indexOf("/accuracy") >= 0) f = "accuracy_" + lg + ".json";
    else if (path.indexOf("/rankings") >= 0) f = "rankings_" + lg + ".json";
    else if (path.indexOf("/backtest") >= 0) f = "backtest_" + lg + ".json";
    else if (path.indexOf("/explain") >= 0) {
      return fetch("data/explain_" + lg + ".json").then(function (r) { return r.json(); })
        .then(function (m) { return m[q.game_id] || { error: "explanation not found" }; });
    } else return Promise.reject(new Error("unavailable in cloud view"));
    return fetch("data/" + f).then(function (r) {
      if (!r.ok) throw new Error("data not ready yet (" + f + ")");
      return r.json();
    });
  }

  function api(path, opts, timeoutMs) {
    if (STATIC) return staticApi(path, opts);
    var ctrl = new AbortController();
    var to = setTimeout(function () { ctrl.abort(); }, timeoutMs || 45000);
    var o = Object.assign({ signal: ctrl.signal }, opts || {});
    return fetch(path, o).then(function (r) {
      clearTimeout(to);
      if (!r.ok) throw new Error("HTTP " + r.status + " on " + path);
      return r.json();
    }, function (err) {
      clearTimeout(to);
      throw (err && err.name === "AbortError") ? new Error("timed out on " + path) : err;
    });
  }

  function loadStatus() {
    state.statusError = null;
    return api("/api/betting/status")
      .then(function (s) {
        if (!s || !s.leagues) throw new Error("unexpected response from /api/betting/status");
        state.status = s; return s;
      })
      .catch(function (e) { state.statusError = e && e.message || String(e); render(); throw e; });
  }

  function loadBoard() {
    state.loading = true; render();
    return api("/api/betting/board?league=" + state.league, null, 60000)
      .then(function (b) { state.board = b; })
      .then(function () { return api("/api/betting/accuracy?league=" + state.league); })
      .then(function (a) { state.accuracy = a; state.loading = false; render(); })
      .catch(function (e) { state.loading = false; state.statusError = (e && e.message || String(e)); render(); });
  }

  // ---- formatting helpers -------------------------------------------
  function evPct(ev) { return (ev >= 0 ? "+" : "") + (ev * 100).toFixed(1) + "%"; }
  function odds(n) { return n == null ? "" : (n > 0 ? "+" + n : "" + n); }
  function sg(n) { return (n > 0 ? "+" : "") + n; }
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }

  function confBadge(c) {
    return '<span class="bet-conf bet-conf-' + c + '">' + c + "</span>";
  }
  function marketTag(m) {
    var label = { spread: "Spread", total: "Total", moneyline: "ML" }[m] || m;
    return '<span class="bet-mkt bet-mkt-' + m + '">' + label + "</span>";
  }

  function errBox(e) {
    return '<div class="bet-err">Could not load the betting model: ' + esc(e && e.message || e) + "</div>";
  }

  // ---- top-level render ---------------------------------------------
  function render() {
    var p = panel();
    if (!p) return;
    if (state.statusError) {
      p.innerHTML =
        '<div class="bet-err">' +
        "<p><strong>Couldn’t reach the betting model.</strong> " + esc(state.statusError) + "</p>" +
        '<p class="muted">The most common cause is that the app server is still running an older version without the Betting routes. ' +
        "Stop the server (close its window or Ctrl+C) and relaunch <code>run.bat</code>, then reload this page.</p>" +
        '<button id="bet-retry" class="btn">↻ Retry</button></div>';
      var rb = p.querySelector("#bet-retry");
      if (rb) rb.onclick = function () { state.statusError = null; initialized = false; render(); show(); };
      return;
    }
    if (!state.status) {
      p.innerHTML = header() + '<div class="bet-working">⏳ Loading the betting model…</div>';
      wire();
      return;
    }

    var lg = state.status.leagues[state.league] || {};
    if (!lg.seeded) { p.innerHTML = header() + seedPrompt(); wire(); return; }
    if (state.loading || !state.board) { p.innerHTML = header() + '<p class="muted">Projecting this week’s slate…</p>'; wire(); return; }

    var showAcc = state.view === "edges" || state.view === "games";
    p.innerHTML = header() + subbar() + (showAcc ? accuracyPanel() : "") + body();
    wire();
  }

  function header() {
    var s = state.status || { leagues: {} };
    var nflA = state.league === "nfl" ? " active" : "";
    var cfbA = state.league === "cfb" ? " active" : "";
    var oddsCfg = s.odds_api_configured
      ? '<span class="bet-ok">multi-book on</span>'
      : '<span class="muted">single-book (ESPN)</span>';
    var cfbdCfg = state.league === "cfb"
      ? (s.cfbd_configured ? '<span class="bet-ok">CFBD connected</span>'
                           : '<span class="muted">no CFBD key</span>')
      : "";
    return (
      '<div class="bet-head">' +
      '  <div class="bet-league" role="group">' +
      '    <button data-lg="nfl" class="bet-lgbtn' + nflA + '">🏈 NFL</button>' +
      '    <button data-lg="cfb" class="bet-lgbtn' + cfbA + '">🎓 College (FBS)</button>' +
      "  </div>" +
      '  <div class="bet-head-actions">' +
      "    " + oddsCfg + " " + cfbdCfg +
      (STATIC
        ? '    <span class="bet-ok" title="The scheduled cloud job locks, grades, and updates everything automatically">☁ auto-updating</span>'
        : ('    <button id="bet-update" class="btn" title="Pull last week’s finals, grade our picks, and sharpen the model">↻ Update from results</button>' +
           '    <button id="bet-odds" class="btn" title="Add a free The Odds API key for multi-book line shopping">⚙ Odds API</button>' +
           (state.league === "cfb" ? '    <button id="bet-cfbd" class="btn" title="Set a free CollegeFootballData API key for college lines + EPA">⚙ CFBD key</button>' : ""))) +
      "  </div>" +
      "</div>"
    );
  }

  function seedPrompt() {
    return (
      '<div class="bet-seed">' +
      "  <h2>Set up the " + esc(labelOf(state.league)) + " model</h2>" +
      "  <p class=\"muted\">The model needs to learn from past results before it can project this week. " +
      "  This pulls the last two seasons of games from ESPN (free) and builds each team’s rating. Takes ~10–30s.</p>" +
      '  <button id="bet-seed-go" class="btn primary">Build the model</button>' +
      "</div>"
    );
  }

  function subbar() {
    var b = state.board;
    var weekTxt = b.week ? ("Week " + b.week) : "";
    var seasonTxt = b.year ? (b.year + " ") : "";
    var tabs =
      '<div class="bet-viewtabs">' +
      viewTab("edges", "🎯 Top Edges") +
      viewTab("games", "📋 All Games (" + b.n_games + ")") +
      viewTab("strategies", "📈 Strategy Tracker") +
      viewTab("lab", "🔬 Model Lab") +
      viewTab("ratings", "📊 Power Ratings") +
      "</div>";
    var meta =
      '<div class="bet-meta muted">' + esc(seasonTxt + weekTxt) +
      " · " + b.model.n_games_learned + " games learned · " +
      b.model.teams_rated + " teams rated · updated " + esc((b.generated_at || "").replace("T", " ")) +
      "</div>";
    return '<div class="bet-subbar">' + tabs + meta + "</div>";
  }

  function viewTab(v, label) {
    return '<button data-view="' + v + '" class="bet-vtab' + (state.view === v ? " active" : "") + '">' + label + "</button>";
  }

  // ---- accuracy ------------------------------------------------------
  function accuracyPanel() {
    var a = state.accuracy;
    if (!a || !a.n) {
      return '<div class="bet-acc bet-acc-empty muted">📈 Track record: no graded games yet. After this week’s games finish, click <strong>Update from results</strong> — every pick is scored against the closing line so the record here is honest, and the model sharpens for next week.</div>';
    }
    var tiles = [
      tile("Games graded", a.n, ""),
      tile("Winner ATS-agnostic", a.su_pct != null ? a.su_pct + "%" : "—", "straight-up winner called correctly"),
      tile("Brier", a.brier != null ? a.brier.toFixed(3) : "—", "win-prob calibration (lower is better; 0.25 = coin flip)"),
      tile("Margin MAE", a.margin_mae != null ? a.margin_mae + " pts" : "—", "avg error on projected margin"),
      tile("Total MAE", a.total_mae != null ? a.total_mae + " pts" : "—", "avg error on projected total"),
    ];
    var mk = a.by_market || {};
    ["spread", "total", "moneyline"].forEach(function (m) {
      if (mk[m]) {
        var r = mk[m];
        var rec = r.win + "-" + r.loss + (r.push ? "-" + r.push : "");
        var roi = r.roi_pct != null ? (r.roi_pct >= 0 ? "+" : "") + r.roi_pct + "% ROI" : "";
        tiles.push(tile(cap(m) + " bets", rec, (r.units >= 0 ? "+" : "") + r.units + "u  " + roi));
      }
    });
    return '<div class="bet-acc"><div class="bet-acc-title">📈 Model track record (out-of-sample)</div><div class="bet-tiles">' + tiles.join("") + "</div></div>";
  }

  function tile(label, val, sub) {
    return '<div class="bet-tile"><div class="bet-tile-val">' + esc(val) + '</div><div class="bet-tile-label">' + esc(label) + "</div>" +
      (sub ? '<div class="bet-tile-sub muted">' + esc(sub) + "</div>" : "") + "</div>";
  }

  // ---- body views ----------------------------------------------------
  function body() {
    if (state.view === "ratings") return ratingsView();
    if (state.view === "strategies") return stratView();
    if (state.view === "lab") return labView();
    if (state.view === "games") return filterBar() + gamesView();
    return filterBar() + edgesView();
  }

  // ---- model lab (backtest + calibration) ---------------------------
  function labView() {
    var r = state.lab;
    if (state.labBusy) {
      return '<div class="bet-working">🔬 ' + esc(state.labBusy) + " This can take up to a minute for college. Please wait…</div>";
    }
    if (!r || r._league !== state.league) {
      api("/api/betting/backtest?league=" + state.league, null, 120000).then(function (rep) {
        rep._league = state.league; state.lab = rep; if (state.view === "lab") render();
      }).catch(function (e) { state.statusError = e.message || String(e); render(); });
      return '<p class="muted">Running the walk-forward backtest over past seasons…</p>';
    }
    var intro =
      '<div class="bet-strat-intro"><p>The model is graded <strong>walk-forward</strong> over ' +
      esc((r.seasons || []).join(", ")) + " — it only ever sees games already played, then its <em>pure</em> number " +
      "(no market anchoring) is scored against the real closing line. Beating the closing line is genuinely hard; the honest goal is to make the underlying projection as accurate as possible.</p>" +
      (STATIC ? "" : '<p><button id="bet-recal" class="btn primary btn-sm">🔧 Recalibrate model</button> ' +
        '<span class="muted">searches the parameters against history and applies the best — ~10s NFL, ~1 min college.</span>' +
        (r.calibrated_at ? ' <span class="muted">Last calibrated ' + esc(r.calibrated_at.replace("T", " ")) + ".</span>" : "") + "</p>") +
      "</div>";

    var tiles = [
      tile("Straight-up", r.su_pct != null ? r.su_pct + "%" : "—", "winner called correctly"),
      tile("Margin MAE", r.margin_mae != null ? r.margin_mae + " pts" : "—", "avg error on the spread"),
      tile("Total MAE", r.total_mae != null ? r.total_mae + " pts" : "—", "avg error on the total"),
      tile("Brier", r.brier != null ? r.brier.toFixed(3) : "—", "win-prob calibration (lower better)"),
      tile("Off the close", r.vs_close_mae != null ? r.vs_close_mae + " pts" : "—", "avg gap from the closing line"),
      tile("Games scored", r.n_games_scored || 0, "out-of-sample"),
    ].join("");

    function betLine(label, b, note) {
      if (!b || (b.win + b.loss) === 0) return "";
      return '<tr><td>' + esc(label) + "</td><td>" + esc(b.record) + '</td><td class="num">' + (b.ats_pct != null ? b.ats_pct + "%" : "—") +
        '</td><td class="num bet-ev ' + pnlCls(b.roi) + '">' + (b.roi != null ? (b.roi >= 0 ? "+" : "") + b.roi + "%" : "—") +
        "</td><td class=\"muted\">" + esc(note) + "</td></tr>";
    }
    var betsTbl =
      '<h4 class="bet-md-h">Betting the pure model vs. the closing line (the hard test)</h4>' +
      '<table class="bet-table"><thead><tr><th>Strategy</th><th>Record</th><th>ATS%</th><th>ROI</th><th></th></tr></thead><tbody>' +
      betLine("Spread — every game", r.spread_all, "break-even ≈ 52.4%") +
      betLine("Moneyline — every game", r.ml_all, "SU winners at market prices") +
      betLine("Value — flagged edges", r.value, "where the model disagreed most") +
      "</tbody></table>" +
      '<p class="muted" style="margin-top:.4rem">Against the razor-sharp closing line these usually sit below break-even — expected, and exactly why the live model anchors to the market. The model earns its keep on softer, earlier lines and in the projection accuracy above.</p>';

    // calibration table
    var cal = (r.calibration || []).map(function (c) {
      var off = Math.abs(c.actual - c.predicted);
      return "<tr><td>" + esc(c.bucket) + '</td><td class="num">' + c.predicted + '%</td><td class="num">' + c.actual +
        '%</td><td class="num">' + c.n + "</td></tr>";
    }).join("");
    var calTbl = cal ? '<h4 class="bet-md-h">Win-probability calibration</h4>' +
      '<table class="bet-table"><thead><tr><th>Predicted</th><th>Mid</th><th>Actual</th><th>n</th></tr></thead><tbody>' + cal + "</tbody></table>" : "";

    var sig = r.signals || {};
    var sigLine = '<p class="muted">Active signals: ' +
      Object.keys(sig).map(function (k) { return k + " " + (sig[k] ? "✓" : "✕"); }).join(" · ") +
      " · recent-form and QB were measured to hurt accuracy and are off.</p>";

    var advanced = STATIC ? "" :
      '<details class="bet-advanced"><summary>Advanced</summary>' +
      '<p class="muted">The model updates itself — “↻ Update from results” each week folds in new games, and the season rollover' +
      (state.league === "cfb" ? " (with preseason priors)" : "") + " happens automatically. You should rarely need this.</p>" +
      '<button id="bet-rebuild" class="btn btn-danger btn-sm">⟳ Rebuild from scratch</button>' +
      '<span class="muted"> — reseeds from the last two completed seasons, recalibrates, ' +
      (state.league === "cfb" ? "reseeds priors, " : "") + "and rebuilds EPA. <strong>Discards any in-season learning</strong> — setup/repair only.</span>" +
      "</details>";

    return intro + '<div class="bet-tiles bet-strat-tiles">' + tiles + "</div>" + sigLine + betsTbl + calTbl + advanced;
  }

  // ---- strategy tracker ---------------------------------------------
  var STRATS = ["spread", "moneyline", "value", "bankroll"];
  function dollars(n) { return "$" + Math.round(n).toLocaleString(); }
  function money(n) { return (n >= 0 ? "+$" : "-$") + Math.abs(n).toFixed(0); }
  function money2(n) { return (n >= 0 ? "+$" : "-$") + Math.abs(n).toFixed(2); }
  function pnlCls(n) { return n > 0 ? "pos" : (n < 0 ? "neg" : ""); }

  function stratView() {
    var r = state.strategies;
    if (!r || r._league !== state.league) {
      api("/api/betting/strategies?league=" + state.league).then(function (rep) {
        rep._league = state.league; state.strategies = rep; if (state.view === "strategies") render();
      });
      return '<p class="muted">Loading strategy tracker…</p>';
    }
    var intro =
      '<div class="bet-strat-intro">' +
      '<p><strong>Four</strong> strategies, graded every week: three flat-<strong>$' + r.stake + '</strong> (spread / moneyline / high-conviction), plus a <strong>' + dollars(r.starting_bankroll || 10000) + ' bankroll</strong> that bets every flagged edge at its recommended ¼-Kelly size. Each game’s bets are <strong>locked ' + r.lock_hours + 'h before kickoff</strong> — never revised after. ' +
      (STATIC ? '<span class="bet-ok">☁ locks automatically in the cloud</span>' : '<button id="bet-locknow" class="btn btn-sm" title="Lock bets for any game kicking off within ' + r.lock_hours + 'h now">🔒 Lock due bets now</button>') + "</p>" +
      "</div>";

    if (!r.n_locks) {
      return intro + '<div class="bet-acc-empty muted">No bets locked yet. Bets lock automatically ~' + r.lock_hours + 'h before each game' + (STATIC ? " (the cloud does this on a schedule)" : " (the app does this in the background while it’s open)") + '. Come back once this week’s games are inside that window.</div>';
    }

    // Season summary tiles — every strategy shown as a $10k bankroll.
    var tiles = STRATS.map(function (s) {
      var t = r.totals[s];
      var curve = (r.curves[s] || []);
      var val = t.bankroll_value != null ? t.bankroll_value : (r.starting_bankroll || 10000);
      var ret = t.return_pct != null ? t.return_pct : 0;
      var sizing = s === "bankroll" ? "¼-Kelly (compounding)" : "flat $" + (r.stake || 100);
      var open = state.stratDetail === s;
      return '<div class="bet-strat-card' + (open ? " open" : "") + '" data-strat="' + s + '" title="Click to see every bet">' +
        '<div class="bet-strat-name">' + esc(r.strategy_labels[s]) + ' <span class="bet-strat-caret">' + (open ? "▾" : "▸") + "</span></div>" +
        '<div class="bet-strat-pnl ' + pnlCls(t.profit) + '">' + dollars(val) + "</div>" +
        '<div class="bet-strat-sub muted">' + (ret >= 0 ? "+" : "") + ret + "% from " + dollars(r.starting_bankroll || 10000) +
        "  ·  " + esc(t.record || "0-0") + (t.win_pct != null ? "  ·  " + t.win_pct + "% win" : "") + "</div>" +
        '<div class="bet-strat-sub muted">' + t.n + " bets settled · " + sizing +
        (t.pending ? " · " + t.pending + " pending" : "") + "</div>" +
        sparkline(curve) + "</div>";
    }).join("");

    // Weekly table
    var head = '<tr><th>Week</th>' + STRATS.map(function (s) {
      return "<th>" + (s === "value" ? "Value" : (s === "bankroll" ? "Bankroll" : cap(s))) + "</th>";
    }).join("") + "</tr>";
    var rows = r.weeks.map(function (w) {
      var open = state.expanded[w.week];
      var cells = STRATS.map(function (s) {
        var b = w.strategies[s];
        var pend = b.pending ? ' <span class="muted">(' + b.pending + "p)</span>" : "";
        return '<td class="bet-wk-cell"><span class="bet-ev ' + pnlCls(b.profit) + '">' +
          (b.bankroll_value != null ? dollars(b.bankroll_value) : "—") + "</span>" +
          ' <span class="muted">' + money(b.profit) + "</span>" + pend + "</td>";
      }).join("");
      var detail = open ? '<tr class="bet-wk-detail"><td colspan="' + (STRATS.length + 1) + '">' + weekDetail(w) + "</td></tr>" : "";
      return '<tr class="bet-wk-row" data-week="' + w.week + '"><td class="bet-wk-toggle">' + (open ? "▾" : "▸") + " Wk " + w.week + "</td>" + cells + "</tr>" + detail;
    }).join("");

    return (
      intro +
      latestWeekPanel(r) +
      '<h3 class="bet-h3">Season totals <span class="muted">— click a strategy to see every bet behind it</span></h3>' +
      '<div class="bet-tiles bet-strat-tiles">' + tiles + "</div>" +
      (state.stratDetail ? stratDetailPanel(r, state.stratDetail) : "") +
      '<h3 class="bet-h3">Week by week <span class="muted">— click a week to see every bet</span></h3>' +
      '<table class="bet-table bet-week-table"><thead>' + head + "</thead><tbody>" + rows + "</tbody></table>"
    );
  }

  function latestWeekPanel(r) {
    var lw = r.latest_week;
    if (!lw) return "";
    var c = lw.combined || {};
    var cards = STRATS.map(function (s) {
      var b = lw.strategies[s];
      return '<div class="bet-lw-card"><div class="bet-lw-name">' + esc(shortStrat(s)) + "</div>" +
        '<div class="bet-lw-pnl ' + pnlCls(b.profit) + '">' + (b.bankroll_value != null ? dollars(b.bankroll_value) : "—") + "</div>" +
        '<div class="bet-lw-sub muted">' + money(b.profit) + " this week · " + esc(b.record || "0-0") +
        (b.pending ? ' · <span class="bet-res-pending">' + b.pending + " pending</span>" : "") + "</div></div>";
    }).join("");
    return (
      '<div class="bet-latest-week">' +
      '<div class="bet-lw-head">📅 Week ' + lw.week + " results" +
      '<span class="bet-lw-combined ' + pnlCls(c.profit) + '">' + (c.record || "") + "  " + money(c.profit || 0) +
      (c.roi != null ? "  (" + c.roi + "% ROI)" : "") + "</span></div>" +
      '<div class="bet-lw-cards">' + cards + "</div>" +
      '<a class="bet-lw-link" data-week="' + lw.week + '">See every bet ↓</a>' +
      "</div>"
    );
  }
  function shortStrat(s) {
    return { value: "High conviction", moneyline: "Moneyline", spread: "Spread", bankroll: "Bankroll (Kelly)" }[s] || s;
  }

  // Every bet behind a strategy's number, with a running balance.
  function stratDetailPanel(r, s) {
    var start = r.starting_bankroll || 10000;
    var bets = (r.totals[s].bets || []).slice().sort(function (a, b) {
      return (a.week - b.week) || String(a.kickoff || "").localeCompare(String(b.kickoff || ""));
    });
    var running = start;
    var rows = bets.map(function (bet) {
      var res = bet.result || "pending";
      var settled = res === "win" || res === "loss" || res === "push";
      if (settled) running += (bet.pnl || 0);
      var price = bet.market === "moneyline" || (bet.price && bet.price !== -110) ? odds(bet.price) : "-110";
      return '<tr class="bet-bet-' + res + '"><td class="num muted">' + (bet.week || "") + "</td>" +
        "<td>" + marketTag(bet.market) + "</td>" +
        "<td>" + esc(bet.game) + (bet.score ? ' <span class="muted">' + esc(bet.score) + "</span>" : "") + "</td>" +
        "<td><strong>" + esc(bet.pick) + "</strong></td>" +
        '<td class="num muted">' + dollars(bet.stake || 100) + "</td>" +
        '<td class="num">' + price + "</td><td>" + resBadge(res) + "</td>" +
        '<td class="num bet-ev ' + (bet.pnl > 0 ? "pos" : (bet.pnl < 0 ? "neg" : "")) + '">' + (settled ? money2(bet.pnl) : "—") + "</td>" +
        '<td class="num">' + (settled ? dollars(running) : "—") + "</td></tr>";
    }).join("");
    var t = r.totals[s];
    if (!bets.length) rows = '<tr><td colspan="9" class="muted">No bets settled or pending yet.</td></tr>';
    return '<div class="bet-strat-detail"><div class="bet-detail-head">📋 Every <strong>' + esc(shortStrat(s)) + "</strong> bet · " +
      esc(t.record || "0-0") + " · " + money2(t.profit) + " → " + dollars(t.bankroll_value) +
      ' <a class="bet-lw-link" id="bet-strat-close">close ✕</a></div>' +
      '<div style="overflow-x:auto"><table class="bet-table bet-detail-table"><thead><tr>' +
      "<th>Wk</th><th>Mkt</th><th>Game</th><th>Pick</th><th>Stake</th><th>Price</th><th>Result</th><th>P&amp;L</th><th>Balance</th>" +
      "</tr></thead><tbody>" + rows + "</tbody></table></div></div>";
  }

  function sparkline(curve) {
    if (!curve.length) return "";
    var vals = curve.map(function (c) { return c.cumulative; });
    var min = Math.min(0, Math.min.apply(null, vals)), max = Math.max(0, Math.max.apply(null, vals));
    var range = (max - min) || 1;
    var w = 120, h = 28;
    var pts = curve.map(function (c, i) {
      var x = curve.length === 1 ? 0 : (i / (curve.length - 1)) * w;
      var y = h - ((c.cumulative - min) / range) * h;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var zeroY = (h - ((0 - min) / range) * h).toFixed(1);
    var last = vals[vals.length - 1];
    return '<svg class="bet-spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
      '<line x1="0" y1="' + zeroY + '" x2="' + w + '" y2="' + zeroY + '" class="bet-spark-zero"/>' +
      '<polyline points="' + pts + '" class="bet-spark-line ' + (last >= 0 ? "pos" : "neg") + '"/></svg>';
  }

  function weekDetail(w) {
    return STRATS.map(function (s) {
      var b = w.strategies[s];
      if (!b.bets.length) return "";
      var rows = b.bets.map(function (bet) {
        var res = bet.result || "pending";
        var pnl = bet.result ? money2(bet.pnl) : "—";
        var price = bet.market === "moneyline" || (bet.price && bet.price !== -110) ? odds(bet.price) : "-110";
        return "<tr class=\"bet-bet-" + res + "\"><td>" + marketTag(bet.market) + "</td><td>" + esc(bet.game) + "</td><td><strong>" + esc(bet.pick) + "</strong></td>" +
          '<td class="num muted">' + dollars(bet.stake || 100) + "</td>" +
          "<td class=\"num\">" + price + "</td><td>" + resBadge(res) + "</td>" +
          '<td class="num bet-ev ' + (bet.pnl > 0 ? "pos" : (bet.pnl < 0 ? "neg" : "")) + '">' + pnl + "</td></tr>";
      }).join("");
      var t = b;
      var pnlSpan = '<span class="bet-ev ' + pnlCls(t.profit) + '">' + money2(t.profit) + '</span>';
      var head = s === "bankroll" ? pnlSpan
        : '<span class="muted">' + esc(t.record || "0-0") + ' · </span>' + pnlSpan;
      return (
        '<div class="bet-detail-strat">' +
        '<div class="bet-detail-head">' + esc(state.strategies.strategy_labels[s]) + " " + head + "</div>" +
        '<table class="bet-table bet-detail-table"><thead><tr><th>Mkt</th><th>Game</th><th>Pick</th><th>Stake</th><th>Price</th><th>Result</th><th>P&amp;L</th></tr></thead><tbody>' +
        rows + "</tbody></table></div>"
      );
    }).join("");
  }

  function resBadge(res) {
    var cls = { win: "win", loss: "loss", push: "push", pending: "pending" }[res] || "pending";
    return '<span class="bet-res bet-res-' + cls + '">' + res + "</span>";
  }

  function filterBar() {
    var f = state.filters;
    function opt(cur, val, label) { return '<option value="' + val + '"' + (cur === val ? " selected" : "") + ">" + label + "</option>"; }
    return (
      '<div class="bet-filters">' +
      '  <label>Market <select id="bf-market">' +
      opt(f.market, "all", "All") + opt(f.market, "spread", "Spread") + opt(f.market, "total", "Total") + opt(f.market, "moneyline", "Moneyline") +
      "  </select></label>" +
      '  <label>Confidence <select id="bf-conf">' +
      opt(f.conf, "all", "Any") + opt(f.conf, "high", "High only") + opt(f.conf, "medium", "Medium+") +
      "  </select></label>" +
      '  <label>Min edge <input id="bf-edge" type="number" step="0.5" min="0" value="' + f.minEdge + '" style="width:4.5em"></label>' +
      (state.view === "games" ? '  <label class="bet-check"><input type="checkbox" id="bf-onlyedges"' + (f.onlyEdges ? " checked" : "") + "> Only games with edges</label>" : "") +
      '  <span class="bet-legend muted">Kelly = suggested stake (¼-Kelly, % of bankroll)</span>' +
      "</div>"
    );
  }

  function passFilters(e) {
    var f = state.filters;
    if (f.market !== "all" && e.market !== f.market) return false;
    if (f.conf === "high" && e.confidence !== "high") return false;
    if (f.conf === "medium" && e.confidence === "low") return false;
    if (Math.abs(e.edge) < f.minEdge) return false;
    return true;
  }

  // ---- date/time helpers (kickoff is ISO UTC, e.g. 2026-08-29T16:00Z) -----
  function parseKick(iso) { if (!iso) return null; var d = new Date(iso); return isNaN(d) ? null : d; }
  function dayKey(iso) { var d = parseKick(iso); if (!d) return "zzz"; return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
  function dayLabel(iso) {
    var d = parseKick(iso); if (!d) return "Date TBD";
    return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  }
  function timeLabel(iso) {
    var d = parseKick(iso); if (!d) return "";
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  function matchupFull(e) {
    var join = e.neutral ? " vs " : " @ ";
    return esc(e.away_full || e.away_abbr || "") + join + esc(e.home_full || e.home_abbr || "");
  }

  function edgeRow(e) {
    var kelly = e.kelly ? (e.kelly * 100).toFixed(1) + "%" : "—";
    var edgeTxt = e.market === "moneyline" ? (e.edge + " pp") : (e.edge + " pts");
    var book = e.book ? '<span class="bet-book">' + esc(e.book) + "</span>" : "";
    return (
      '<tr class="bet-erow bet-conf-row-' + e.confidence + '" data-game-id="' + esc(e.game_id) + '" title="Click for the model’s reasoning">' +
      "<td>" + marketTag(e.market) + "</td>" +
      '<td class="bet-game"><span class="bet-game-teams">' + matchupFull(e) + '</span>' +
      '<span class="bet-game-time muted">' + esc(timeLabel(e.kickoff)) + "</span></td>" +
      '<td class="bet-pick"><strong>' + esc(e.pick) + "</strong> " + book + "</td>" +
      '<td class="num">' + esc(edgeTxt) + "</td>" +
      '<td class="num">' + Math.round(e.win_prob * 100) + "%</td>" +
      '<td class="num bet-ev ' + (e.ev >= 0 ? "pos" : "neg") + '">' + evPct(e.ev) + "</td>" +
      '<td class="num">' + kelly + "</td>" +
      "<td>" + confBadge(e.confidence) + "</td>" +
      "</tr>"
    );
  }

  function edgesView() {
    var edges = (state.board.top_edges || []).filter(passFilters);
    if (!edges.length) {
      return '<p class="muted bet-noedge">' +
        "No bets clear the current filters. The model only flags a play when it disagrees with the market by enough to matter — often that’s a handful of games, and early in the season it defers to sharp opening lines by design.</p>";
    }
    // Group by game day, days in chronological order.
    var byDay = {};
    edges.forEach(function (e) { (byDay[dayKey(e.kickoff)] = byDay[dayKey(e.kickoff)] || []).push(e); });
    var keys = Object.keys(byDay).sort();
    var head = '<thead><tr><th>Mkt</th><th>Game</th><th>Pick</th><th>Edge</th><th>Win%</th><th>EV</th><th>Kelly</th><th>Conf</th></tr></thead>';
    var sections = keys.map(function (k) {
      var list = byDay[k];
      // within a day, keep EV order (already sorted by board)
      var label = k === "zzz" ? "Date TBD" : dayLabel(list[0].kickoff);
      return '<div class="bet-day"><div class="bet-day-head">📅 ' + esc(label) +
        ' <span class="muted">· ' + list.length + " edge" + (list.length !== 1 ? "s" : "") + "</span></div>" +
        '<table class="bet-table">' + head + "<tbody>" + list.map(edgeRow).join("") + "</tbody></table></div>";
    }).join("");
    return '<div class="bet-edges">' + sections + "</div>";
  }

  function gameCard(g) {
    var p = g.projection, m = g.market || {};
    var edges = (g.edges || []).filter(passFilters);
    if (state.filters.onlyEdges && !edges.length) return "";
    var a = g.away, h = g.home;
    var aRank = a.rank ? '<span class="bet-rank">#' + a.rank + "</span> " : "";
    var hRank = h.rank ? '<span class="bet-rank">#' + h.rank + "</span> " : "";
    var favA = p.proj_margin < 0 ? " bet-winner" : "";
    var favH = p.proj_margin > 0 ? " bet-winner" : "";
    var mline = m.home_spread != null
      ? (sg(m.home_spread) + " / " + (m.total != null ? "O" + m.total : "—") + (m.home_ml != null ? " / " + odds(m.home_ml) + "," + odds(m.away_ml) : ""))
      : "no line";
    var comp = p.components || {};
    var restNote = comp.rest_note ? '<span class="bet-note">🛌 ' + esc(comp.rest_note) + "</span>" : "";
    var kick = timeLabel(g.date);
    var status = g.completed ? '<span class="bet-final">FINAL ' + (a.score) + "-" + (h.score) + "</span>"
      : ('<span class="bet-kick">🕐 ' + esc(kick || g.status || "") + "</span>");
    var aName = esc(a.full || a.name || a.abbr), hName = esc(h.full || h.name || h.abbr);

    var edgeHtml = edges.length
      ? '<div class="bet-card-edges">' + edges.map(function (e) {
          return '<div class="bet-chip bet-conf-row-' + e.confidence + '">' + marketTag(e.market) +
            " <strong>" + esc(e.pick) + "</strong> " +
            '<span class="bet-chip-ev ' + (e.ev >= 0 ? "pos" : "neg") + '">' + evPct(e.ev) + "</span> " +
            confBadge(e.confidence) + "</div>";
        }).join("") + "</div>"
      : '<div class="bet-card-noedge muted">No edge — model agrees with the market here.</div>';

    return (
      '<div class="bet-card" data-game-id="' + esc(g.id) + '" title="Click for the model’s full reasoning">' +
      '  <div class="bet-card-top">' +
      '    <div class="bet-matchup">' +
      '      <div class="bet-team' + favA + '">' + aRank + '<span class="bet-tname">' + aName + '</span> <span class="bet-rec muted">' + esc(a.record || "") + "</span>" +
      '        <span class="bet-proj">' + p.proj_away_score + "</span></div>" +
      '      <div class="bet-team' + favH + '">' + hRank + '<span class="bet-tname">' + hName + '</span> <span class="bet-rec muted">' + esc(h.record || "") + "</span>" +
      '        <span class="bet-proj">' + p.proj_home_score + "</span></div>" +
      "    </div>" +
      '    <div class="bet-card-num">' +
      '      <div class="bet-line"><span class="muted">Model</span> ' + esc(a.abbr) + " " + sg(-p.proj_margin) + ", O/U " + p.proj_total + "</div>" +
      '      <div class="bet-line"><span class="muted">Market</span> ' + esc(h.abbr) + " " + esc(mline) + "</div>" +
      '      <div class="bet-line bet-status">' + status + " " + restNote + "</div>" +
      "    </div>" +
      "  </div>" +
      edgeHtml +
      "</div>"
    );
  }

  function gamesView() {
    var games = state.board.games.slice();
    games.forEach(function (g) {
      var es = (g.edges || []).filter(passFilters);
      g._bestEv = es.length ? Math.max.apply(null, es.map(function (e) { return e.ev; })) : -1;
    });
    // Group by game day (chronological); within a day, edges first then kickoff.
    var byDay = {};
    games.forEach(function (g) { (byDay[dayKey(g.date)] = byDay[dayKey(g.date)] || []).push(g); });
    var keys = Object.keys(byDay).sort();
    var out = keys.map(function (k) {
      var list = byDay[k].slice().sort(function (x, y) {
        return (y._bestEv - x._bestEv) || (x.date || "").localeCompare(y.date || "");
      });
      var cards = list.map(gameCard).join("");
      if (!cards.trim()) return "";
      var label = k === "zzz" ? "Date TBD" : dayLabel(list[0].date);
      return '<div class="bet-day"><div class="bet-day-head">📅 ' + esc(label) +
        ' <span class="muted">· ' + list.length + " game" + (list.length !== 1 ? "s" : "") + "</span></div>" +
        '<div class="bet-cards">' + cards + "</div></div>";
    }).join("");
    if (!out.trim()) return '<p class="muted">No games match the current filters.</p>';
    return out;
  }

  // ---- ratings view --------------------------------------------------
  function ratingsView() {
    if (!state.ratings || state.ratings.league !== state.league) {
      api("/api/betting/rankings?league=" + state.league).then(function (r) {
        state.ratings = r; if (state.view === "ratings") render();
      });
      return '<p class="muted">Loading power ratings…</p>';
    }
    var teams = state.ratings.teams.slice(0, 40);
    var rows = teams.map(function (t) {
      return "<tr><td class=\"num\">" + t.rank + "</td><td>" + esc(t.abbr || t.name || t.id) + "</td>" +
        '<td class="num"><strong>' + t.rating + "</strong></td><td class=\"num muted\">" + t.gp + "</td></tr>";
    }).join("");
    return (
      '<div class="bet-ratings">' +
      '<p class="muted">Elo power ratings — the model’s core. ~' + (state.league === "nfl" ? "25" : "27") +
      " Elo points ≈ 1 point of spread. Updated after every game." +
      (state.ratings.as_of ? " Through " + esc((state.ratings.as_of || "").slice(0, 10)) + "." : "") + "</p>" +
      '<table class="bet-table bet-rank-table"><thead><tr><th>#</th><th>Team</th><th>Elo</th><th>GP</th></tr></thead><tbody>' +
      rows + "</tbody></table></div>"
    );
  }

  // ---- game detail modal (the model's reasoning) --------------------
  function detailEl() {
    var el = document.getElementById("bet-detail-modal");
    if (!el) {
      el = document.createElement("div");
      el.id = "bet-detail-modal";
      el.className = "bet-modal";
      el.innerHTML = '<div class="bet-modal-backdrop"></div><div class="bet-modal-panel" role="dialog" aria-modal="true">' +
        '<button class="bet-modal-close" aria-label="Close">✕</button><div id="bet-modal-body"></div></div>';
      document.body.appendChild(el);
      el.querySelector(".bet-modal-backdrop").onclick = closeGameDetail;
      el.querySelector(".bet-modal-close").onclick = closeGameDetail;
      document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeGameDetail(); });
    }
    return el;
  }
  function closeGameDetail() { var el = document.getElementById("bet-detail-modal"); if (el) el.classList.remove("open"); }

  function openGameDetail(gameId) {
    var el = detailEl();
    el.classList.add("open");
    document.getElementById("bet-modal-body").innerHTML = '<p class="muted">Loading the model’s reasoning…</p>';
    api("/api/betting/explain?league=" + state.league + "&game_id=" + encodeURIComponent(gameId))
      .then(function (exp) {
        if (exp.error) { document.getElementById("bet-modal-body").innerHTML = '<p class="bet-err">' + esc(exp.error) + "</p>"; return; }
        document.getElementById("bet-modal-body").innerHTML = detailHtml(exp);
      })
      .catch(function (e) { document.getElementById("bet-modal-body").innerHTML = '<p class="bet-err">' + esc(e.message || e) + "</p>"; });
  }

  function detailHtml(exp) {
    var p = exp.projection, h = exp.home, a = exp.away, m = exp.market || {};
    var favA = p.proj_margin < 0 ? " bet-winner" : "";
    var favH = p.proj_margin > 0 ? " bet-winner" : "";
    var mline = m.home_spread != null ? (h.abbr + " " + sg(m.home_spread) + " · O/U " + (m.total != null ? m.total : "—") +
      (m.home_ml != null ? " · ML " + odds(m.home_ml) + "/" + odds(m.away_ml) : "")) : "no market line";

    var scoreboard =
      '<div class="bet-md-score">' +
      '  <div class="bet-md-team' + favA + '">' + (a.rank ? '<span class="bet-rank">#' + a.rank + "</span> " : "") + esc(a.full || a.abbr) +
      '    <span class="bet-md-pts">' + p.proj_away_score.toFixed(0) + "</span></div>" +
      '  <div class="bet-md-team' + favH + '">' + (h.rank ? '<span class="bet-rank">#' + h.rank + "</span> " : "") + esc(h.full || h.abbr) +
      '    <span class="bet-md-pts">' + p.proj_home_score.toFixed(0) + "</span></div>" +
      "</div>";

    var summary =
      '<div class="bet-md-summary">' +
      '<div><span class="muted">Model line</span><strong>' + esc(a.abbr) + " " + sg(-p.proj_margin) + "</strong></div>" +
      '<div><span class="muted">Total</span><strong>' + p.proj_total.toFixed(1) + "</strong></div>" +
      '<div><span class="muted">Win prob</span><strong>' + esc(h.abbr) + " " + Math.round(p.home_win_prob * 100) + "%</strong></div>" +
      '<div><span class="muted">Market</span><strong>' + esc(mline) + "</strong></div>" +
      "</div>";

    var narr = '<div class="bet-md-narrative">' + exp.narrative.map(function (s) { return "<p>" + esc(s) + "</p>"; }).join("") + "</div>";

    var factors = '<h4 class="bet-md-h">How the margin was built</h4><table class="bet-table bet-md-factors"><tbody>' +
      exp.factors.map(function (f) {
        return "<tr><td>" + esc(f.label) + '</td><td class="num"><strong>' + esc(f.value) + "</strong></td><td class=\"muted\">" + esc(f.detail) + "</td></tr>";
      }).join("") + "</tbody></table>";

    var bets;
    if (exp.bets && exp.bets.length) {
      bets = '<h4 class="bet-md-h">Where the model sees a bet</h4>' + exp.bets.map(function (e) {
        return '<div class="bet-md-bet"><div class="bet-md-bet-head">' + marketTag(e.market) +
          " <strong>" + esc(e.pick) + "</strong> " + confBadge(e.confidence) +
          ' <span class="bet-ev pos">' + evPct(e.ev) + " EV</span></div>" +
          '<p class="muted">' + esc(e.why) + "</p></div>";
      }).join("");
    } else {
      bets = '<h4 class="bet-md-h">Where the model sees a bet</h4><p class="muted">' +
        esc(exp.bets_note || "No bet flagged — the model agrees with the market on this game.") + "</p>";
    }

    var caveat = exp.confidence_note ? '<div class="bet-md-caveat">⚠ ' + esc(exp.confidence_note) + "</div>" : "";

    return '<div class="bet-md-title">' + esc(exp.matchup) + '<span class="muted"> · ' + esc(exp.status || "") + "</span></div>" +
      scoreboard + summary + caveat + narr + factors + bets;
  }

  // ---- events --------------------------------------------------------
  function wire() {
    var p = panel();
    if (!p) return;
    p.querySelectorAll(".bet-lgbtn").forEach(function (b) {
      b.onclick = function () { state.league = b.dataset.lg; state.board = null; state.accuracy = null; state.ratings = null; state.strategies = null; state.lab = null; ensureAndLoad(); };
    });
    p.querySelectorAll(".bet-vtab").forEach(function (b) {
      b.onclick = function () { state.view = b.dataset.view; render(); };
    });
    p.querySelectorAll(".bet-wk-row").forEach(function (row) {
      row.onclick = function () {
        var wk = row.dataset.week; state.expanded[wk] = !state.expanded[wk]; render();
      };
    });
    var lwLink = p.querySelector(".bet-lw-link[data-week]");
    if (lwLink) lwLink.onclick = function () { state.expanded[lwLink.dataset.week] = true; render(); };
    p.querySelectorAll(".bet-strat-card[data-strat]").forEach(function (c) {
      c.onclick = function () {
        var s = c.dataset.strat; state.stratDetail = (state.stratDetail === s ? null : s); render();
        var d = panel().querySelector(".bet-strat-detail"); if (d) d.scrollIntoView({ behavior: "smooth", block: "nearest" });
      };
    });
    bind(p, "#bet-strat-close", function () { state.stratDetail = null; render(); });
    bind(p, "#bet-locknow", doLockNow);
    bind(p, "#bet-recal", doRecalibrate);
    p.querySelectorAll("[data-game-id]").forEach(function (el) {
      el.style.cursor = "pointer";
      el.addEventListener("click", function () {
        var id = el.getAttribute("data-game-id");
        if (id) openGameDetail(id);
      });
    });
    var seedGo = p.querySelector("#bet-seed-go");
    if (seedGo) seedGo.onclick = doSeed;
    bind(p, "#bet-rebuild", doRebuild);
    bind(p, "#bet-update", doUpdate);
    bind(p, "#bet-odds", promptOddsKey);
    bind(p, "#bet-cfbd", promptCfbdKey);
    bind(p, "#bf-market", null, "change", function (el) { state.filters.market = el.value; render(); });
    bind(p, "#bf-conf", null, "change", function (el) { state.filters.conf = el.value; render(); });
    bind(p, "#bf-edge", null, "input", function (el) { state.filters.minEdge = parseFloat(el.value) || 0; render(); });
    bind(p, "#bf-onlyedges", null, "change", function (el) { state.filters.onlyEdges = el.checked; render(); });
  }

  function bind(root, sel, onClick, evt, fn) {
    var el = root.querySelector(sel);
    if (!el) return;
    if (onClick) el.onclick = onClick;
    if (evt && fn) el.addEventListener(evt, function () { fn(el); });
  }

  function toast(m) { if (window.toast) window.toast(m); else console.log(m); }

  function doSeed() {
    var yrs = state.league === "nfl" ? [2024, 2025] : [2024, 2025];
    panel().innerHTML = header() + '<div class="bet-working">Building the ' + esc(labelOf(state.league)) + " model from " + yrs.join("–") + " results… this runs a couple thousand games through Elo. Please wait.</div>";
    api("/api/betting/seed", post({ league: state.league, years: yrs }))
      .then(function () { return loadStatus(); })
      .then(function () { toast("Model built."); return loadBoard(); })
      .catch(function (e) { panel().innerHTML = header() + errBox(e); wire(); });
  }

  function doUpdate() {
    toast("Pulling results & updating the model…");
    api("/api/betting/update", post({ league: state.league }))
      .then(function (r) {
        var st = r.strategies || {};
        var settled = (st.settle && st.settle.settled) || 0;
        toast("Learned " + (r.new_finals_learned || 0) + " new finals · " + (r.predictions_settled || 0) + " picks graded · " + settled + " strategy bets settled.");
        state.board = null; state.accuracy = null; state.strategies = null; return loadBoard();
      })
      .catch(function (e) { toast("Update failed: " + (e.message || e)); });
  }

  function doRebuild() {
    var lg = labelOf(state.league);
    if (!window.confirm("Rebuild the " + lg + " model from scratch?\n\n" +
      "This reseeds from the last two completed seasons, recalibrates, " +
      (state.league === "cfb" ? "reseeds preseason priors, " : "") + "and rebuilds EPA.\n\n" +
      "It DISCARDS any in-season learning. Only do this for initial setup or repair — " +
      "normally the model updates itself.")) return;
    state.labBusy = "Rebuilding the " + lg + " model (seed → calibrate → " +
      (state.league === "cfb" ? "priors → " : "") + "EPA)…";
    render();
    api("/api/betting/rebuild", post({ league: state.league }), 300000)
      .then(function (r) {
        state.labBusy = null; state.lab = null; state.board = null; state.strategies = null; state.ratings = null;
        var c = r.calibration || {};
        toast("Rebuilt " + lg + ": " + (r.seeded && r.seeded.games_processed || 0) + " games seeded, MAE " +
          (c.baseline_mae) + "→" + (c.tuned_mae) + ".");
        return loadStatus();
      })
      .then(function () { render(); })
      .catch(function (e) { state.labBusy = null; toast("Rebuild failed: " + (e.message || e)); render(); });
  }

  function doRecalibrate() {
    state.labBusy = "Searching parameters against " + labelOf(state.league) + " history and applying the best…";
    render();
    api("/api/betting/calibrate", post({ league: state.league }), 180000)
      .then(function (rep) {
        state.labBusy = null; state.lab = null; state.board = null;
        var t = rep.tuned_train || {}, b = rep.baseline || {};
        toast("Recalibrated: margin MAE " + (b.margin_mae) + " → " + (t.margin_mae) + " pts. Applied to the live model.");
        render();
      })
      .catch(function (e) { state.labBusy = null; toast("Calibration failed: " + (e.message || e)); render(); });
  }

  function doLockNow() {
    api("/api/betting/lock", post({ league: state.league }))
      .then(function (r) {
        toast(r.locked ? ("Locked " + r.locked + " game(s) within the window.") : "No games are within the lock window right now.");
        state.strategies = null; render();
      })
      .catch(function (e) { toast("Lock failed: " + (e.message || e)); });
  }

  function promptOddsKey() {
    var cur = state.status && state.status.odds_api_configured;
    var msg = "Paste a free The Odds API key (the-odds-api.com) for multi-book line shopping.\n" +
      (cur ? "A key is already set. Enter a new one to replace it, or leave blank to keep it." : "Leave blank to cancel.");
    var key = window.prompt(msg, "");
    if (key === null) return;
    api("/api/betting/config", post({ odds_api_key: key.trim() }))
      .then(function (r) {
        toast(r.odds_api_configured ? "Odds API key saved — multi-book on." : "Odds API key cleared.");
        return loadStatus();
      })
      .then(function () { state.board = null; return loadBoard(); });
  }

  function promptCfbdKey() {
    var cur = state.status && state.status.cfbd_configured;
    var key = window.prompt("Paste a free CollegeFootballData API key (collegefootballdata.com/key) for college betting lines + EPA ratings.\n" +
      (cur ? "A key is already set. Enter a new one to replace it." : "Leave blank to cancel."), "");
    if (key === null) return;
    api("/api/betting/config", post({ cfbd_api_key: key.trim() }))
      .then(function (r) {
        toast(r.cfbd_configured ? "CFBD key saved. Run “Update from results” to build college lines + EPA." : "CFBD key cleared.");
        state.board = null; state.lab = null; return loadStatus();
      })
      .then(function () { render(); });
  }

  function post(obj) {
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) };
  }

  // ---- lifecycle -----------------------------------------------------
  function labelOf(lg) { return lg === "cfb" ? "College (FBS)" : "NFL"; }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  function ensureAndLoad() {
    var lg = state.status && state.status.leagues[state.league];
    if (lg && lg.seeded) return loadBoard();
    render(); // shows seed prompt
  }

  var initialized = false;
  function show() {
    render();  // show header/spinner immediately so the tab never looks frozen
    if (!initialized) {
      initialized = true;
      loadStatus().then(function () { ensureAndLoad(); }).catch(function () { /* handled in render */ });
    } else if (state.statusError) {
      state.statusError = null;
      loadStatus().then(function () { ensureAndLoad(); }).catch(function () {});
    } else if (!state.board) {
      ensureAndLoad();
    }
  }

  window.Betting = { show: show };
})();
