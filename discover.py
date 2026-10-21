#!/usr/bin/env python3
"""
Discover AI startups that are hiring, and find their ATS job board automatically.

Sources
  1. HN "Ask HN: Who is hiring?" (latest monthly thread, via the Algolia API)
     - parses each top-level comment: company name, website, ATS links
     - keeps only AI companies (scored: headline, company name/domain, specific AI terms in the body)
     - no ATS link in the post? scans the company's website / careers page for one
  2. Funding news RSS (TechCrunch, Crunchbase News, Google News searches)
     - parses "X raises $40M Series B ..." headlines, keeps AI-related ones
     - guesses the ATS slug from the company name (flagged for review: slugs can collide)

Supported ATS: Ashby, Greenhouse, Lever (+EU), Workable, SmartRecruiters

Output
  data/candidates.csv   review queue  (status: new / approved / rejected / added / filtered)
  data/companies.csv    rows are added automatically for:
                          - high-confidence finds (ATS link verified) when run with --auto-add
                          - any candidate you set to status=approved
                        rows previously auto-added from HN that no longer pass the AI filter are removed

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
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
COMPANIES = ROOT / "data" / "companies.csv"
CANDIDATES = ROOT / "data" / "candidates.csv"


def google_news(q):
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"


FUNDING_FEEDS = [
    "https://techcrunch.com/category/venture/feed/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://news.crunchbase.com/feed/",
    google_news('AI startup raises "Series A" when:14d'),
    google_news('AI startup raises "Series B" when:14d'),
    google_news('AI startup raises seed round when:14d'),
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; ai-hiring-radar/0.3; personal project)"}
CAND_FIELDS = ["status", "name", "website", "ats", "slug", "confidence", "source", "source_url",
               "signal", "found_at", "notes"]
COMPANY_FIELDS = ["name", "website", "ats", "slug", "stage", "last_raise", "last_raise_date",
                  "sector", "hq", "notes"]

# --- AI filter -------------------------------------------------------------
# Plain "AI" in a post body doesn't count: almost every company mentions it now.
HEAD_CS = re.compile(r"\b(AI|ML|LLMs?|GenAI|NLP|GPUs?|MLOps)\b")                       # case-sensitive
HEAD_CI = re.compile(r"\b(machine learning|deep learning|foundation models?|inference|computer vision|"
                     r"AI[- ]native|agentic|applied AI|research (?:engineer|scientist)|robotics)\b", re.I)
STRONG = re.compile(r"\b(LLMs?|GenAI|generative AI|machine learning|deep learning|foundation models?|"
                    r"inference|GPUs?|computer vision|NLP|neural (?:networks?|nets?)|reinforcement learning|"
                    r"RLHF|AI research|AI[- ]native|AI agents?|agentic|multimodal|ML (?:engineers?|infra\w*|platform|research\w*)|"
                    r"research scientists?|model training|fine[- ]?tun\w+|embeddings?|RAG|evals?|"
                    r"vision[- ]language|speech recognition|TTS|diffusion models?)\b", re.I)


def ai_score(text, name="", website=""):
    first = text.strip().split("\n", 1)[0]
    score = 0
    if re.search(r"\bAI\b|\.ai\b", name) or urlparse(website).netloc.endswith(".ai"):
        score += 2
    if HEAD_CS.search(first) or HEAD_CI.search(first):
        score += 2
    score += len({m.group(0).lower() for m in STRONG.finditer(text)})
    return score


def is_ai_company(text, name="", website=""):
    return ai_score(text, name, website) >= 3


# --- ATS link patterns → (ats, slug). Order matters: EU hosts before generic ones.
ATS_PATTERNS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?!embed)([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever_eu", re.compile(r"jobs\.eu\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?!j/|api/)([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"\b(?!www\.|apply\.)([a-z0-9-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
]
SLUG_STOPWORDS = {"embed", "jobs", "api", "www", "apply", "j", "search", "careers"}
SKIP_DOMAINS = ("news.ycombinator.com", "ycombinator.com", "linkedin.com", "twitter.com", "x.com",
                "github.com", "ashbyhq.com", "greenhouse.io", "lever.co", "workable.com",
                "smartrecruiters.com", "workatastartup.com", "wellfound.com", "angel.co",
                "notion.site", "docs.google.com", "forms.gle", "calendly.com", "youtube.com", "medium.com")


# ---------------------------------------------------------------- http helpers

def get(url, timeout=12):
    with urlopen(Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def get_json(url, timeout=12):
    return json.loads(get(url, timeout))


def board_api(ats, slug):
    return {
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "lever_eu": f"https://api.eu.lever.co/v0/postings/{slug}?mode=json",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
    }[ats]


def board_job_count(ats, slug):
    """Return number of open jobs on the board, or None if the board doesn't exist."""
    try:
        data = get_json(board_api(ats, slug))
    except (HTTPError, URLError, ValueError, TimeoutError, ConnectionError):
        return None
    if isinstance(data, list):
        return len(data)
    if "totalFound" in data:
        return int(data["totalFound"])
    return len(data.get("jobs", []))


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
            if key not in seen and slug.lower() not in SLUG_STOPWORDS:
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
    """Try common slugs on the three most common startup ATSes; returns (ats, slug, jobs) or None."""
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
            page = get(website.rstrip("/") + path, timeout=8)
        except (HTTPError, URLError, ValueError, TimeoutError, ConnectionError, OSError):
            continue
        found = find_ats(page)
        if found:
            return found[0]
    return None


# ---------------------------------------------------------------- sources

def discover_hn(max_site_checks, sleep):
    """Returns (found, not_ai_names). not_ai_names lets us drop earlier false positives."""
    hits = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=20")["hits"]
    thread = next((h for h in hits if h["title"].lower().startswith("ask hn: who is hiring")), None)
    if not thread:
        print("  HN: no 'Who is hiring' thread found", file=sys.stderr)
        return [], set()
    tid = thread["objectID"]
    item = get_json(f"https://hn.algolia.com/api/v1/items/{tid}", timeout=60)
    posts = item.get("children", [])
    print(f"  HN: {thread['title']}  (id {tid}, {len(posts)} posts)")
    out, not_ai, site_checks = [], set(), 0

    for c in posts:
        raw = html.unescape(c.get("text") or "")   # HN escapes "/" as &#x2F; inside links
        if not raw:
            continue
        text = strip_html(raw)
        name = company_from_hn(text)
        if not name:
            continue
        website = find_website(raw)
        if not is_ai_company(text, name, website):
            not_ai.add(norm(name))
            continue
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
        out.append({**base, "ats": "", "slug": "", "confidence": "review",
                    "notes": "no supported ATS found (may use Rippling, Workday, own site...)"})
    ai = len(out)
    print(f"  HN: {ai} AI companies, {len(not_ai)} filtered out, {site_checks} websites scanned")
    return out, not_ai


FUNDING_RE = re.compile(
    r"^(?P<name>[A-Z][\w.&'’ -]{1,60}?)(?:,.*?,)?\s+(?:raises|lands|secures|closes|nabs|snags|bags|gets|pulls in|picks up)\s+"
    r"(?P<amount>[$€£][\d.,]+\s?(?:[MBK]|million|billion)?)"
    r"(?:.*?(?P<round>Series [A-H]\+?|seed|pre-seed))?",
    re.I,
)
DESCRIPTOR_RE = re.compile(r"^.*\b(?:startup|start-up|company|firm|maker|platform|developer|unicorn|provider)\s+", re.I)


def clean_company(name):
    name = re.sub(r"^(?:exclusive|scoop|breaking|report)\s*[:|-]\s*", "", name, flags=re.I)
    name = DESCRIPTOR_RE.sub("", name)          # "Legal AI startup Harvey" -> "Harvey"
    name = name.strip(" '\"’,")
    return name if 0 < len(name.split()) <= 4 else ""


def discover_funding(sleep, max_items):
    out, seen = [], set()
    for feed in FUNDING_FEEDS:
        try:
            root = ET.fromstring(get(feed, timeout=20))
        except Exception as e:  # noqa: BLE001
            print(f"  feed failed {feed[:60]}: {e}", file=sys.stderr)
            continue
        n_feed = 0
        for item in root.iter("item"):
            if len(out) >= max_items:
                break
            title = html.unescape(item.findtext("title") or "").strip()
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)       # Google News: "... - Publisher"
            title = re.sub(r"^(?:exclusive|scoop|breaking|report)\s*[:|-]\s*", "", title, flags=re.I)
            desc = strip_html(item.findtext("description") or "")
            link = item.findtext("link") or ""
            m = FUNDING_RE.search(title)
            if not m:
                continue
            blob = title + " " + desc
            if not (re.search(r"\bAI\b", blob) or STRONG.search(blob)):
                continue
            name = clean_company(m.group("name"))
            if not name or norm(name) in seen:
                continue
            seen.add(norm(name))
            n_feed += 1
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
        print(f"  funding: {n_feed} AI rounds from {urlparse(feed).netloc}")
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
    ap.add_argument("--max-site-checks", type=int, default=150)
    ap.add_argument("--max-funding", type=int, default=40)
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()
    sources = set(args.sources.split(","))
    today = datetime.now(timezone.utc).date().isoformat()

    companies = read_csv(COMPANIES, COMPANY_FIELDS)
    candidates = read_csv(CANDIDATES, CAND_FIELDS)

    found, not_ai = [], set()
    if "hn" in sources:
        try:
            hn_found, not_ai = discover_hn(args.max_site_checks, args.sleep)
            found += hn_found
        except Exception as e:  # noqa: BLE001
            print(f"  HN failed: {e}", file=sys.stderr)
    if "funding" in sources:
        found += discover_funding(args.sleep, args.max_funding)

    # 1) drop earlier HN auto-adds that the current AI filter rejects (hand-added rows are never touched)
    removed = [c["name"] for c in companies if c["notes"] == "via hn" and norm(c["name"]) in not_ai]
    companies = [c for c in companies if not (c["notes"] == "via hn" and norm(c["name"]) in not_ai)]
    for c in candidates:
        if c["source"] == "hn" and c["status"] in ("new", "added") and norm(c["name"]) in not_ai:
            c["status"], c["notes"] = "filtered", "not an AI company (stricter filter)"

    # 2) merge new finds; upgrade existing review rows when we now found their ATS
    by_name = {norm(c["name"]): c for c in candidates}
    known_slugs = {(c["ats"].replace("_eu", ""), c["slug"].lower()) for c in companies if c["slug"]}
    company_names = {norm(c["name"]) for c in companies}
    new_cands = upgraded = 0
    for f in found:
        key = (f["ats"].replace("_eu", ""), f["slug"].lower())
        if norm(f["name"]) in company_names or (f["slug"] and key in known_slugs):
            continue
        if f["slug"]:  # verify the board exists and is hiring
            n = board_job_count(f["ats"], f["slug"])
            time.sleep(args.sleep)
            if not n:
                f.update(confidence="review", notes=(f["notes"] + "; board empty or missing").strip("; "))
        status = "approved" if args.auto_add and f["confidence"] == "high" else "new"
        row = {k: f.get(k, "") for k in CAND_FIELDS} | {"status": status, "found_at": today}

        existing = by_name.get(norm(f["name"]))
        if existing:
            if existing["status"] == "new" and not existing["slug"] and f["slug"]:
                existing.update({k: row[k] for k in ("ats", "slug", "confidence", "notes", "status")})
                upgraded += 1
            continue
        candidates.append(row)
        by_name[norm(f["name"])] = row
        if f["slug"]:
            known_slugs.add(key)
        new_cands += 1

    # 3) promote approved candidates into companies.csv
    added = []
    for c in candidates:
        if c["status"] == "approved" and c["slug"] and norm(c["name"]) not in company_names:
            is_funding = c["source"] == "funding"
            companies.append({
                "name": c["name"], "website": c["website"], "ats": c["ats"], "slug": c["slug"],
                "stage": c["signal"].split(" $")[0] if is_funding and not c["signal"].startswith("$") else "",
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
    print(f"\n{len(found)} AI hiring signals · {new_cands} new candidates · {upgraded} upgraded · "
          f"{len(added)} added · {len(removed)} removed (not AI) · {review} waiting for review")
    for n in added:
        print(f"  + {n}")
    for n in removed:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
