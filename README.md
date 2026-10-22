# AI Hiring Radar

A directory of AI startups ranked by how many engineering roles they have open right now.
Job data comes straight from each company's public ATS board (Ashby, Greenhouse, Lever) — no scraping of HTML, no paid data.

## Run it locally

```bash
python fetch_jobs.py --detect      # pulls live roles → data/companies.json
python -m http.server 8000         # open http://localhost:8000
```

No dependencies beyond the Python standard library.

## Automatic discovery

`discover.py` finds new AI companies that are hiring, without you typing names:

- **HN "Who is hiring?"** — reads the latest monthly thread, keeps AI companies, and pulls the ATS link from the post. If the post has none, it scans the company's website and `/careers` page for one.
- **Funding news** — reads TechCrunch, Crunchbase News and Google News searches, picks out "X raises $40M Series B" headlines for AI companies, and guesses the job-board slug.

Supported job boards: Ashby, Greenhouse, Lever (incl. EU), Workable, SmartRecruiters.

**How "AI company" is decided.** Nearly every post mentions AI now, so a plain "AI" in the body doesn't count. A post needs a score of 3+: AI terms in the headline line (+2), "AI" or `.ai` in the company name/domain (+2), and +1 for each distinct specific term in the body (LLM, inference, computer vision, fine-tuning, ...). Companies auto-added from HN that fail a later, stricter filter are removed automatically and marked `filtered`.

```bash
python discover.py --auto-add
```

Results go to `data/candidates.csv`:

| confidence | what happens |
|---|---|
| `high` (ATS link found in the post or on the company site, board verified) | added to `companies.csv` automatically with `--auto-add` |
| `review` (slug guessed from a name, or no ATS found) | waits in `candidates.csv` with `status=new` |

To review: edit `data/candidates.csv` (the GitHub web editor works) and set `status` to `approved` or `rejected`. Approved rows are added on the next run. Guessed slugs are never auto-added, because the same slug can belong to a different company on another ATS.

The daily GitHub Action runs discovery first, then fetches roles.

## Add a company by hand

Add a row to `data/companies.csv`:

| column | example | notes |
|---|---|---|
| name | Sierra | display name |
| website | https://sierra.ai | |
| ats | ashby | `ashby` / `greenhouse` / `lever` / `lever_eu`, or leave empty and run with `--detect` |
| slug | sierra | the board name in `jobs.ashbyhq.com/<slug>`, `job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>` |
| stage, last_raise, last_raise_date | Series C, $175M, 2026-09 | fill in from funding news |
| sector, hq, notes | AI agents (CX), San Francisco | |

Finding the slug: open the company's careers page and click any job — the URL shows the ATS and slug.

## Deploy for free

1. Push this folder to a GitHub repo.
2. Settings → Pages → deploy from the `main` branch root.
3. The workflow in `.github/workflows/refresh.yml` refreshes the data daily and commits it.

## Filter by role

The page has a role filter above the company list: type any job title (comma-separate several, e.g. `forward deployed, solutions engineer`) or click a preset. Presets: Forward Deployed (FDE), Solutions / Sales Eng, Customer / Support Eng, Applied AI / ML Eng, Research, Infra / Platform. Add **Remote only** to narrow further.

Each preset has its own link, e.g. `…/ai-hiring-radar/#fde`, so you can bookmark it.

The filter reads `data/jobs.json`, which `fetch_jobs.py` writes with every open role.

## What each number means

- **Eng roles** — titles matching engineer, researcher, scientist, ML, infra, platform, etc.
- **New (7d)** — roles first published in the last 7 days.
- `data/history.json` keeps daily counts per company, so you can chart growth later.

## Roadmap

- More discovery sources: VC portfolio pages, YC directory, more funding feeds
- Funding round + investors per company
- Growth chart from `history.json`
