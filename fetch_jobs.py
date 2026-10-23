#!/usr/bin/env python3
"""
Fetch open roles for every company in data/companies.csv from its public job board,
and write data/companies.json (per-company stats) and data/jobs.json (every role).

Usage:
    python fetch_jobs.py                 # uses data/companies.csv
    python fetch_jobs.py --detect        # also try to auto-detect the board for rows with empty `ats`
    python fetch_jobs.py --workers 16    # parallel requests (default 12)

companies.csv columns:
    name,website,ats,slug,stage,last_raise,last_raise_date,sector,hq,notes
    - ats:  see ats.ADAPTERS (ashby, greenhouse, lever, lever_eu, workable, smartrecruiters,
            recruitee, personio, bamboohr, breezy), or empty to auto-detect with --detect
    - slug: the company's board name, e.g. jobs.ashbyhq.com/<slug>
"""
import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ats import ADAPTERS, BOARD_URL
from locations import COUNTRIES, parse_location

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "data" / "companies.csv"
OUT_PATH = ROOT / "data" / "companies.json"
HISTORY_PATH = ROOT / "data" / "history.json"
JOBS_PATH = ROOT / "data" / "jobs.json"
ENRICH_PATH = ROOT / "data" / "enrich.json"   # funding stage + size, written by enrich.py

ENG_RE = re.compile(
    r"\b(engineer|engineering|developer|swe|sre|devops|research(er)?|scientist|ml|machine learning|"
    r"infrastructure|platform|backend|frontend|full[- ]?stack|architect)\b",
    re.I,
)
DETECT_ORDER = ["ashby", "greenhouse", "lever", "workable", "recruitee", "smartrecruiters", "lever_eu"]


def detect_ats(slug):
    """Try each board type until one returns jobs."""
    for ats in DETECT_ORDER:
        try:
            jobs = ADAPTERS[ats](slug)
            if jobs:
                return ats, jobs
        except Exception:  # noqa: BLE001
            continue
    return None, []


def parse_date(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
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
        # a handful of fresh eng roles for the default (unfiltered) view
        "sample_eng_roles": [
            {"title": j["title"], "location": j["location"], "url": j["url"]}
            for j in sorted(eng, key=lambda j: str(j["posted"] or ""), reverse=True)[:5]
        ],
    }


def fetch_one(row, detect, retries=2):
    """Returns (row, ats, jobs, error). Runs in a worker thread."""
    slug, ats = row["slug"].strip(), row["ats"].strip().lower()
    for attempt in range(retries + 1):
        try:
            if not ats and detect:
                found, jobs = detect_ats(slug)
                if not found:
                    return row, ats, [], "no job board found"
                return row, found, jobs, None
            if ats not in ADAPTERS:
                return row, ats, [], f"unknown ats '{ats}'"
            return row, ats, ADAPTERS[ats](slug), None
        except Exception as e:  # noqa: BLE001
            err = str(e)
            if "404" in err or attempt == retries:
                return row, ats, [], err
            time.sleep(1.5 * (attempt + 1))   # transient error: back off and retry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true", help="auto-detect the board for rows with empty ats")
    ap.add_argument("--workers", type=int, default=12, help="parallel requests")
    args = ap.parse_args()

    started = time.time()
    now = datetime.now(timezone.utc)
    rows = [r for r in csv.DictReader(CSV_PATH.open(encoding="utf-8")) if r.get("slug", "").strip()]
    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else {}
    enrich = json.loads(ENRICH_PATH.read_text()) if ENRICH_PATH.exists() else {}
    results, failures, all_jobs = [], [], []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        outcomes = list(pool.map(lambda r: fetch_one(r, args.detect), rows))

    for row, ats, jobs, err in outcomes:
        name, slug = row["name"].strip(), row["slug"].strip()
        if err:
            failures.append((name, slug, ats, err))
            print(f"  ✗ {name:<24} {ats or '?':<15} {slug:<22} {err[:80]}", file=sys.stderr)
            continue

        stats = summarize(jobs, now)
        for j in jobs:
            d = parse_date(j["posted"])
            loc = parse_location(j["location"])
            for h in j.get("country_hints", []):
                c = COUNTRIES.get(str(h).strip().lower(), str(h).strip())
                if c and c not in loc["countries"]:
                    loc["countries"].append(c)
            all_jobs.append({"c": name, "t": j["title"], "l": j["location"], "u": j["url"],
                             "p": d.date().isoformat() if d else "",
                             "r": 1 if (j["remote"] or loc["remote"]) else 0,
                             "ci": loc["cities"], "co": loc["countries"]})

        prev = history.get(name, [])
        prev_count = prev[-1]["open_roles"] if prev else None
        stats["delta_since_last"] = None if prev_count is None else stats["open_roles"] - prev_count
        if not prev or prev[-1]["date"] != now.date().isoformat():
            history.setdefault(name, []).append({"date": now.date().isoformat(), "open_roles": stats["open_roles"]})
        else:
            prev[-1]["open_roles"] = stats["open_roles"]
        history[name] = history[name][-60:]  # ~2 months of daily points

        results.append({
            "name": name, "website": row.get("website", ""), "ats": ats, "slug": slug,
            "board_url": BOARD_URL[ats].format(slug=slug),
            "stage": row.get("stage", ""), "last_raise": row.get("last_raise", ""),
            "last_raise_date": row.get("last_raise_date", ""), "sector": row.get("sector", ""),
            "hq": row.get("hq", ""), "notes": row.get("notes", ""),
            **{("funding_" + k if k in ("round", "round_text", "round_est", "amount", "date", "url", "headline") else k): v
               for k, v in (enrich.get(name) or {}).items()
               if k in ("round", "round_text", "round_est", "amount", "date", "url", "headline", "team_size", "yc_stage", "yc_batch")},
            **stats,
        })
        print(f"  ✓ {name:<24} {ats:<15} {stats['open_roles']:>4} roles  ({stats['eng_roles']} eng, +{stats['new_7d']} this week)")

    results.sort(key=lambda r: r["eng_roles"], reverse=True)
    OUT_PATH.write_text(json.dumps({"updated_at": now.isoformat(), "companies": results}, indent=1, ensure_ascii=False))
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    JOBS_PATH.write_text(json.dumps({"updated_at": now.isoformat(), "jobs": all_jobs},
                                    ensure_ascii=False, separators=(",", ":")))
    print(f"\nWrote {len(results)} companies and {len(all_jobs)} roles → data/  "
          f"({len(failures)} failed, {time.time() - started:.0f}s)")


if __name__ == "__main__":
    main()
