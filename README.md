# AI Hiring Radar

A directory of AI startups ranked by how many engineering roles they have open right now.
Job data comes straight from each company's public ATS board (Ashby, Greenhouse, Lever) — no scraping of HTML, no paid data.

## Run it locally

```bash
python fetch_jobs.py --detect      # pulls live roles → data/companies.json
python -m http.server 8000         # open http://localhost:8000
```

No dependencies beyond the Python standard library.

## Add a company

Add a row to `data/companies.csv`:

| column | example | notes |
|---|---|---|
| name | Sierra | display name |
| website | https://sierra.ai | |
| ats | ashby | `ashby` / `greenhouse` / `lever`, or leave empty and run with `--detect` |
| slug | sierra | the board name in `jobs.ashbyhq.com/<slug>`, `job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>` |
| stage, last_raise, last_raise_date | Series C, $175M, 2026-09 | fill in from funding news |
| sector, hq, notes | AI agents (CX), San Francisco | |

Finding the slug: open the company's careers page and click any job — the URL shows the ATS and slug.

## Deploy for free

1. Push this folder to a GitHub repo.
2. Settings → Pages → deploy from the `main` branch root.
3. The workflow in `.github/workflows/refresh.yml` refreshes the data daily and commits it.

## What each number means

- **Eng roles** — titles matching engineer, researcher, scientist, ML, infra, platform, etc.
- **New (7d)** — roles first published in the last 7 days.
- `data/history.json` keeps daily counts per company, so you can chart growth later.

## Roadmap

- Auto-discover new companies from HN "Who's Hiring" (Algolia API) and funding-news RSS → a review queue
- Funding round + investors per company
- Growth chart from `history.json`
