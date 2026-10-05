"""Paths and tunables shared by every pipeline step."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
# The built site lands inside the repo's GitHub Pages folder by default.
SITE_DIR = Path(os.environ.get("IRW_SITE_DIR", ROOT.parent / "docs" / "webcasts"))

COMPANIES_FILE = DATA_DIR / "companies.json"
WEBCASTS_FILE = DATA_DIR / "webcasts.json"
OVERRIDES_FILE = DATA_DIR / "overrides.csv"
CONFERENCE_ALIASES_FILE = DATA_DIR / "conference_aliases.json"

# SEC asks automated clients to identify themselves with a contact address:
# https://www.sec.gov/os/accessing-edgar-data
CONTACT = os.environ.get("IRW_CONTACT", "")
USER_AGENT = os.environ.get(
    "IRW_USER_AGENT",
    f"IRWebcastsBot/0.1 ({CONTACT or 'set IRW_CONTACT to an email address'})",
)

# Exchanges (as named in SEC's company_tickers_exchange.json) that count as
# "listed on a U.S. exchange". OTC is opt-in with --include-otc.
US_EXCHANGES = ("Nasdaq", "NYSE", "CBOE")

# Seconds between requests to the same host. SEC allows 10 req/s.
SEC_DELAY = 0.15
SITE_DELAY = 1.0
TIMEOUT = 20
