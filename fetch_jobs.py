#!/usr/bin/env python3
"""
Fetch open roles for AI startups from public ATS job-board APIs
(Ashby / Greenhouse / Lever) and write data/companies.json.

Usage:
    python fetch_jobs.py                 # uses data/companies.csv
    python fetch_jobs.py --detect        # also try to auto-detect ATS for rows with empty `ats`

companies.csv columns:
    name,website,ats,slug,stage,last_raise,last_raise_date,sector,hq,notes
    - ats:  ashby | greenhouse | lever | (empty = auto-detect with --detect)
    - slug: the company's board name, e.g. jobs.ashbyhq.com/<slug>
"""
import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "data" / "companies.csv"
OUT_PATH = ROOT / "data" / "companies.json"
HISTORY_PATH = ROOT / "data" / "history.json"

ENG_RE = re.compile(
    r"\b(engineer|engineering|developer|swe|sre|devops|research(er)?|scientist|ml|machine learning|"
    r"infrastructure|platform|backend|frontend|full[- ]?stack|architect)\b",
    re.I,
)
UA = {"User-Agent": "ai-startup-directory/0.1 (+personal project)"}


def get_json(url, timeout=20):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- ATS adapters: each returns list of {title, location, url, posted, remote} ----------

def fetch_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({
            "title": j.get("title", "").strip(),
            "location": j.get("location") or "",
            "url": j.get("jobUrl"),
            "posted": j.get("publishedAt"),
            "remote": bool(j.get("isRemote")) or (j.get("workplaceType") == "Remote"),
            "department": j.get("department") or "",
        })
    return out


def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append({
            "title": j.get("title", "").strip(),
            "location": loc,
            "url": j.get("absolute_url"),
            "posted": j.get("first_published") or j.get("updated_at"),
            "remote": "remote" in loc.lower(),
            "department": "",
        })
    return out


def fetch_lever(slug, host="api.lever.co"):
    data = get_json(f"https://{host}/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        ts = j.get("createdAt")
        out.append({
            "title": j.get("text", "").strip(),
            "location": loc,
            "url": j.get("hostedUrl"),
            "posted": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
            "remote": (j.get("workplaceType") == "remote") or "remote" in loc.lower(),
            "department": cat.get("team") or "",
        })
    return out


def fetch_lever_eu(slug):
    return fetch_lever(slug, host="api.eu.lever.co")


ADAPTERS = {"ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever, "lever_eu": fetch_lever_eu}
BOARD_URL = {
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "lever_eu": "https://jobs.eu.lever.co/{slug}",
}


def detect_ats(slug):
    """Try each ATS until one returns a non-empty board."""
    for ats, fn in ADAPTERS.items():
        try:
            jobs = fn(slug)
            if jobs:
                return ats, jobs
        except (HTTPError, URLError, ValueError):
            continue
    return None, []


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize(jobs, now):
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    eng = [j for j in jobs if ENG_RE.search(j["title"])]
    new7 = [j for j in jobs if (d := parse_date(j["posted"])) and d >= week_ago]
    new30 = [j for j in jobs if (d := parse_date(j["posted"])) and d >= month_ago]
    return {
        "open_roles": len(jobs),
        "eng_roles": len(eng),
        "new_7d": len(new7),
        "new_30d": len(new30),
        "remote_roles": sum(1 for j in jobs if j["remote"]),
        # keep a handful of fresh eng roles for the UI
        "sample_eng_roles": [
            {"title": j["title"], "location": j["location"], "url": j["url"]}
            for j in sorted(eng, key=lambda j: j["posted"] or "", reverse=True)[:5]
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true", help="auto-detect ATS for rows with empty ats")
    ap.add_argument("--sleep", type=float, default=0.5, help="seconds between requests (be polite)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else {}
    results, failures = [], []

    for row in rows:
        name, slug, ats = row["name"].strip(), row["slug"].strip(), row["ats"].strip().lower()
        try:
            if not ats and args.detect:
                ats, jobs = detect_ats(slug)
                if not ats:
                    raise ValueError("no ATS board found")
            elif ats in ADAPTERS:
                jobs = ADAPTERS[ats](slug)
            else:
                raise ValueError(f"unknown ats '{ats}'")
        except Exception as e:  # noqa: BLE001
            failures.append((name, slug, ats, str(e)))
            print(f"  ✗ {name:<20} {ats or '?':<10} {slug:<20} {e}", file=sys.stderr)
            continue

        stats = summarize(jobs, now)
        # growth vs. last snapshot
        prev = history.get(name, [])
        prev_count = prev[-1]["open_roles"] if prev else None
        stats["delta_since_last"] = None if prev_count is None else stats["open_roles"] - prev_count
        history.setdefault(name, []).append({"date": now.date().isoformat(), "open_roles": stats["open_roles"]})
        history[name] = history[name][-60:]  # keep ~2 months of daily points

        results.append({
            "name": name,
            "website": row.get("website", ""),
            "ats": ats,
            "slug": slug,
            "board_url": BOARD_URL[ats].format(slug=slug),
            "stage": row.get("stage", ""),
            "last_raise": row.get("last_raise", ""),
            "last_raise_date": row.get("last_raise_date", ""),
            "sector": row.get("sector", ""),
            "hq": row.get("hq", ""),
            "notes": row.get("notes", ""),
            **stats,
        })
        print(f"  ✓ {name:<20} {ats:<10} {stats['open_roles']:>4} roles  ({stats['eng_roles']} eng, +{stats['new_7d']} this week)")
        time.sleep(args.sleep)

    results.sort(key=lambda r: r["eng_roles"], reverse=True)
    OUT_PATH.write_text(json.dumps({"updated_at": now.isoformat(), "companies": results}, indent=2, ensure_ascii=False))
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    print(f"\nWrote {len(results)} companies → {OUT_PATH.relative_to(ROOT)}  ({len(failures)} failed)")
    if failures:
        print("Fix the slug/ats for failed rows in data/companies.csv (or run with --detect).")


if __name__ == "__main__":
    main()
