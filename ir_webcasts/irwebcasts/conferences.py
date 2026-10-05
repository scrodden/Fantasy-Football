"""Classify webcasts and group conference presentations into conferences.

The same event is written many ways across IR sites:
  "J.P. Morgan 44th Annual Healthcare Conference"
  "Fireside Chat at the 2026 JPMorgan Healthcare Conference"
  "JPM Healthcare Conference (January 13, 2026)"
We pull out the conference phrase, normalize it (host-bank aliases, ordinals,
"annual", years, punctuation) and key it with the year. Webcasts that share a
conference-platform event code (e.g. wsw.com/webcast/jpm44/...) are merged even
when one title omits the conference name. data/conference_aliases.json can map
stubborn variants onto one name.
"""
import json
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from . import config

_KEYWORD = r"(?:Conference|Summit|Symposium|Forum|Showcase|Expo|Investor Days?)"
_CONF_RE = re.compile(
    r"((?:[A-Z0-9][\w.&'’+/-]*|of|and|for|in|on|the|de|&|\+|-)(?:\s+(?:[A-Z0-9][\w.&'’+/-]*|of|and|for|in|on|the|de|&|\+|-))*?\s+"
    + _KEYWORD + r")(?!\s+(?:[Cc]alls?|ID|Id|[Ll]ine|[Nn]umber|[Cc]ode|[Oo]perator|[Rr]oom)\b)(?:\s+(?:20\d\d|on\s+(?!(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b)[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*){0,3}))?")
# Words that end up in front of the name but aren't part of it.
_LEAD_JUNK = re.compile(
    r"^(?:(?:the|at|in|to|a|an|of|and|for|on|will|present|presents|presenting|presentation|"
    r"participate|participates|participating|participation|fireside|chat|webcast|live|"
    r"company|management|ceo|cfo|host|hosts|virtual|annual|inaugural)\b\s*)+", re.I)
_NOT_CONFERENCE = re.compile(
    r"^(?:earnings|quarterly|results|investor|analyst|annual|shareholder|stockholder|special|"
    r"press|news|company|our|this|virtual|annual meeting)\s+" + _KEYWORD + r"$", re.I)

# Host-bank aliases (lowercase, applied to the normalized name).
BANK_ALIASES = [
    (r"\bj\.?\s?p\.?\s?morgan\b|\bjpm\b|\bjp morgan\b", "jpmorgan"),
    (r"\bbank of america(?: securities)?\b|\bbofa(?: securities| global research)?\b|\bbofaml\b|\bbaml\b", "bofa"),
    (r"\bgoldman sachs\b|\bgoldman\b", "goldman sachs"),
    (r"\b(?:td )?cowen\b|\btd securities\b", "td cowen"),
    (r"\b(?:svb )?leerink(?: partners)?\b", "leerink"),
    (r"\brbc(?: capital markets)?\b", "rbc"),
    (r"\bciti(?:group|bank)?\b", "citi"),
    (r"\bb\.?\s?riley(?: securities)?\b", "b riley"),
    (r"\bh\.?\s?c\.?\s?wainwright\b", "hc wainwright"),
    (r"\bevercore(?: isi)?\b", "evercore"),
    (r"\bkeybanc(?: capital markets)?\b", "keybanc"),
    (r"\bubs(?: securities)?\b", "ubs"),
    (r"\bwells fargo(?: securities)?\b", "wells fargo"),
    (r"\bpiper sandler\b|\bpiper jaffray\b", "piper sandler"),
    (r"\bcitizens jmp\b|\bjmp(?: securities)?\b", "citizens jmp"),
    (r"\b(?:robert w\.? )?baird\b", "baird"),
]
HOST_NAMES = {
    "jpmorgan": "J.P. Morgan", "bofa": "BofA", "goldman sachs": "Goldman Sachs",
    "morgan stanley": "Morgan Stanley", "td cowen": "TD Cowen", "leerink": "Leerink",
    "rbc": "RBC", "citi": "Citi", "b riley": "B. Riley", "hc wainwright": "H.C. Wainwright",
    "evercore": "Evercore ISI", "keybanc": "KeyBanc", "ubs": "UBS", "deutsche bank": "Deutsche Bank",
    "wells fargo": "Wells Fargo", "piper sandler": "Piper Sandler", "citizens jmp": "Citizens JMP",
    "baird": "Baird", "barclays": "Barclays", "jefferies": "Jefferies", "stifel": "Stifel",
    "raymond james": "Raymond James", "william blair": "William Blair", "oppenheimer": "Oppenheimer",
    "needham": "Needham", "guggenheim": "Guggenheim", "truist": "Truist", "bmo": "BMO",
    "canaccord": "Canaccord Genuity", "cantor": "Cantor", "wolfe": "Wolfe Research",
    "bernstein": "Bernstein", "mizuho": "Mizuho", "roth": "Roth", "craig-hallum": "Craig-Hallum",
    "lake street": "Lake Street", "sidoti": "Sidoti", "janney": "Janney", "kbw": "KBW",
    "nasdaq": "Nasdaq", "ld micro": "LD Micro", "jones": "JonesTrading", "benchmark": "Benchmark",
    "macquarie": "Macquarie", "scotiabank": "Scotiabank", "cibc": "CIBC", "seaport": "Seaport",
    "loop": "Loop Capital", "northland": "Northland", "chardan": "Chardan", "maxim": "Maxim",
    "ladenburg": "Ladenburg Thalmann", "alliance global": "A.G.P.", "noble": "Noble Capital",
}

# Conference platforms whose URL path carries a per-event code.
_EVENT_CODE_HOSTS = {"wsw.com": 1, "kvgo.com": 1}

_STOP = {"the", "annual", "virtual", "inaugural", "global", "securities", "investor", "investors", "a", "an"}


def extract_conference_name(text):
    """Return the conference phrase in text ("Wells Fargo Industrials Conference") or None."""
    if not text:
        return None
    text = re.sub(r"(?:https?://|www\.)\S+", " ", text)
    for m in _CONF_RE.finditer(text):
        name = _LEAD_JUNK.sub("", m.group(1)).strip(" -–:,")
        if len(name.split()) < 2 or _NOT_CONFERENCE.match(name):
            continue
        if re.search(r"\bconference call\b", text[m.start(): m.end() + 6], re.I):
            continue
        trailer = text[m.end(1): m.end()].strip()
        return re.sub(r"\s+", " ", f"{name} {trailer}".strip())
    return None


def classify(title):
    t = (title or "").lower()
    if re.search(r"annual (?:general |shareholders'? |stockholders'? )?meeting|\bagm\b|special meeting", t):
        return "Shareholder Meeting"
    if re.search(r"investor day|analyst day|capital markets day|investor meeting|strategy day|r&d day|innovation day", t):
        return "Investor Day"
    if extract_conference_name(title or "") or re.search(r"fireside chat", t):
        return "Conference"
    if re.search(r"earnings|quarter|\bq[1-4]\b|results|fiscal (?:year|20)|year[- ]end|\bfy\s?\d", t):
        return "Earnings"
    return "Other"


def _load_aliases():
    if config.CONFERENCE_ALIASES_FILE.exists():
        with open(config.CONFERENCE_ALIASES_FILE, encoding="utf-8") as f:
            return {k.lower(): v for k, v in json.load(f).items() if not k.startswith("_")}
    return {}


def normalize(name):
    """Canonical key for a conference name, without the year."""
    n = name.lower().replace("’", "'")
    n = re.sub(r"\b(?:19|20)\d\d\b", " ", n)
    n = re.sub(r"\b\d+(?:st|nd|rd|th)\b", " ", n)
    for pattern, repl in BANK_ALIASES:
        n = re.sub(pattern, repl, n)
    n = n.replace("&", " and ").replace("health care", "healthcare").replace("+", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    words = [w for w in n.split() if w not in _STOP]
    return " ".join(words)


def _event_code(url):
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    for h, idx in _EVENT_CODE_HOSTS.items():
        if host == h or host.endswith("." + h):
            segs = [s for s in parts.path.split("/") if s]
            if len(segs) > idx and segs[0].lower() == "webcast":
                return f"{h}:{segs[idx].lower()}"
    return None


def _year(title, date):
    m = re.search(r"\b(20\d\d)\b", title or "")
    if m:
        return m.group(1)
    return (date or "")[:4] or None


def host_of(key):
    """Host bank named in a normalized conference key, if any."""
    for k, display in HOST_NAMES.items():
        if re.search(rf"\b{re.escape(k)}\b", key):
            return display
    return None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build(webcasts, companies):
    """Assign conference ids to conference webcasts; return conference records.

    Mutates each webcast dict to add `event_type` and (when grouped)
    `conference_id`.
    """
    aliases = _load_aliases()
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    names = {}
    for w in webcasts:
        w["event_type"] = classify(w.get("title"))
        w.pop("conference_id", None)
        name = extract_conference_name(w.get("title") or "")
        code = _event_code(w["url"])
        if name:
            canon = aliases.get(normalize(name), name)
            year = _year(w.get("title"), w.get("date"))
            key = f"{normalize(canon)}|{year or 'undated'}"
            names[w["id"]] = (key, canon)
            parent.setdefault(key, key)
            w["event_type"] = "Conference"
            if code:
                union(code, key)
        elif code:
            names[w["id"]] = (code, None)
            parent.setdefault(code, code)

    groups = defaultdict(list)
    for w in webcasts:
        if w["id"] in names:
            groups[find(names[w["id"]][0])].append(w)

    conferences = {}
    for root, members in groups.items():
        display = Counter(names[w["id"]][1] for w in members if names[w["id"]][1])
        if not display:
            # Only an event code: worth a page only when several companies share it.
            if len({w["company_id"] for w in members}) < 2:
                continue
            display = Counter({f"Conference ({root.split(':', 1)[1]})": 1})
        name = _pretty(display.most_common(1)[0][0])
        dates = sorted(w["date"] for w in members if w.get("date"))
        key_names = [names[w["id"]][0] for w in members if names[w["id"]][1]]
        norm = key_names[0].split("|")[0] if key_names else normalize(name)
        year = (key_names[0].split("|")[1] if key_names else None) or (dates[0][:4] if dates else None)
        if year == "undated":
            year = dates[0][:4] if dates else None
        if year and year not in name:
            name = f"{name} {year}"
        cid = slugify(f"{norm} {year or ''}")
        sector_counts = Counter(companies[w["company_id"]].get("sector", "Other")
                                for w in members if w["company_id"] in companies)
        conf = conferences.setdefault(cid, {
            "id": cid, "name": name, "year": year, "host": host_of(norm),
            "start": None, "end": None, "companies": set(), "sectors": Counter(),
        })
        conf["start"] = min(filter(None, [conf["start"], dates[0] if dates else None]), default=None)
        conf["end"] = max(filter(None, [conf["end"], dates[-1] if dates else None]), default=None)
        conf["sectors"].update(sector_counts)
        for w in members:
            w["conference_id"] = cid
            w["event_type"] = "Conference"
            conf["companies"].add(w["company_id"])

    out = []
    for conf in conferences.values():
        conf["company_count"] = len(conf.pop("companies"))
        conf["sectors"] = dict(conf["sectors"].most_common())
        out.append(conf)
    return out


def _pretty(name):
    return re.sub(r"\s+", " ", name).strip()
