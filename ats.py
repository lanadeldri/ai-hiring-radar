"""
Public job-board (ATS) adapters, shared by fetch_jobs.py and discover.py.

Every adapter takes a board slug and returns a list of jobs:
    {title, location, url, posted, remote, department, country_hints?}

Supported: Ashby, Greenhouse, Lever (US + EU), Workable, SmartRecruiters,
           Recruitee, Personio, BambooHR, Breezy HR
"""
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (compatible; ai-hiring-radar/0.4; personal project)"}
NET_ERRORS = (HTTPError, URLError, ValueError, TimeoutError, ConnectionError, OSError, ET.ParseError)


def get(url, timeout=15):
    with urlopen(Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def get_json(url, timeout=15):
    return json.loads(get(url, timeout))


def _job(title, location, url, posted, remote=False, department="", country_hints=None):
    return {"title": (title or "").strip(), "location": location or "", "url": url, "posted": posted,
            "remote": bool(remote), "department": department or "", "country_hints": country_hints or []}


# ---------------------------------------------------------------- adapters

def fetch_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        sec = j.get("secondaryLocations") or []
        locs = [j.get("location") or ""] + [x.get("location", "") for x in sec]
        addrs = [j.get("address")] + [x.get("address") for x in sec]
        hints = [((a or {}).get("postalAddress") or {}).get("addressCountry", "") for a in addrs]
        out.append(_job(j.get("title"), "; ".join(l for l in locs if l), j.get("jobUrl"), j.get("publishedAt"),
                        bool(j.get("isRemote")) or j.get("workplaceType") == "Remote", j.get("department"),
                        [h for h in hints if h]))
    return out


def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_job(j.get("title"), loc, j.get("absolute_url"), j.get("first_published") or j.get("updated_at"),
                        "remote" in loc.lower()))
    return out


def _lever(slug, host):
    data = get_json(f"https://{host}/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cat = j.get("categories") or {}
        loc = "; ".join(cat.get("allLocations") or []) or cat.get("location") or ""
        ts = j.get("createdAt")
        out.append(_job(j.get("text"), loc, j.get("hostedUrl"),
                        datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
                        j.get("workplaceType") == "remote" or "remote" in loc.lower(), cat.get("team")))
    return out


def fetch_lever(slug):
    return _lever(slug, "api.lever.co")


def fetch_lever_eu(slug):
    return _lever(slug, "api.eu.lever.co")


def fetch_workable(slug):
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    return [_job(j.get("title"), ", ".join(x for x in [j.get("city"), j.get("country")] if x),
                 j.get("url") or j.get("shortlink"), j.get("published_on") or j.get("created_at"),
                 j.get("telecommuting"), j.get("department"), [j.get("country")] if j.get("country") else [])
            for j in data.get("jobs", [])]


def fetch_smartrecruiters(slug):
    out, offset = [], 0
    while True:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            text = loc.get("fullLocation") or ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
            out.append(_job(j.get("name"), text, f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                            j.get("releasedDate"), loc.get("remote"), (j.get("department") or {}).get("label")))
        offset += len(items)
        if not items or offset >= int(data.get("totalFound", 0)) or offset >= 2000:
            return out


def fetch_recruitee(slug):
    data = get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []):
        locs = [x.get("name") or ", ".join(y for y in [x.get("city"), x.get("country")] if y)
                for x in j.get("locations") or []]
        loc = "; ".join(l for l in locs if l) or j.get("location") or ", ".join(
            x for x in [j.get("city"), j.get("country")] if x)
        out.append(_job(j.get("title"), loc, j.get("careers_url") or f"https://{slug}.recruitee.com/o/{j.get('slug')}",
                        (j.get("published_at") or "").replace(" UTC", "+00:00").replace(" ", "T"),
                        j.get("remote"), j.get("department"), [j.get("country")] if j.get("country") else []))
    return out


def fetch_personio(slug):
    last_err = None
    for tld in ("de", "com"):
        try:
            root = ET.fromstring(get(f"https://{slug}.jobs.personio.{tld}/xml?language=en"))
        except NET_ERRORS as e:
            last_err = e
            continue
        out = []
        for p in root.iter("position"):
            offices = [p.findtext("office") or ""] + [o.text or "" for o in p.findall("./additionalOffices/office")]
            pid = p.findtext("id")
            out.append(_job(p.findtext("name"), "; ".join(o for o in offices if o),
                            f"https://{slug}.jobs.personio.{tld}/job/{pid}", p.findtext("createdAt"),
                            "remote" in " ".join(offices).lower(), p.findtext("department")))
        return out
    raise last_err or ValueError("no Personio board")


def fetch_bamboohr(slug):
    data = get_json(f"https://{slug}.bamboohr.com/careers/list")
    out = []
    for j in data.get("result", []):
        loc = j.get("location") or j.get("atsLocation") or {}
        text = ", ".join(x for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x)
        out.append(_job(j.get("jobOpeningName"), text, f"https://{slug}.bamboohr.com/careers/{j.get('id')}", None,
                        j.get("isRemote") or j.get("locationType") == "1", j.get("departmentLabel"),
                        [loc.get("country")] if loc.get("country") else []))
    return out


def fetch_breezy(slug):
    data = get_json(f"https://{slug}.breezy.hr/json")
    out = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("location") or {}
        country = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country")
        text = loc.get("name") or ", ".join(x for x in [loc.get("city"), country] if x)
        out.append(_job(j.get("name"), text, j.get("url"), j.get("published_date"), loc.get("is_remote"),
                        (j.get("department") or ""), [country] if country else []))
    return out


ADAPTERS = {
    "ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever, "lever_eu": fetch_lever_eu,
    "workable": fetch_workable, "smartrecruiters": fetch_smartrecruiters, "recruitee": fetch_recruitee,
    "personio": fetch_personio, "bamboohr": fetch_bamboohr, "breezy": fetch_breezy,
}
BOARD_URL = {
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "lever_eu": "https://jobs.eu.lever.co/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com",
    "personio": "https://{slug}.jobs.personio.de",
    "bamboohr": "https://{slug}.bamboohr.com/careers",
    "breezy": "https://{slug}.breezy.hr",
}

# ---------------------------------------------------------------- finding boards in text

PATTERNS = [
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)"),
    ("greenhouse", r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?!embed)([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever_eu", r"jobs\.eu\.lever\.co/([A-Za-z0-9._-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9._-]+)"),
    ("workable", r"apply\.workable\.com/(?!j/|api/)([A-Za-z0-9_-]+)"),
    ("workable", r"\b(?!www\.|apply\.)([a-z0-9-]+)\.workable\.com"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"\b(?!www\.)([a-z0-9-]+)\.recruitee\.com"),
    ("personio", r"\b([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("bamboohr", r"\b(?!www\.)([a-z0-9-]+)\.bamboohr\.com"),
    ("breezy", r"\b(?!www\.|app\.|api\.)([a-z0-9-]+)\.breezy\.hr"),
]
PATTERNS = [(a, re.compile(p, re.I)) for a, p in PATTERNS]
SLUG_STOPWORDS = {"embed", "jobs", "api", "www", "apply", "j", "search", "careers", "app", "static", "assets", "cdn"}
ATS_DOMAINS = ("ashbyhq.com", "greenhouse.io", "lever.co", "workable.com", "smartrecruiters.com",
               "recruitee.com", "personio.de", "personio.com", "bamboohr.com", "breezy.hr")


def find_boards(text):
    """All (ats, slug) pairs mentioned in text/html, de-duplicated, in order."""
    seen, out = set(), []
    for ats, pat in PATTERNS:
        for m in pat.finditer(text):
            slug = m.group(1).rstrip(".")
            key = (ats.replace("_eu", ""), slug.lower())
            if key not in seen and slug.lower() not in SLUG_STOPWORDS:
                seen.add(key)
                out.append((ats, slug))
    return out


def job_count(ats, slug):
    """Open jobs on the board, or None if it doesn't exist / can't be read."""
    try:
        return len(ADAPTERS[ats](slug))
    except Exception:  # noqa: BLE001 - any failure means "no usable board"
        return None
