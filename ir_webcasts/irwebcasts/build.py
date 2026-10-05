"""Turn the collected data into the static website.

The site is a handful of HTML pages plus compact JSON files they read, so the
output stays small no matter how many companies we track (no per-company HTML).
"""
import json
import shutil
from datetime import datetime, timezone

from . import config, conferences, sectors, store


def build(out_dir=None):
    out = out_dir or config.SITE_DIR
    companies = store.load_companies()
    webcasts = list(store.load_webcasts().values())
    webcasts = [w for w in webcasts if w["company_id"] in companies]
    confs = conferences.build(webcasts, companies)

    counts = {}
    for w in webcasts:
        counts[w["company_id"]] = counts.get(w["company_id"], 0) + 1

    company_rows = []
    for c in companies.values():
        if not c.get("listed", True) and not counts.get(c["id"]):
            continue
        company_rows.append({
            "id": c["id"], "n": c.get("name") or c["id"], "t": c.get("tickers", []),
            "x": c.get("exchange"), "co": c.get("country", "US"), "s": c.get("sector", sectors.OTHER),
            "i": c.get("industry"), "ir": c.get("ir_url"), "w": counts.get(c["id"], 0),
        })
    company_rows.sort(key=lambda r: r["n"].lower())

    webcast_rows = [{
        "id": w["id"], "c": w["company_id"], "ti": w.get("title") or "Webcast", "u": w["url"],
        "d": w.get("date"), "ty": w.get("event_type", "Other"), "cf": w.get("conference_id"),
        "src": w.get("source_url"), "fs": w.get("first_seen", "")[:10],
    } for w in webcasts]
    # Newest first; undated links go last rather than masquerading as recent.
    webcast_rows.sort(key=lambda r: (r["d"] is not None, r["d"] or "", r["fs"]), reverse=True)

    conf_rows = sorted(confs, key=lambda c: (c.get("start") or c.get("year") or ""), reverse=True)

    meta = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sectors": sectors.SECTORS + [sectors.OTHER],
        "companies": len(company_rows),
        "companies_with_ir": sum(1 for r in company_rows if r["ir"]),
        "webcasts": len(webcast_rows),
        "conferences": len(conf_rows),
    }

    out.mkdir(parents=True, exist_ok=True)
    for f in config.WEB_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, out / f.name)
    data_dir = out / "data"
    data_dir.mkdir(exist_ok=True)
    for name, payload in (("companies", company_rows), ("webcasts", webcast_rows),
                          ("conferences", conf_rows), ("meta", meta)):
        with open(data_dir / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    return meta
