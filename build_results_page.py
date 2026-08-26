#!/usr/bin/env python3
"""Generate a static results page (docs/index.html) for GitHub Pages.

A read-only snapshot the scheduled workflow regenerates every run: each week's
strategy totals, season P&L, and this week's top edges. Bookmark the Pages URL
and you never have to open the app to check how the strategies are doing.
"""

from __future__ import annotations

import datetime as _dt
import html
import os

from betting import data, strategies, train

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html")


def esc(x):
    return html.escape(str(x if x is not None else ""))


def money(n):
    n = n or 0
    return ("+$" if n >= 0 else "-$") + f"{abs(n):.0f}"


def cls(n):
    return "pos" if (n or 0) > 0 else ("neg" if (n or 0) < 0 else "")


def latest_week_html(rep):
    lw = rep.get("latest_week")
    if not lw:
        return ('<p class="muted">No graded games yet this season. Once a slate '
                "finishes, its results appear here automatically.</p>")
    c = lw.get("combined", {})
    cards = ""
    for s in strategies.STRATEGIES:
        b = lw["strategies"][s]
        label = {"spread": "Spread", "moneyline": "Moneyline", "value": "High conviction"}[s]
        cards += (f'<div class="card"><div class="cardlabel">{esc(label)}</div>'
                  f'<div class="pnl {cls(b["profit"])}">{money(b["profit"])}</div>'
                  f'<div class="sub">{esc(b.get("record") or "0-0")}'
                  + (f' · {b["roi"]}%' if b.get("roi") is not None else "")
                  + (f' · <span class="pending">{b["pending"]} pending</span>' if b.get("pending") else "")
                  + "</div></div>")
    return (f'<div class="lwhead">Week {esc(lw["week"])} results '
            f'<span class="{cls(c.get("profit"))}">{esc(c.get("record",""))} '
            f'{money(c.get("profit"))}'
            + (f' ({c["roi"]}% ROI)' if c.get("roi") is not None else "") + "</span></div>"
            f'<div class="cards">{cards}</div>')


def totals_html(rep):
    rows = ""
    for s in strategies.STRATEGIES:
        t = rep["totals"][s]
        label = {"spread": "Spread — every game", "moneyline": "Moneyline — every game",
                 "value": "High conviction (value)"}[s]
        rows += (f"<tr><td>{esc(label)}</td><td>{esc(t.get('record') or '0-0')}</td>"
                 f'<td class="{cls(t["profit"])}">{money(t["profit"])}</td>'
                 f"<td>{t['roi'] if t.get('roi') is not None else '—'}%</td>"
                 f"<td>{t['n']}"
                 + (f" · {t['pending']} pending" if t.get("pending") else "") + "</td></tr>")
    return ('<table><thead><tr><th>Strategy</th><th>Record</th><th>Profit</th>'
            f"<th>ROI</th><th>Bets</th></tr></thead><tbody>{rows}</tbody></table>")


def edges_html(board):
    edges = (board.get("top_edges") or [])[:10]
    if not edges:
        return '<p class="muted">No edges flagged for the current slate.</p>'
    rows = ""
    for e in edges:
        mk = {"spread": "SPREAD", "total": "TOTAL", "moneyline": "ML"}.get(e["market"], e["market"])
        game = esc((e.get("away_full") or e.get("game", "")) + " @ " + (e.get("home_full") or ""))
        ev = e.get("ev", 0)
        rows += (f'<tr><td class="tag">{mk}</td><td class="gm">{game}</td>'
                 f'<td><b>{esc(e["pick"])}</b></td>'
                 f'<td>{round(e.get("win_prob",0)*100)}%</td>'
                 f'<td class="{cls(ev)}">{"+" if ev>=0 else ""}{round(ev*100,1)}%</td>'
                 f'<td>{esc(e.get("confidence",""))}</td></tr>')
    return ('<table><thead><tr><th>Mkt</th><th>Game</th><th>Pick</th><th>Win%</th>'
            f"<th>EV</th><th>Conf</th></tr></thead><tbody>{rows}</tbody></table>")


def league_section(lg):
    label = data.LEAGUE_LABEL[lg]
    try:
        rep = strategies.report(lg)
    except Exception as exc:
        rep = {"totals": {}, "latest_week": None}
        print(f"[page] {lg} report failed: {exc}")
    try:
        board = train.build_board(lg)
        wk = board.get("week")
    except Exception as exc:
        board = {"top_edges": []}
        wk = None
        print(f"[page] {lg} board failed: {exc}")
    return (f'<section><h2>{esc(label)}' + (f" — Week {wk}" if wk else "") + "</h2>"
            f'<div class="panel latest">{latest_week_html(rep)}</div>'
            "<h3>Season totals</h3>"
            f'<div class="panel">{totals_html(rep) if rep.get("totals") else ""}</div>'
            "<h3>This week's top edges</h3>"
            f'<div class="panel">{edges_html(board)}</div></section>')


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Football Betting — Results</title>
<style>
:root{{--bg:#0e1116;--panel:#161b22;--elev:#1c232d;--bd:#2a323d;--tx:#e6edf3;--mut:#8b98a5;--grn:#3fb950;--red:#f85149;--blue:#58a6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5}}
.wrap{{max-width:900px;margin:0 auto;padding:1.4rem 1rem 4rem}}
h1{{margin:.2rem 0}}h2{{margin:1.8rem 0 .6rem;border-bottom:1px solid var(--bd);padding-bottom:.3rem}}h3{{margin:1.2rem 0 .5rem;font-size:1rem}}
.muted{{color:var(--mut)}}.updated{{color:var(--mut);font-size:.85rem}}
.panel{{background:var(--panel);border:1px solid var(--bd);border-radius:10px;padding:.9rem 1rem;margin-bottom:.6rem;overflow-x:auto}}
.latest{{border-color:var(--blue)}}
.lwhead{{font-weight:800;font-size:1.1rem;margin-bottom:.7rem;display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem}}
.card{{background:var(--elev);border:1px solid var(--bd);border-radius:8px;padding:.55rem .7rem}}
.cardlabel{{font-size:.78rem;color:var(--mut);font-weight:600}}
.pnl{{font-size:1.35rem;font-weight:800}}.sub{{font-size:.75rem;color:var(--mut);margin-top:.1rem}}
.pending{{color:var(--blue)}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th{{text-align:left;color:var(--mut);font-size:.72rem;text-transform:uppercase;padding:.4rem .5rem;border-bottom:1px solid var(--bd)}}
td{{padding:.4rem .5rem;border-bottom:1px solid var(--bd)}}
.pos{{color:var(--grn);font-weight:700}}.neg{{color:var(--red)}}
.tag{{font-size:.68rem;font-weight:700;color:var(--blue)}}.gm{{color:var(--mut)}}
footer{{margin-top:2rem;color:var(--mut);font-size:.8rem;line-height:1.6}}
</style></head><body><div class="wrap">
<h1>🏈 Football Betting — Results</h1>
<p class="updated">Auto-updated {updated} · regenerates every couple of hours</p>
{sections}
<footer>Model estimates for research/entertainment — not betting advice. Each game's
bets are locked ~24h before kickoff and graded against the result. This page is a
read-only snapshot; the full interactive app runs locally.</footer>
</div></body></html>"""


def main():
    sections = "".join(league_section(lg) for lg in data.LEAGUES)
    updated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(PAGE.format(updated=updated, sections=sections))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
