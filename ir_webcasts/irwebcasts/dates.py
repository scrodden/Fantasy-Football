"""Find the first calendar date mentioned in a snippet of IR-page text."""
import re
from datetime import date

_MONTHS = {
    m: i + 1 for i, names in enumerate([
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
        ("dec", "december"),
    ]) for m in names
}
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"

_PATTERNS = [
    # 2026-01-13
    (re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b"), lambda m: (m[1], m[2], m[3])),
    # January 13, 2026 / Jan. 13 2026 / Jan 13th, 2026
    (re.compile(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I),
     lambda m: (m[3], _MONTHS[m[1].lower().rstrip(".")], m[2])),
    # 13 January 2026
    (re.compile(r"\b(\d{1,2})\s+" + _MONTH_RE + r",?\s+(20\d\d)\b", re.I),
     lambda m: (m[3], _MONTHS[m[2].lower().rstrip(".")], m[1])),
    # 01/13/2026 (U.S. order)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b"), lambda m: (m[3], m[1], m[2])),
]


def find_all(text):
    """Every date in text as [(position, YYYY-MM-DD)], in reading order."""
    out = []
    for pattern, parts in _PATTERNS:
        for m in pattern.finditer(text or ""):
            try:
                y, mo, d = (int(x) for x in parts(m))
                out.append((m.start(), date(y, mo, d).isoformat()))
            except (ValueError, KeyError):
                continue
    return sorted(out)


def find_date(text):
    """Return the first date in text as YYYY-MM-DD, or None."""
    found = find_all(text)
    return found[0][1] if found else None


_EVENT_LEAD = re.compile(r"(?:\bon|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day,?)\s*$", re.I)


def find_event_date(text, earliest=None, latest=None):
    """In a press release, the date an event happens rather than the dateline.

    Prefers a date introduced by "on" or a weekday ("on Tuesday, June 9, 2026").
    earliest/latest (YYYY-MM-DD) drop dates that can't be the event, such as
    a dividend payment date a year out.
    """
    found = [(pos, v) for pos, v in find_all(text)
             if (not earliest or v >= earliest) and (not latest or v <= latest)]
    for pos, value in found:
        if _EVENT_LEAD.search(text[max(0, pos - 20):pos]):
            return value
    return found[0][1] if found else None


def strip_dates(text):
    """Remove date-ish fragments so they don't pollute titles."""
    for pattern, _ in _PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"\b\d{1,2}:\d\d\s*(?:a\.?m\.?|p\.?m\.?)?(?:\s*(?:ET|EST|EDT|CT|PT|GMT|UTC))?", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -–|,:")
