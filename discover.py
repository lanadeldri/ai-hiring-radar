#!/usr/bin/env python3
"""
Discover AI startups that are hiring, and find their ATS job board automatically.

Sources
  1. HN "Ask HN: Who is hiring?" (latest monthly thread, via the Algolia API)
     - parses each top-level comment: company name, website, ATS links
     - keeps only AI-related posts
     - no ATS link in the post? scans the company's website / careers page for one
  2. Funding news RSS (TechCrunch venture feed by default)
     - parses "X raises $40M Series B ..." headlines, keeps AI-related ones
     - guesses the ATS slug from the company name (flagged for review: slugs can collide)

Output
  data/candidates.csv   review queue  (status: new / approved / rejected — edit by hand)
  data/companies.csv    rows are added automatically for:
                          - high-confidence finds (ATS link verified) when run with --auto-add
                          - any candidate you set to status=approved

Usage
  python discover.py --auto-add
  python discover.py --sources hn            # only HN
  python discover.py --max-site-checks 50    # cap website scans
"""
import argparse
import csv
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
COMPANIES = ROOT / "data" / "companies.csv"
CANDIDATES = ROOT / "data" / "candidates.csv"

FUNDING_FEEDS = [
    "https://techcrunch.com/category/venture/feed/",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; ai-hiring-radar/0.2; personal project)"}
CAND_FIELDS = ["status", "name", "website", "ats", "slug", "confidence", "source", "source_url",
               "signal", "found_at", "notes"]
COMPANY_FIELDS = ["name", "website", "ats", "slug", "stage", "last_raise", "last_raise_date",
                  "sector", "hq", "notes"]

AI_RE = re.compile(
    r"\b(AI|A\.I\.|LLMs?|GenAI|generative|machine learning|ML|deep learning|foundation models?|"
    r"agents?|agentic|inference|GPUs?|RAG|NLP|computer vision|neural|transformers?|"
    r"artificial intelligence|copilot|voice AI|AI-native)\b"
)

# ATS link patterns → (ats, slug). Order matters: EU hosts before generic ones.
ATS_PATTERNS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?!embed)([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever_eu", re.compile(r"jobs\.eu\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
]
SKIP_DOMAINS = ("news.ycombinator.com", "ycombinator.com", "linkedin.com", "twitter.com", "x.com",
                "github.com", "ashbyhq.com", "greenhouse.io", "lever.co", "workatastartup.com",
                "wellfound.com", "angel.co", "notion.site", "docs.google.com", "forms.gle",
                "calendly.com", "youtube.com", "medium.com")


# ---------------------------------------------------------------- http helpers

def get(url, timeout=20):
    with urlopen(Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def get_json(url, timeout=20):
    return json.loads(get(url, timeout))


def board_api(ats, slug):
    return {
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
        "lever_eu": f"https://api.eu.lever.co/v0/postings/{slug}?mode=json&limit=1",
    }[ats]


def board_job_count(ats, slug):
    """Return number of open jobs on the board, or None if the board doesn't exist."""
    try:
        data = get_json(board_api(ats, slug))
    except (HTTPError, URLError, ValueError, TimeoutError):
        return None
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    return len(jobs)


# ---------------------------------------------------------------- parsing helpers

def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace(" ai", "").replace(".ai", ""))


def find_ats(text):
    """All (ats, slug) pairs mentioned in text/html, de-duplicated, in order."""
    seen, out = set(), []
    for ats, pat in ATS_PATTERNS:
        for m in pat.finditer(text):
            slug = m.group(1).rstrip(".")
            key = (ats.replace("_eu", ""), slug.lower())
            if key not in seen and slug.lower() not in ("embed", "jobs", "api"):
                seen.add(key)
                out.append((ats, slug))
    return out


def find_website(raw_html):
    for href in re.findall(r'href="([^"]+)"', raw_html):
        href = html.unescape(href)
        host = urlparse(href).netloc.lower()
        if host and not any(host.endswith(d) for d in SKIP_DOMAINS):
            return f"{urlparse(href).scheme or 'https'}://{host}"
    m = re.search(r"\b((?:https?://)?(?:www\.)?[a-z0-9-]+\.(?:ai|com|io|dev|co|app|so|xyz|tech))\b", strip_html(raw_html), re.I)
    if m:
        u = m.group(1)
        host = urlparse(u if "://" in u else "https://" + u).netloc.lower()
        if not any(host.endswith(d) for d in SKIP_DOMAINS):
            return "https://" + host
    return ""


def strip_html(s):
    s = re.sub(r"<p>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def company_from_hn(text):
    """HN convention: 'Company | Role | Location | ...' on the first line."""
    first = text.strip().split("\n", 1)[0]
    name = re.split(r"\s+[|–—]\s+|\s+-\s+|\s*\|\s*", first, maxsplit=1)[0]
    name = re.sub(r"\s*\(.*?\)\s*", " ", name)                 # drop "(YC W24)" etc.
    name = re.sub(r"https?://\S+", "", name).strip(" :,.")
    return name if 1 < len(name) <= 40 else ""


def is_ai_post(text):
    """AI keyword in the headline line, or several mentions in the body (casual 'we use AI' doesn't count)."""
    first = text.strip().split("\n", 1)[0]
    return bool(AI_RE.search(first)) or len(AI_RE.findall(text)) >= 3


def slug_guesses(name):
    base = re.sub(r"\(.*?\)", "", name).strip()
    words = re.findall(r"[a-z0-9]+", base.lower())
    if not words:
        return []
    joined, dashed = "".join(words), "-".join(words)
    guesses = [joined, dashed]
    if words[-1] != "ai":
        guesses += [joined + "ai", dashed + "-ai"]
    else:
        guesses += ["".join(words[:-1]), "-".join(words[:-1])]
    return list(dict.fromkeys(g for g in guesses if g))


def guess_board(name):
    """Try common slugs on each ATS; returns (ats, slug, jobs) or None."""
    for slug in slug_guesses(name):
        for ats in ("ashby", "greenhouse", "lever"):
            n = board_job_count(ats, slug)
            if n:
                return ats, slug, n
            time.sleep(0.15)
    return None


def scan_site_for_ats(website):
    """Look for an ATS link on the homepage and common careers paths."""
    for path in ("", "/careers", "/jobs", "/company/careers", "/about/careers"):
        try:
            page = get(website.rstrip("/") + path, timeout=12)
        except (HTTPError, URLError, ValueError, TimeoutError, ConnectionError):
            continue
        found = find_ats(page)
        if found:
            return found[0]
    return None


# ---------------------------------------------------------------- sources

def discover_hn(max_site_checks, sleep):
    hits = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=20")["hits"]
    thread = next((h for h in hits if h["title"].lower().startswith("ask hn: who is hiring")), None)
    if not thread:
        print("  HN: no 'Who is hiring' thread found", file=sys.stderr)
        return []
    tid = thread["objectID"]
    print(f"  HN: {thread['title']}  (id {tid})")
    item = get_json(f"https://hn.algolia.com/api/v1/items/{tid}")
    out, site_checks = [], 0

    for c in item.get("children", []):
        raw = html.unescape(c.get("text") or "")   # HN escapes "/" as &#x2F; inside links
        text = strip_html(raw)
        if not raw or not is_ai_post(text):
            continue
        name = company_from_hn(text)
        if not name:
            continue
        website = find_website(raw)
        signal = text.strip().split("\n", 1)[0][:140]
        src = f"https://news.ycombinator.com/item?id={c['id']}"
        base = {"name": name, "website": website, "source": "hn", "source_url": src, "signal": signal}

        ats_links = find_ats(raw)
        if ats_links:
            ats, slug = ats_links[0]
            out.append({**base, "ats": ats, "slug": slug, "confidence": "high", "notes": "ATS link in HN post"})
            continue
        if website and site_checks < max_site_checks:
            site_checks += 1
            found = scan_site_for_ats(website)
            time.sleep(sleep)
            if found:
                out.append({**base, "ats": found[0], "slug": found[1], "confidence": "high", "notes": "ATS link on company site"})
                continue
        out.append({**base, "ats": "", "slug": "", "confidence": "review", "notes": "no ATS found (may use Workable, Rippling, own site...)"})
    return out


FUNDING_RE = re.compile(
    r"^(?P<name>[A-Z][\w.&'’ -]{1,40}?)(?:,.*?,)?\s+(?:raises|lands|secures|closes|nabs|snags|bags|gets|pulls in|picks up)\s+"
    r"(?P<amount>[$€£][\d.,]+\s?(?:[MBK]|million|billion)?)"
    r"(?:.*?(?P<round>Series [A-H]\+?|seed|pre-seed|Seed))?",
    re.I,
)


def discover_funding(sleep):
    out = []
    for feed in FUNDING_FEEDS:
        try:
            root = ET.fromstring(get(feed))
        except Exception as e:  # noqa: BLE001
            print(f"  feed failed {feed}: {e}", file=sys.stderr)
            continue
        for item in root.iter("item"):
            title = html.unescape(item.findtext("title") or "").strip()
            desc = strip_html(item.findtext("description") or "")
            link = item.findtext("link") or ""
            m = FUNDING_RE.search(title)
            if not m or not AI_RE.search(title + " " + desc):
                continue
            name = m.group("name").strip()
            signal = " ".join(x for x in [m.group("round"), m.group("amount")] if x)
            board = guess_board(name)
            time.sleep(sleep)
            row = {"name": name, "website": "", "source": "funding", "source_url": link,
                   "signal": signal, "notes": title[:140]}
            if board:
                ats, slug, n = board
                row.update(ats=ats, slug=slug, confidence="review", notes=f"slug guessed ({n} jobs) — check it's the right company")
            else:
                row.update(ats="", slug="", confidence="review")
            out.append(row)
    return out


# ---------------------------------------------------------------- merge + write

def read_csv(path, fields):
    if not path.exists():
        return []
    return [{f: r.get(f, "") for f in fields} for r in csv.DictReader(path.open(encoding="utf-8"))]


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="hn,funding")
    ap.add_argument("--auto-add", action="store_true", help="add verified high-confidence finds to companies.csv")
    ap.add_argument("--max-site-checks", type=int, default=80)
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()
    sources = set(args.sources.split(","))
    today = datetime.now(timezone.utc).date().isoformat()

    companies = read_csv(COMPANIES, COMPANY_FIELDS)
    candidates = read_csv(CANDIDATES, CAND_FIELDS)
    known_slugs = {(c["ats"].replace("_eu", ""), c["slug"].lower()) for c in companies if c["slug"]}
    known_names = {norm(c["name"]) for c in companies} | {norm(c["name"]) for c in candidates}

    found = []
    if "hn" in sources:
        try:
            found += discover_hn(args.max_site_checks, args.sleep)
        except Exception as e:  # noqa: BLE001
            print(f"  HN failed: {e}", file=sys.stderr)
    if "funding" in sources:
        found += discover_funding(args.sleep)

    new_cands, added = 0, []
    for f in found:
        key = (f["ats"].replace("_eu", ""), f["slug"].lower())
        if norm(f["name"]) in known_names or (f["slug"] and key in known_slugs):
            continue
        # verify the board actually exists and is hiring
        if f["slug"]:
            n = board_job_count(f["ats"], f["slug"])
            time.sleep(args.sleep)
            if not n:
                f.update(confidence="review", notes=(f["notes"] + "; board empty or missing").strip("; "))
        f.update(status="new", found_at=today)
        if args.auto_add and f["confidence"] == "high":
            f["status"] = "approved"
        candidates.append({k: f.get(k, "") for k in CAND_FIELDS})
        known_names.add(norm(f["name"]))
        if f["slug"]:
            known_slugs.add(key)
        new_cands += 1

    # promote approved candidates into companies.csv
    company_names = {norm(c["name"]) for c in companies}
    for c in candidates:
        if c["status"] == "approved" and c["slug"] and norm(c["name"]) not in company_names:
            is_funding = c["source"] == "funding"
            companies.append({
                "name": c["name"], "website": c["website"], "ats": c["ats"], "slug": c["slug"],
                "stage": c["signal"].split(" $")[0] if is_funding else "",
                "last_raise": c["signal"] if is_funding else "",
                "last_raise_date": c["found_at"] if is_funding else "",
                "sector": "", "hq": "", "notes": f"via {c['source']}",
            })
            company_names.add(norm(c["name"]))
            c["status"] = "added"
            added.append(c["name"])

    write_csv(CANDIDATES, CAND_FIELDS, candidates)
    write_csv(COMPANIES, COMPANY_FIELDS, companies)
    review = sum(1 for c in candidates if c["status"] == "new")
    print(f"\n{len(found)} AI hiring signals found · {new_cands} new candidates · {len(added)} added to companies.csv · {review} waiting for review")
    for n in added:
        print(f"  + {n}")


if __name__ == "__main__":
    main()
