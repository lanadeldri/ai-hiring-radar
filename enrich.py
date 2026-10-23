#!/usr/bin/env python3
"""
Add funding stage and company size to every company in data/companies.csv.

Sources (all free and public):
  1. Funding news   one Google News search per company ("<name>" raises ...). The newest headline
                    that names the company and a round ("Series A", "seed", ...) or an amount wins.
  2. YC directory   team size and YC's own stage label (Early / Growth) for YC companies,
                    matched by website domain or name.
  3. Funding leads  rounds already parsed by discover.py from funding news (candidates.csv).

Output: data/enrich.json  {company name: {round, amount, date, headline, url, team_size, yc_stage, checked_at}}
fetch_jobs.py merges it into companies.json. News lookups are cached for 30 days.

Usage:
  python enrich.py
  python enrich.py --refresh-days 7 --workers 4
"""
import argparse
import csv
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from ats import get, get_json
from discover import FUNDING_RE, PREFIX_RE, YC_TAGS, clean_company, norm

ROOT = Path(__file__).parent
COMPANIES = ROOT / "data" / "companies.csv"
CANDIDATES = ROOT / "data" / "candidates.csv"
OUT = ROOT / "data" / "enrich.json"

# names that are ordinary words: require an AI/tech hint in the headline to avoid other companies
TECH_HINT = re.compile(r"\b(AI|A\.I\.|artificial intelligence|LLM|GenAI|machine learning|startup|software|platform|"
                       r"agents?|robotics|data|cloud|developer|API|automation|tech)\b", re.I)
ROUND_RE = re.compile(r"\b(pre-?seed|seed|series [a-h]\+?|growth round|series extension)\b", re.I)


def bucket(round_text):
    """'Series B' -> 'Series B'; 'pre-seed' -> 'Seed'; 'Series D' -> 'Series C+'."""
    r = (round_text or "").lower()
    if "seed" in r:
        return "Seed"
    m = re.search(r"series ([a-h])", r)
    if m:
        letter = m.group(1).upper()
        return f"Series {letter}" if letter in "AB" else "Series C+"
    if "growth" in r:
        return "Series C+"
    return ""


def norm_amount(text):
    """'$40 million' -> '$40M', '€1.2 billion' -> '€1.2B'."""
    m = re.match(r"([$€£])\s*([\d.,]+)\s*(million|billion|[MBK])?", (text or "").strip(), re.I)
    if not m:
        return ""
    unit = (m.group(3) or "").lower()
    unit = {"million": "M", "billion": "B", "m": "M", "b": "B", "k": "K"}.get(unit, "")
    return f"{m.group(1)}{m.group(2).rstrip('.,')}{unit}"


def domain(url):
    host = urlparse(url if "://" in (url or "") else "https://" + (url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def generic_name(name):
    words = re.findall(r"[A-Za-z]+", name)
    return len(words) == 1 and len(words[0]) <= 8 and not re.search(r"ai$", name.lower())


def search_news(name):
    """Newest funding headline for this company, or None."""
    q = f'"{name}" (raises OR raised OR funding OR "Series A" OR "Series B" OR seed)'
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
    root = ET.fromstring(get(url, timeout=20))
    best = None
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        title = PREFIX_RE.sub("", re.sub(r"\s+-\s+[^-]{2,40}$", "", title))
        m = FUNDING_RE.search(title)
        if not m or norm(clean_company(m.group("name"))) != norm(name):
            continue
        if generic_name(name) and not TECH_HINT.search(title):
            continue
        rnd = m.group("round") or (ROUND_RE.search(title).group(1) if ROUND_RE.search(title) else "")
        try:
            when = parsedate_to_datetime(item.findtext("pubDate")).date()
        except Exception:  # noqa: BLE001
            when = None
        hit = {"round": bucket(rnd), "round_text": rnd.strip().title() if rnd else "", "amount": norm_amount(m.group("amount")),
               "date": when.isoformat() if when else "", "headline": title[:160], "url": item.findtext("link") or ""}
        if best is None or hit["date"] > best["date"]:
            best = hit
    return best


def load_yc():
    by_domain, by_name = {}, {}
    for tag in YC_TAGS:
        try:
            for c in get_json(f"https://yc-oss.github.io/api/tags/{tag}.json", timeout=60):
                info = {"team_size": c.get("team_size"), "yc_stage": c.get("stage") or "", "yc_batch": c.get("batch") or ""}
                if c.get("website"):
                    by_domain[domain(c["website"])] = info
                by_name[norm(c.get("name", ""))] = info
        except Exception as e:  # noqa: BLE001
            print(f"  YC tag {tag} failed: {e}", file=sys.stderr)
    return by_domain, by_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-days", type=int, default=30, help="re-check funding news after this many days")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max", type=int, default=2000, help="cap on news lookups per run")
    args = ap.parse_args()
    started, today = time.time(), datetime.now(timezone.utc).date()

    companies = list(csv.DictReader(COMPANIES.open(encoding="utf-8")))
    cache = json.loads(OUT.read_text()) if OUT.exists() else {}

    # YC size + stage
    yc_domain, yc_name = load_yc()
    yc_hits = 0
    for c in companies:
        info = yc_domain.get(domain(c.get("website", ""))) if c.get("website") else None
        info = info or yc_name.get(norm(c["name"]))
        if info:
            cache.setdefault(c["name"], {}).update({k: v for k, v in info.items() if v})
            yc_hits += 1

    # rounds discover.py already parsed from funding headlines
    if CANDIDATES.exists():
        for r in csv.DictReader(CANDIDATES.open(encoding="utf-8")):
            if r.get("source") == "funding" and r.get("status") == "added" and r.get("signal"):
                e = cache.setdefault(r["name"], {})
                if not e.get("round"):
                    rnd = ROUND_RE.search(r["signal"])
                    amt = re.search(r"[$€£][\d.,]+\s?(?:[MBK]|million|billion)?", r["signal"])
                    e.update({"round": bucket(rnd.group(1) if rnd else ""), "round_text": rnd.group(1).title() if rnd else "",
                              "amount": norm_amount(amt.group(0)) if amt else "", "date": r.get("found_at", "")})

    # funding news, cached
    def stale(name):
        try:
            return (today - date.fromisoformat(cache.get(name, {}).get("checked_at", ""))).days >= args.refresh_days
        except ValueError:
            return True
    todo = [c["name"] for c in companies if stale(c["name"])][:args.max]
    print(f"  YC matches: {yc_hits} · news lookups: {len(todo)} (others cached)")

    def lookup(name):
        for attempt in range(3):
            try:
                return name, search_news(name), None
            except Exception as e:  # noqa: BLE001
                err = str(e)
                time.sleep(2 * (attempt + 1))
        return name, None, err

    found = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for name, hit, err in pool.map(lookup, todo):
            e = cache.setdefault(name, {})
            if err:
                failed += 1
                continue                               # leave unchecked so the next run retries
            e["checked_at"] = today.isoformat()
            if hit and (not e.get("date") or hit["date"] >= e.get("date", "")):
                e.update(hit)
                found += 1

    names = {c["name"] for c in companies}
    cache = {k: v for k, v in cache.items() if k in names}   # drop companies no longer listed
    OUT.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True))
    with_round = sum(1 for v in cache.values() if v.get("round"))
    print(f"\n{found} rounds found this run · {failed} lookups failed · "
          f"{with_round}/{len(companies)} companies have a round · {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
