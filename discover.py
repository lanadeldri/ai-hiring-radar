#!/usr/bin/env python3
"""
Discover AI startups that are hiring, and find their job board automatically.

Sources
  1. HN "Ask HN: Who is hiring?"  last N monthly threads (default 12), via the Algolia API.
     Keeps AI companies (scored filter), takes the job-board link from the post.
  2. YC directory (yc-oss API)    companies tagged AI / Artificial Intelligence / Generative AI /
     Machine Learning that YC marks as hiring. Comes with sector, HQ and batch.
  3. Funding news RSS             TechCrunch, Crunchbase News, Google News searches.
     Board slug is guessed from the name, so these always wait for review.

For leads without a board link, the company website and its /careers pages are scanned for one.
Scans run in parallel and are cached for a week (checked_at), so daily runs stay fast.

Supported job boards: see ats.py.

Output
  data/candidates.csv  every lead and what happened to it. status:
                         added     in companies.csv
                         approved  you approved it; added on the next run
                         new       needs your review (guessed slug, or board empty)
                         no_ats    no supported job board found (re-checked weekly)
                         rejected  you rejected it
                         filtered  not an AI company
  data/companies.csv   verified finds are added with --auto-add; approved candidates are always added;
                       earlier HN auto-adds that fail the AI filter are removed.

Usage
  python discover.py --auto-add
  python discover.py --sources hn,yc --hn-months 6
"""
import argparse
import csv
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from ats import ATS_DOMAINS, find_boards, get, get_json, job_count

ROOT = Path(__file__).parent
COMPANIES = ROOT / "data" / "companies.csv"
CANDIDATES = ROOT / "data" / "candidates.csv"
RESCAN_DAYS = 7


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
YC_TAGS = ["ai", "artificial-intelligence", "generative-ai", "machine-learning"]

CAND_FIELDS = ["status", "name", "website", "ats", "slug", "confidence", "source", "source_url",
               "signal", "sector", "hq", "stage", "found_at", "checked_at", "notes"]
COMPANY_FIELDS = ["name", "website", "ats", "slug", "stage", "last_raise", "last_raise_date",
                  "sector", "hq", "notes"]
TERMINAL = {"added", "rejected", "filtered", "approved"}

# --- AI filter (for HN posts) --------------------------------------------
# Plain "AI" in a post body doesn't count: almost every company mentions it now.
HEAD_CS = re.compile(r"\b(AI|ML|LLMs?|GenAI|NLP|GPUs?|MLOps)\b")
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


SKIP_DOMAINS = ("news.ycombinator.com", "ycombinator.com", "linkedin.com", "twitter.com", "x.com",
                "github.com", "workatastartup.com", "wellfound.com", "angel.co", "notion.site",
                "docs.google.com", "forms.gle", "calendly.com", "youtube.com", "medium.com") + ATS_DOMAINS


# ---------------------------------------------------------------- parsing helpers

def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace(" ai", "").replace(".ai", ""))


def strip_html(s):
    s = re.sub(r"<p>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


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


def company_from_hn(text):
    """HN convention: 'Company | Role | Location | ...' on the first line."""
    first = text.strip().split("\n", 1)[0]
    name = re.split(r"\s+[|–—]\s+|\s+-\s+|\s*\|\s*", first, maxsplit=1)[0]
    name = re.sub(r"\s*\(.*?\)\s*", " ", name)                 # drop "(YC W24)" etc.
    name = re.sub(r"https?://\S+", "", name).strip(" :,.")
    return name if 1 < len(name) <= 40 else ""


def slug_guesses(name):
    words = re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", "", name).lower())
    if not words:
        return []
    joined, dashed = "".join(words), "-".join(words)
    guesses = [joined, dashed]
    guesses += [joined + "ai", dashed + "-ai"] if words[-1] != "ai" else ["".join(words[:-1]), "-".join(words[:-1])]
    return list(dict.fromkeys(g for g in guesses if g))


def guess_board(name):
    for slug in slug_guesses(name):
        for ats in ("ashby", "greenhouse", "lever"):
            n = job_count(ats, slug)
            if n:
                return ats, slug, n
    return None


def scan_site(website):
    """Look for a job-board link on the homepage and common careers paths."""
    base = website.rstrip("/")
    for path in ("", "/careers", "/jobs", "/company/careers", "/about/careers"):
        try:
            page = get(base + path, timeout=8)
        except Exception:  # noqa: BLE001
            continue
        found = find_boards(page)
        if found:
            return found
    return []


# ---------------------------------------------------------------- sources → leads
# A lead: {name, website, source, source_url, signal, sector, hq, stage, boards: [(ats, slug)], recent, guess}

def leads_hn(months):
    hits = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=100")["hits"]
    threads = [h for h in hits if h["title"].lower().startswith("ask hn: who is hiring")][:months]
    leads, ai_names, not_ai = {}, set(), set()
    for i, th in enumerate(threads):
        try:
            item = get_json(f"https://hn.algolia.com/api/v1/items/{th['objectID']}", timeout=90)
        except Exception as e:  # noqa: BLE001
            print(f"  HN: skipped {th['title']}: {e}", file=sys.stderr)
            continue
        posts = item.get("children", [])
        n_ai = 0
        for c in posts:
            raw = html.unescape(c.get("text") or "")   # HN escapes "/" as &#x2F; inside links
            if not raw:
                continue
            text = strip_html(raw)
            name = company_from_hn(text)
            if not name:
                continue
            website = find_website(raw)
            key = norm(name)
            if not is_ai_company(text, name, website):
                not_ai.add(key)
                continue
            ai_names.add(key)
            n_ai += 1
            if key in leads:           # keep the newest post (threads are newest first)
                continue
            leads[key] = {"name": name, "website": website, "source": "hn",
                          "source_url": f"https://news.ycombinator.com/item?id={c['id']}",
                          "signal": text.strip().split("\n", 1)[0][:140], "boards": find_boards(raw),
                          "recent": i == 0, "sector": "", "hq": "", "stage": ""}
        print(f"  HN: {th['title']}: {len(posts)} posts, {n_ai} AI")
    return list(leads.values()), not_ai - ai_names


def leads_yc():
    seen, leads = set(), []
    for tag in YC_TAGS:
        try:
            companies = get_json(f"https://yc-oss.github.io/api/tags/{tag}.json", timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"  YC: tag {tag} failed: {e}", file=sys.stderr)
            continue
        for c in companies:
            if not c.get("isHiring") or c.get("status") not in ("Active", "Public") or not c.get("website"):
                continue
            key = norm(c.get("name", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            sub = (c.get("subindustry") or c.get("industry") or "").split("->")[-1].strip()
            leads.append({"name": c["name"].strip(), "website": c["website"].strip(), "source": "yc",
                          "source_url": c.get("url", ""), "signal": (c.get("one_liner") or "")[:140],
                          "sector": sub, "hq": (c.get("all_locations") or "").split(";")[0].strip(),
                          "stage": f"YC {c['batch']}" if c.get("batch") else "", "boards": [], "recent": True})
    print(f"  YC: {len(leads)} AI companies marked hiring")
    return leads


FUNDING_RE = re.compile(
    r"^(?P<name>[A-Z][\w.&'’ -]{1,60}?)(?:,.*?,)?\s+(?:raises|lands|secures|closes|nabs|snags|bags|gets|pulls in|picks up)\s+"
    r"(?P<amount>[$€£][\d.,]+\s?(?:[MBK]|million|billion)?)"
    r"(?:.*?(?P<round>Series [A-H]\+?|seed|pre-seed))?",
    re.I,
)
DESCRIPTOR_RE = re.compile(r"^.*\b(?:startup|start-up|company|firm|maker|platform|developer|unicorn|provider)\s+", re.I)
PREFIX_RE = re.compile(r"^(?:exclusive|scoop|breaking|report)\s*[:|-]\s*", re.I)


def clean_company(name):
    name = DESCRIPTOR_RE.sub("", PREFIX_RE.sub("", name)).strip(" '\"’,")   # "Legal AI startup Harvey" -> "Harvey"
    return name if 0 < len(name.split()) <= 4 else ""


def leads_funding(max_items):
    leads, seen = [], set()
    for feed in FUNDING_FEEDS:
        try:
            root = ET.fromstring(get(feed, timeout=20))
        except Exception as e:  # noqa: BLE001
            print(f"  funding: feed failed {feed[:60]}: {e}", file=sys.stderr)
            continue
        n = 0
        for item in root.iter("item"):
            if len(leads) >= max_items:
                break
            title = html.unescape(item.findtext("title") or "").strip()
            title = PREFIX_RE.sub("", re.sub(r"\s+-\s+[^-]{2,40}$", "", title))   # Google News "- Publisher"
            m = FUNDING_RE.search(title)
            blob = title + " " + strip_html(item.findtext("description") or "")
            if not m or not (re.search(r"\bAI\b", blob) or STRONG.search(blob)):
                continue
            name = clean_company(m.group("name"))
            if not name or norm(name) in seen:
                continue
            seen.add(norm(name))
            n += 1
            leads.append({"name": name, "website": "", "source": "funding", "source_url": item.findtext("link") or "",
                          "signal": " ".join(x for x in [m.group("round"), m.group("amount")] if x),
                          "boards": [], "recent": True, "guess": True, "sector": "", "hq": "", "stage": "",
                          "headline": title[:140]})
        print(f"  funding: {n} AI rounds from {urlparse(feed).netloc}")
    return leads


# ---------------------------------------------------------------- resolve a lead to a job board

def resolve(lead):
    """Runs in a worker thread. Returns the lead with ats/slug/status/notes filled in."""
    boards, how = lead["boards"], "job-board link in HN post"
    if not boards and lead.get("website"):
        boards, how = scan_site(lead["website"]), "job-board link on company site"
    for ats, slug in boards:
        n = job_count(ats, slug)
        if n:
            return {**lead, "ats": ats, "slug": slug, "status": "high", "notes": f"{how} ({n} jobs)"}
    if lead.get("guess"):
        g = guess_board(lead["name"])
        if g:
            return {**lead, "ats": g[0], "slug": g[1], "status": "new",
                    "notes": f"slug guessed ({g[2]} jobs) — check it's the right company. {lead.get('headline', '')}"}
        return {**lead, "ats": "", "slug": "", "status": "no_ats", "notes": lead.get("headline", "")}
    if boards:
        ats, slug = boards[0]
        if lead["recent"]:
            return {**lead, "ats": ats, "slug": slug, "status": "new", "notes": f"{how}, but board is empty"}
        return {**lead, "ats": ats, "slug": slug, "status": "no_ats", "notes": "board no longer has jobs"}
    return {**lead, "ats": "", "slug": "", "status": "no_ats",
            "notes": "no supported job board found (may use Rippling, Workday, YC Work at a Startup, own site...)"}


# ---------------------------------------------------------------- merge + write

def read_csv(path, fields):
    if not path.exists():
        return []
    return [{f: (r.get(f) or "") for f in fields} for r in csv.DictReader(path.open(encoding="utf-8"))]


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def recently_checked(row, today):
    try:
        return (today - date.fromisoformat(row["checked_at"])).days < RESCAN_DAYS
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="hn,yc,funding")
    ap.add_argument("--auto-add", action="store_true", help="add verified finds to companies.csv")
    ap.add_argument("--hn-months", type=int, default=12)
    ap.add_argument("--max-checks", type=int, default=3000, help="cap on leads resolved per run")
    ap.add_argument("--max-funding", type=int, default=40)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()
    sources = set(args.sources.split(","))
    started = time.time()
    today = datetime.now(timezone.utc).date()

    companies = read_csv(COMPANIES, COMPANY_FIELDS)
    candidates = read_csv(CANDIDATES, CAND_FIELDS)
    for c in candidates:                 # older files: unreviewed rows without a board are now "no_ats"
        if c["status"] == "new" and not c["slug"]:
            c["status"] = "no_ats"

    leads, not_ai = [], set()
    if "hn" in sources:
        try:
            hn, not_ai = leads_hn(args.hn_months)
            leads += hn
        except Exception as e:  # noqa: BLE001
            print(f"  HN failed: {e}", file=sys.stderr)
    if "yc" in sources:
        leads += leads_yc()
    if "funding" in sources:
        leads += leads_funding(args.max_funding)

    # 1) drop earlier HN auto-adds that the AI filter rejects (hand-added rows are never touched)
    removed = [c["name"] for c in companies if c["notes"] == "via hn" and norm(c["name"]) in not_ai]
    companies = [c for c in companies if not (c["notes"] == "via hn" and norm(c["name"]) in not_ai)]
    for c in candidates:
        if c["source"] == "hn" and c["status"] in ("new", "added", "no_ats") and norm(c["name"]) in not_ai:
            c["status"], c["notes"] = "filtered", "not an AI company (stricter filter)"

    # 2) pick leads that need resolving: not already listed, not decided, not checked this week
    by_name = {norm(c["name"]): c for c in candidates}
    listed = {norm(c["name"]) for c in companies}
    listed_slugs = {(c["ats"].replace("_eu", ""), c["slug"].lower()) for c in companies if c["slug"]}
    todo, seen = [], set()
    for lead in leads:
        key = norm(lead["name"])
        if not key or key in seen or key in listed:
            continue
        seen.add(key)
        if any((a.replace("_eu", ""), s.lower()) in listed_slugs for a, s in lead["boards"]):
            continue
        old = by_name.get(key)
        if old and (old["status"] in TERMINAL or recently_checked(old, today)):
            continue
        todo.append(lead)
    todo = todo[:args.max_checks]
    print(f"\n  resolving {len(todo)} leads ({len(leads)} found, rest already known or checked this week)...")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        resolved = list(pool.map(resolve, todo))

    # 3) merge results into candidates.csv
    counts = {"high": 0, "new": 0, "no_ats": 0}
    for r in resolved:
        counts[r["status"]] += 1
        key = (r["ats"].replace("_eu", ""), r["slug"].lower())
        if r["status"] == "high" and key in listed_slugs:       # same board under another name
            continue
        status = ("approved" if args.auto_add else "new") if r["status"] == "high" else r["status"]
        row = {"status": status, "name": r["name"], "website": r["website"], "ats": r["ats"], "slug": r["slug"],
               "confidence": "high" if r["status"] == "high" else "review", "source": r["source"],
               "source_url": r["source_url"], "signal": r["signal"], "sector": r.get("sector", ""),
               "hq": r.get("hq", ""), "stage": r.get("stage", ""), "found_at": today.isoformat(),
               "checked_at": today.isoformat(), "notes": r["notes"]}
        old = by_name.get(norm(r["name"]))
        if old:
            row["found_at"] = old["found_at"] or row["found_at"]
            old.update(row)
        else:
            candidates.append(row)
            by_name[norm(r["name"])] = row
        if r["status"] == "high":
            listed_slugs.add(key)

    # 4) promote approved candidates into companies.csv
    added = []
    for c in candidates:
        if c["status"] == "approved" and c["slug"] and norm(c["name"]) not in listed:
            funding = c["source"] == "funding"
            companies.append({
                "name": c["name"], "website": c["website"], "ats": c["ats"], "slug": c["slug"],
                "stage": c["stage"] or (c["signal"].split(" $")[0] if funding and not c["signal"].startswith("$") else ""),
                "last_raise": c["signal"] if funding else "",
                "last_raise_date": c["found_at"] if funding else "",
                "sector": c["sector"], "hq": c["hq"], "notes": f"via {c['source']}",
            })
            listed.add(norm(c["name"]))
            c["status"] = "added"
            added.append(c["name"])

    write_csv(CANDIDATES, CAND_FIELDS, candidates)
    write_csv(COMPANIES, COMPANY_FIELDS, companies)
    review = sum(1 for c in candidates if c["status"] == "new")
    print(f"\n{len(todo)} leads checked: {counts['high']} verified boards, {counts['new']} need review, "
          f"{counts['no_ats']} no supported board")
    print(f"{len(added)} added · {len(removed)} removed (not AI) · {review} waiting for review · "
          f"{len(companies)} companies total · {time.time() - started:.0f}s")
    for n in removed:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
