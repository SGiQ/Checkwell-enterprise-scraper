# CheckWell Enterprise Scraper

> Multi-platform community lead engine with pluggable niche packs, reply drafting, and Reddit OAuth posting.

Find prospects asking for help in communities, draft personalized replies, and (optionally) post them — all driven by a single YAML config per vertical. Ships pre-tuned for senior care, easily retargeted for any niche.

---

## What it does

- **Scans Reddit, YouTube, and HackerNews** for posts and comments matching your niche
- **Scores intent** (high / medium) using keyword classifiers from your niche pack
- **Drafts replies** from templates tied to lead signals (e.g. "lives alone" → wellness-check template)
- **Posts to Reddit** via OAuth so you reply as yourself, not as a bot account
- **Dashboard + JSON API** for review, export, and integration with downstream CRMs

## Quick start

```bash
git clone https://github.com/SGiQ/Checkwell-enterprise-scraper.git
cd Checkwell-enterprise-scraper

pip install -e .[dev]                 # editable install with test deps
cp .env.example .env                  # then edit secrets if needed
cwscraper serve                       # dashboard at http://localhost:5050
```

Or run a one-shot scan:

```bash
cwscraper scan --niche caregiver
```

Or via Docker:

```bash
docker compose up --build
```

## Niche packs

A niche pack is a single YAML file under `src/cwscraper/niches/` that defines:

- High-intent and medium-intent keyword lists (used for scoring)
- Reddit subreddits to scan
- YouTube search queries + RSS fallback channels
- HackerNews search queries
- Reply templates with trigger phrases

Ship a new vertical by copying `niches/blank.yaml`, filling it in, and running:

```bash
CWSCRAPER_NICHE=plumbing cwscraper serve
```

The caregiver pack ships in the box — same keywords that drive CheckWellCall's own lead funnel.

## Architecture

```
src/cwscraper/
├── core/         pure logic: models, scoring, niche loader, scan engine, scheduler, storage
├── scanners/     one module per platform (reddit, youtube, hackernews) + base class
├── replies/      template drafter + Reddit OAuth poster
├── niches/       YAML niche packs (caregiver.yaml, blank.yaml)
├── web/          Flask dashboard + JSON API
└── cli.py        `cwscraper` entry point
```

Storage today is JSON files (good enough for single-tenant). A `Repository` protocol in `core/store.py` lets a Postgres/SQLAlchemy backend slot in for Phase 2 multi-tenant without touching scanners or routes.

## Configuration

All knobs are in `.env`:

| Variable | Purpose |
| --- | --- |
| `CWSCRAPER_NICHE` | Which niche pack to load (default: `caregiver`) |
| `CWSCRAPER_DATA_DIR` | Where to keep JSON state (default: `./data`) |
| `CWSCRAPER_PORT` | Dashboard port (default: `5050`) |
| `CWSCRAPER_SECRET` | Flask session secret — set in production |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key. Without it the YouTube scanner falls back to RSS feeds of channels listed in the niche pack. |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Reddit OAuth app credentials, needed only if you want to post replies from the dashboard. Create at https://www.reddit.com/prefs/apps |

## API

The dashboard is built on a JSON API — every UI action has an endpoint. Highlights:

```
GET  /api/health                # liveness probe
GET  /api/stats                 # roll-up counters for the dashboard
GET  /api/leads?intent=high     # filtered leads
POST /api/scan                  # trigger a scan (background)
GET  /api/scan/status           # live progress for the UI
POST /api/leads/<id>/status     # mark new/reviewed/contacted/dismissed
POST /api/replies/draft         # generate a draft using the niche templates
POST /api/replies/send          # post the reply to Reddit via OAuth
GET  /api/export/csv            # export all leads
```

`/api/health` is what Railway's healthcheck hits — keep it free for monitoring.

## CLI

```
cwscraper serve [--niche caregiver] [--port 5050] [--debug]
cwscraper scan  [--niche caregiver]      # one-shot, prints result JSON
cwscraper niches                         # list bundled niche packs
```

## Roadmap

See [docs/MONETIZATION.md](docs/MONETIZATION.md) for the productization plan.

| Phase | Scope |
| --- | --- |
| **0.1** (this release) | Standalone single-tenant. Reddit + YouTube + HackerNews. JSON storage. CLI + dashboard. Pluggable niche packs. |
| **0.2** | Multi-tenant: Postgres, workspaces, Stripe (Solo + Agency tiers), encrypted OAuth tokens. |
| **0.3** | Agency features: public API with key auth, white-label theming, seat management, custom niche pack builder. |

## License

Proprietary. © SGiQ Business Strategies. All rights reserved.
