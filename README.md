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

`discover.py` finds AI companies that are hiring, without you typing names:

| source | what it reads | how the job board is found |
|---|---|---|
| **HN "Who is hiring?"** | last 12 monthly threads (Algolia API) | link in the post, else the company site |
| **YC directory** | [yc-oss](https://github.com/yc-oss/api) companies tagged AI / Artificial Intelligence / Generative AI / Machine Learning that YC marks as hiring | company site; also brings sector, HQ and batch |
| **Funding news** | TechCrunch, Crunchbase News, Google News searches for AI rounds | slug guessed from the name, so always reviewed by hand |

For a lead without a job-board link, the company's homepage and `/careers`, `/jobs` pages are scanned for one. Scans run in parallel and are cached for 7 days, so the first run takes 10–15 minutes and later runs a few minutes.

Supported job boards (`ats.py`): Ashby, Greenhouse, Lever (incl. EU), Workable, SmartRecruiters, Recruitee, Personio, BambooHR, Breezy HR.

**How "AI company" is decided for HN posts.** Nearly every post mentions AI now, so a plain "AI" in the body doesn't count. A post needs a score of 3+: AI terms in the headline line (+2), "AI" or `.ai` in the company name/domain (+2), and +1 for each distinct specific term in the body (LLM, inference, computer vision, fine-tuning, ...). YC companies use YC's own tags instead.

```bash
python discover.py --auto-add                  # all sources
python discover.py --sources hn,yc --hn-months 6
```

Every lead is recorded in `data/candidates.csv` with a `status`:

| status | meaning |
|---|---|
| `added` | in `companies.csv` (board link found and verified to have jobs) |
| `new` | needs your review: slug guessed from a name, or the board is empty |
| `approved` / `rejected` | your decision; approved rows are added on the next run |
| `no_ats` | no supported job board found; re-checked weekly |
| `filtered` | not an AI company; earlier HN auto-adds that fail the filter are removed |

To review, edit `data/candidates.csv` in the GitHub web editor and change `status`.

## Add a company by hand

Add a row to `data/companies.csv`:

| column | example | notes |
|---|---|---|
| name | Sierra | display name |
| website | https://sierra.ai | |
| ats | ashby | any key in `ats.ADAPTERS`, or leave empty and run `fetch_jobs.py --detect` |
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

**Country** and **City** dropdowns narrow roles by location. Their counts follow the current role filter, so with FDE selected you see how many FDE roles each country and city has.

Locations are normalized by `locations.py`, which turns the many formats job boards use ("San Francisco, CA | New York City, NY", "Seattle, Washington", "Remote - US", "Bangalore India", ...) into standard city and country names. It resolves a country for about 99.7% of roles; the rest are regions like "EMEA".

Each preset has its own link, e.g. `…/ai-hiring-radar/#fde`, so you can bookmark it.

The filter reads `data/jobs.json`, which `fetch_jobs.py` writes with every open role.

## Track your applications (Google sign-in)

Sign in with Google (top right) to track roles. Pick a role filter, then use the **Track…** menu next to any job: Saved, Applied, Interviewing, Offer or Rejected. The company row shows a summary (e.g. "Applied 2 · Saved 1"), and the **Mine** filter shows only tracked roles, only applied ones, or hides roles you've already applied to. Tracked roles that disappear from a company's board stay in your list, marked "no longer listed".

Sign-in and storage use Firebase (free tier): Firebase Authentication for Google sign-in, Firestore for the records, stored at `users/{uid}/apps/{jobKey}`. `firestore.rules` limits every user to their own records; paste it into Firebase → Firestore Database → Rules. Visitors who don't sign in see the page exactly as before.

## What each number means

- **Eng roles** — titles matching engineer, researcher, scientist, ML, infra, platform, etc.
- **New (7d)** — roles first published in the last 7 days.
- `data/history.json` keeps daily counts per company, so you can chart growth later.

## Roadmap

- More discovery sources: VC portfolio job boards (check their terms first)
- Rippling and Workday job boards
- Funding round + investors per company
- Growth chart from `history.json`
