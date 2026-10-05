"""Pull webcast links (with a title and date) out of an IR page's HTML.

IR sites come from a handful of vendors (Q4, Notified/GlobeNewswire, S&P,
EQS, ...) plus custom builds, so rather than per-vendor parsers this works on
two generic signals: links to known webcast hosts, and links whose text or URL
says "webcast"/"listen"/"replay". The surrounding list item / table row / card
supplies the event title and date.
"""
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .dates import find_date, strip_dates

# Hosts that only serve webcasts/event players, so any link to them counts.
WEBCAST_HOSTS = (
    "edge.media-server.com", "webcasts.com", "events.q4inc.com",
    "onlinexperiences.com", "wsw.com", "kvgo.com", "viavid.com", "choruscall.com",
    "veracast.com", "on24.com", "webinar.net", "openbriefing.com", "investis-live.com",
    "world-television.com", "royalcast.com", "mediasite.com", "netroadshow.com",
    "streamstudio.com", "webcaster4.com", "brrmedia.co.uk", "media-server.com",
    "notified.com", "lsegissuerservices.com", "mzwebcast.com", "webcastlite.mziq.com",
    "conferencingportals.com", "incommconferencing.com",
    "irwebcasting.com", "webcastgroup.com", "gowebcasting.com",
    "cuepoint.com", "event.choruscall.com", "virtualshareholdermeeting.com",
    "meetnow.global", "lumiconnect.com",
)

# Hosts that are dial-in registration pages, not webcasts.
EXCLUDED_HOSTS = ("register.vevent.com", "vevent.com", "register-conf.media-server.com")

_LINK_TEXT_RE = re.compile(
    r"\b(webcast|listen|replay|archived? (?:audio|presentation|event)|live audio|"
    r"watch (?:the )?(?:live|replay|webcast|presentation|event|video)|audio archive|"
    r"view (?:the )?(?:webcast|replay|event|presentation recording))\b", re.I)
_URL_RE = re.compile(r"webcast|/player|replay", re.I)
_SKIP_URL_RE = re.compile(r"\.(pdf|pptx?|xlsx?|docx?|jpg|png|zip)(\?|$)|^mailto:|^tel:|^javascript:|podcast", re.I)

# Words that describe the link rather than the event.
_GENERIC = re.compile(
    r"^(webcast|live webcast|listen|listen to (?:the )?(?:webcast|replay|call)|replay|"
    r"webcast replay|watch|watch (?:the )?(?:webcast|replay|now)|view|view (?:webcast|replay|event|details)|"
    r"click here|here|audio|presentation|slides|transcript|event details|details|more|"
    r"register|add to calendar|download|pdf|play|archive|archived webcast|upcoming events?|"
    r"past events?|events?|and presentations|events & presentations|events and presentations|"
    r"read more|learn more|\W*)$",
    re.I)

_CONTAINERS = {"li", "tr", "article", "section", "div", "dd", "td", "p"}

# Links on an IR page that lead to events/presentations listings.
EVENTS_LINK_RE = re.compile(
    r"\b(events?|presentations?|webcasts?|calendar|conferences?)\b", re.I)
EVENT_DETAIL_RE = re.compile(r"event[-_]?details?|/events?/(?:detail|\d)|eventid=|/event/", re.I)


def is_webcast_url(url):
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if any(host == h or host.endswith("." + h) for h in EXCLUDED_HOSTS):
        return False
    if parts.path in ("", "/") and not parts.query:
        return False  # a provider's home page, not an event
    if "/analyst/" in parts.path or "pwd=" in parts.query:
        return False  # Q&A access for sell-side participants
    return any(host == h or host.endswith("." + h) for h in WEBCAST_HOSTS)


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _is_generic(text):
    return bool(_GENERIC.match(_clean(text))) or len(_clean(text)) < 6


def _event_blocks(a):
    """Ancestors of a link, innermost first, that could be one event's block."""
    node = a
    for _ in range(8):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            return
        if node.name not in _CONTAINERS:
            continue
        text = _clean(node.get_text(" "))
        if len(text) < 12:
            continue
        # Stop before climbing into a list that holds several events.
        if len(text) > 600 or _count_webcast_links(node) > 3:
            return
        yield node


def _count_webcast_links(node):
    return sum(1 for a in node.find_all("a", href=True) if _looks_like_webcast(a, a["href"]))


def _looks_like_webcast(a, href):
    if _SKIP_URL_RE.search(href):
        return False
    if is_webcast_url(href):
        return True
    host = urlsplit(href).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in EXCLUDED_HOSTS):
        return False
    text = _clean(a.get_text(" ")) or a.get("title", "") or a.get("aria-label", "")
    return bool(_LINK_TEXT_RE.search(text) or _URL_RE.search(urlsplit(href).path))


_TITLE_CLASS = re.compile(r"title|headline|heading|name|subject", re.I)


def _usable(text):
    text = strip_dates(_clean(text))
    if len(text) < 8 or _is_generic(text) or re.fullmatch(r"[\d\s:/.,apmAPMET-]+", text):
        return None
    return text[:200]


def _title_in(node, use_links=False):
    """Event title inside a block: headline-like elements, then plain text,
    then links that aren't webcast/registration links (event-detail links)."""
    for el in node.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b"]) + \
            node.find_all(class_=_TITLE_CLASS):
        if (text := _usable(el.get_text(" "))):
            return text
    for string in node.find_all(string=True):
        if string.find_parent("a") is None and string.parent.name not in ("script", "style"):
            if (text := _usable(string)):
                return text
    if not use_links:
        return ""
    for link in node.find_all("a", href=True):
        href = link["href"]
        host = urlsplit(href).netloc.lower()
        if (_SKIP_URL_RE.search(href) or _looks_like_webcast(link, href)
                or any(host.endswith(h) for h in EXCLUDED_HOSTS)):
            continue
        if (text := _usable(link.get_text(" "))):
            return text
    return ""


def _describe(a):
    """(title, date) for a webcast link, from the link itself or its block."""
    anchor = _clean(a.get_text(" ")) or _clean(a.get("title", ""))
    title = _usable(anchor) if not _LINK_TEXT_RE.search(anchor) else None
    date = find_date(anchor)
    blocks = list(_event_blocks(a))
    for node in blocks:
        title = title or _title_in(node)
        date = date or find_date(_clean(node.get_text(" ")))
        if title and date:
            break
    for node in blocks:
        title = title or _title_in(node, use_links=True)
    return title or "", date


def extract_webcasts(html, base_url):
    """Return [{url, title, date}] for every webcast link on the page."""
    soup = BeautifulSoup(html, "lxml")
    found = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not href.startswith("http") or href in found or not _looks_like_webcast(a, href):
            continue
        if href.rstrip("/") == base_url.rstrip("/"):
            continue
        title, date = _describe(a)
        found[href] = {"url": href, "title": title, "date": date}
    return list(found.values())


def find_links(html, base_url, pattern):
    """Links whose text (or path) matches pattern, as absolute URLs."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip()).split("#")[0]
        if not href.startswith("http") or _SKIP_URL_RE.search(href):
            continue
        text = _clean(a.get_text(" "))
        if pattern.search(text) or pattern.search(urlsplit(href).path):
            if href not in out:
                out.append(href)
    return out


def page_heading(html):
    """Best-effort event title for an event-detail page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["h1", "h2"]):
        text = strip_dates(_clean(tag.get_text(" ")))
        if len(text) >= 8 and not _is_generic(text):
            return text[:200], find_date(_clean(soup.get_text(" "))[:3000])
    return "", None
