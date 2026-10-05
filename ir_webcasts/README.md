# IR Webcasts

A website that collects links to the webcasts posted on the investor-relations
(IR) pages of every company listed on a U.S. exchange: earnings calls, broker
and industry conferences, investor days and shareholder meetings.

* **Webcasts** (`index.html`): every webcast, newest first. Filter by sector, event type,
  exchange and date (upcoming, past 30/90/365 days, by year), or search by company,
  ticker or conference. Filters are saved in the URL, so a view like
  `index.html?sector=Health+Care&type=Conference` can be bookmarked and shared.
* **Conferences** (`conferences.html` → `conference.html?id=…`): presentations from the
  same conference are grouped onto one landing page with its dates, host bank and
  presenting companies. The page can be filtered by sector.
* **Companies** (`companies.html` → `company.html?id=…` or `company.html?t=TICKER`): one
  landing page per company with its IR site link, sector, industry and all of its webcasts.

The built site is written to `docs/webcasts/`, so it is served by this repo's GitHub Pages
at `/webcasts/`.

## How the data is collected

```
SEC company list ─▶ universe ─▶ sec-mine / sec-recent ─▶ discover ─▶ crawl ─▶ build
 (tickers, SIC)      companies    8-K press releases       IR site     webcast   static
                                  → webcast links,         per         links     site
                                    company domain/IR URL  company
```

| Step | What it does | Source |
|---|---|---|
| `universe` | Every Nasdaq/NYSE/CBOE ticker (OTC with `--include-otc`), grouped by company (CIK). The SIC code is mapped to one of 11 GICS-style sectors. | `sec.gov/files/company_tickers_exchange.json`, `data.sec.gov/submissions` |
| `sec-mine` | Backfill. Reads each company's recent 8-K/6-K press releases that mention "webcast". Direct webcast links are saved, and the company's own domain and IR URL are recorded as hints (EDGAR doesn't publish company websites). | EDGAR full-text search |
| `sec-recent` | Daily. Reads every 8-K/6-K from the last few days that mentions a webcast. | EDGAR full-text search |
| `discover` | Finds the IR site. Order of evidence: `data/overrides.csv`, then IR URLs from the company's own press releases, then common locations (`investors.<domain>`, `<domain>/investors`, …), then an "Investors" link on the homepage. A page is accepted only if it reads like an IR site. | company websites |
| `crawl` | Opens the IR home page, follows events/presentations links on the same site, then event-detail pages. Collects links to known webcast hosts (Q4, Notified/media-server, webcasts.com, Chorus Call, wsw.com, Kaleido, …) and links labelled webcast/listen/replay. Each link gets a title and date from its surrounding event block. | IR sites |
| `build` | Classifies event types, groups conference webcasts into conferences and writes the site plus compact JSON. | — |

**Conference grouping** (`irwebcasts/conferences.py`): the conference name is pulled out of
each title and normalized. That means aligning bank aliases (J.P. Morgan / JPMorgan / JPM,
BofA / Bank of America, Cowen / TD Cowen, …), dropping ordinals and "Annual", and keying on
the year. So "J.P. Morgan 44th Annual Healthcare Conference" and "Fireside chat at the 2026
JPMorgan Healthcare Conference" land on the same page. Webcasts on conference platforms
that share an event code (e.g. `wsw.com/webcast/jpm44/...`) are merged even when a title
leaves the conference name out. If two names should be one conference, add the pair to
`data/conference_aliases.json`.

## Running it

```bash
cd ir_webcasts
pip install -r requirements.txt
export IRW_CONTACT=you@example.com        # SEC requires a contact address in the User-Agent

python -m irwebcasts universe              # ~8k companies; first run ~20 min (SEC rate limit)
python -m irwebcasts sec-mine --limit 500  # resumable; skips companies already mined
python -m irwebcasts discover --minutes 30
python -m irwebcasts crawl --minutes 60    # oldest-crawled first, so repeated runs rotate
python -m irwebcasts build                 # → ../docs/webcasts
python -m http.server -d ../docs/webcasts  # preview at http://localhost:8000
python -m unittest discover -s tests
```

Useful flags: `universe --limit 200` (only the 200 largest companies), `crawl --tickers AAPL MSFT`,
and `crawl --render`, which uses a headless browser for IR pages built in JavaScript (needs
`pip install playwright && playwright install chromium`).

The workflow `.github/workflows/ir-webcasts.yml` runs everything daily and commits
`ir_webcasts/data/` and `docs/webcasts/`. Set the repository variable `IRW_CONTACT`
(Settings → Secrets and variables → Actions → Variables) before enabling it. Each run gets
about 3½ hours of crawling, so the full universe cycles through in about 2–3 weeks.

## Fixing data by hand

`data/overrides.csv` holds per-ticker corrections for the IR URL, website or sector. They are applied on
every `universe` run. Use it for companies the discovery step can't find and for SIC codes
that map to the wrong sector (e.g. Alphabet's SIC 7370 says "IT" but GICS says
"Communication Services").

## Data files

* `data/companies.json`: `{id: {id, country, cik, tickers[], name, exchange, sic, industry, sector, ir_url, ir_source, hints, last_crawled, crawl_status, listed}}`
* `data/webcasts.json`: `{id: {id, company_id, url, title, date, source, source_url, first_seen, last_seen}}`

Company ids are `us-<CIK>`, so a ticker change doesn't break a company's history. Every
company also carries a `country`.

## Adding non-U.S. exchanges later

The pipeline isn't tied to SEC except for the `universe` and `sec-*` steps:

1. Add a universe loader per market that emits companies with a non-`us-` id
   (e.g. `gb-<LEI>` or `<exchange>-<ticker>`), `country`, `exchange`, `website` if the
   listing source has one, and a sector. LEI + GLEIF data is a good cross-market key.
2. `discover`, `crawl` and `build` work unchanged. Most of the providers above (Investis,
   Royalcast, Openbriefing, EQS, LSEG) already appear in the webcast-host list.
3. Add a country/exchange filter to the webcasts and companies pages. `country` is
   already in the data.

## Known limits and next steps

* **JS-only IR sites.** Some Q4 and custom sites render their event lists in the browser.
  `--render` handles them, but it's slower. A Q4-specific adapter that reads the site's own events
  feed would be faster.
* **Crawl speed.** Requests run one at a time, with one request per second per host. Crawling
  several hosts in parallel would shorten a full pass from weeks to days.
* **Sector accuracy.** SIC→sector is approximate. Use overrides, or swap in a GICS or
  NAICS source if you have one licensed.
* **Search/SEO.** Company and conference pages render in the browser from JSON. If you
  want them indexed by search engines, pre-render one HTML page per company and conference in `build`.
* **Storage.** JSON files are fine to start with. At several hundred thousand webcasts, move to SQLite or
  Postgres and split the site JSON by year.
