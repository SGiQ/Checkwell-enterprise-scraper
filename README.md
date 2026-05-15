# CheckWell Enterprise Scraper

> Dual-mode lead engine. Monitor communities for prospect pain on Reddit/YouTube/HN, OR discover B2B targets via Google Places. Same dashboard, same pipeline, pluggable niche packs per vertical.

Built for selling wellness-check services to caregivers and the agencies that serve them — generalized into a niche-pack framework so the same engine ships for any vertical.

---

## What it does

### Community mode — find consumer pain points
- Scans Reddit, YouTube (API or RSS), and HackerNews for posts and comments matching your niche
- Scores intent (high / medium) from keyword classifiers in the niche pack
- Drafts replies from templates triggered by what the lead said
- Posts to Reddit via OAuth so you reply as yourself, not a bot

### Directory mode — find B2B prospects
- Scans Google Places (New) for businesses by category + location
- Six extraction passes per website to find contact emails (regex, mailto, Cloudflare-decoded, JSON-LD schema, text-obfuscation, Title-Case name pairing)
- Optional Playwright headless-browser enricher for JS-rendered sites
- Drafts cold emails from per-niche templates with placeholders for business name / city / contact

### Both modes share
- **Pipeline / CRM** — unified prospect view, B2B-flavored stages (`new → qualified → outreach_sent → reply_received → meeting_booked → customer / lost`), notes, tags, follow-up dates, activity log
- **Pre-flight readiness banner** — calls out missing env vars and config gaps *before* you click scan
- **Dashboard + JSON API** — every UI action has an endpoint
- **CSV export** for both leads and businesses

---

## Quick start

```bash
git clone https://github.com/SGiQ/Checkwell-enterprise-scraper.git
cd Checkwell-enterprise-scraper

pip install -e '.[dev]'                  # editable install with test deps
cp .env.example .env                     # then set the keys you need
cwscraper serve                          # dashboard at http://localhost:5050
```

For the Playwright enricher (heavyweight, optional):

```bash
pip install -e '.[dev,playwright]'
playwright install chromium
```

Or via Docker (includes Chromium):

```bash
docker compose up --build
```

CLI alternatives:

```bash
cwscraper scan --niche senior_care_agencies_se  # one-shot scan, prints JSON
cwscraper niches                                # list bundled niche packs
```

---

## Niche packs

A niche pack is a single YAML file under `src/cwscraper/niches/` that defines either community-mode (keywords + subreddits + search queries + reply templates) or directory-mode (business categories + locations + outreach email templates).

**Switch packs from the dashboard sidebar** — no restart needed. The choice persists in `data/config.json`.

### Bundled packs

| Slug | Mode | What it finds |
| --- | --- | --- |
| `caregiver` | Community | Adult children of aging parents on Reddit / YouTube / HN |
| `blank` | Community | Empty starter template |
| `senior_care_agencies_se` | Directory | Non-medical home-care agencies in FL/GA/SC/NC/TN |
| `home_health_agencies_se` | Directory | Medicare-certified home health agencies in SE US |
| `assisted_living_facilities_se` | Directory | ALF operators across SE US |
| `memory_care_facilities_se` | Directory | Dementia care communities |
| `geriatric_care_managers_us` | Directory | Aging Life Care professionals — national |
| `churches_se` | Directory | Faith communities (member-care ministry referral partners) |
| `mental_health_practices_se` | Directory | Therapy / counseling practices (caregiver-burnout + elderly-patient angles) |
| `drug_rehab_centers_se` | Directory | Addiction treatment + recovery centers (post-discharge support) |
| `outpatient_clinics_se` | Directory | Outpatient + ambulatory surgery centers (post-procedure follow-up) |

### Building your own

Copy `blank.yaml`, fill in the fields for your mode, save as `<your-slug>.yaml`, restart. It'll appear in the dropdown automatically. See [docs/NICHE_PACKS.md](docs/NICHE_PACKS.md) for the schema.

---

## Pipeline (CRM-lite)

Both lead types (community posts + business agencies) flow through the same B2B-toned pipeline. The **Pipeline** tab in the dashboard shows all prospects grouped by stage with one-click stage transitions. Click any prospect to open a side drawer with stage / follow-up date / tags / notes / activity log — everything autosaves.

Stages: `new → qualified → outreach_sent → reply_received → meeting_booked → customer / lost`.

Legacy `status` values on existing rows are auto-mapped to pipeline stages on first read (no migration needed).

---

## Contact enrichment

Two enrichers, run on already-discovered business leads:

### `website` (fast, default)
Visits the homepage + up to 7 candidate contact pages (`/contact`, `/about`, `/our-team`, `/locations`, `/leadership`, etc.) and runs six extraction passes:

1. `mailto:` links — name comes free as link text
2. Cloudflare-obfuscated emails (`data-cfemail="..."` XOR-decoded)
3. JSON-LD structured data (`schema.org` `email` / `contactPoint` walker)
4. Text-style obfuscation (`info [at] example [dot] com` → `info@example.com`)
5. Title-Case name regex preceding plain-text emails
6. Residual emails with no nearby name

~0.5s per page. Real-world hit rate on small-business B2B: 30–60%.

### `playwright` (deep)
Renders each page through headless Chromium so JS-loaded mailtos resolve. Inherits the same six extraction passes. ~3–5s per page; cracks the ~50% of misses from the fast enricher. Requires `pip install '.[playwright]' && playwright install chromium` locally; the bundled Dockerfile installs it for you.

Both run via `POST /api/enrich` with `{"enricher": "website|playwright", "only_missing_email": true}`. The default `only_missing_email=true` skips businesses that already have an email — re-runs are cheap.

### Manual entry
Per-business `+ add email` button on the Businesses tab opens an inline editor for email + name + title. Saves to `contacts[]` with `source_url: "manual"` so provenance is preserved. The `Re-enrich` button next to each row runs the chosen enricher on just one business with a synchronous result toast.

---

## Architecture

```
src/cwscraper/
├── core/
│   ├── models.py         Lead + BusinessLead + pipeline constants
│   ├── niche.py          NichePack loader + list_bundled_niches()
│   ├── scoring.py        intent classifier
│   ├── store.py          Repository protocol + JSONRepository
│   ├── engine.py         scan dispatch (community vs directory)
│   ├── scheduler.py      auto-scan loop
│   └── preflight.py      readiness check (blockers / warnings / notes)
├── scanners/             community-mode platforms
│   ├── reddit.py
│   ├── youtube.py
│   ├── hackernews.py
│   └── google_places.py  directory-mode (Places API New)
├── enrichment/
│   ├── website_scraper.py    fast HTML-based enricher
│   └── playwright_scraper.py headless Chromium variant
├── replies/
│   ├── drafter.py        community reply templates
│   ├── outreach.py       B2B cold-email templates
│   └── reddit_poster.py  OAuth + posting
├── niches/               7 bundled YAML packs
├── web/
│   ├── app.py            Flask routes (AppContext for runtime niche-swap)
│   └── templates/dashboard.html
└── cli.py                `cwscraper` entry point
```

Storage layer sits behind a `Repository` protocol so the JSON backend can be swapped for SQLAlchemy + Postgres in Phase 2 without touching scanners or routes.

---

## Configuration

All knobs are env vars (set in `.env` locally or Railway Variables in prod):

| Variable | Purpose |
| --- | --- |
| `CWSCRAPER_NICHE` | Default niche pack on first boot (later overridden by the dashboard dropdown, which persists to `config.json`) |
| `CWSCRAPER_DATA_DIR` | Where to keep JSON state (default: `./data`; set to `/data` on Railway with a Volume) |
| `CWSCRAPER_PORT` | Dashboard port (default: `5050`; Railway injects `$PORT`) |
| `CWSCRAPER_SECRET` | Flask session secret — required in prod |
| `CWSCRAPER_ENRICHMENT_WORKERS` | Thread-pool size for batch enrichment (default: 5; lower to 1–2 if running Playwright on a small instance) |
| `GOOGLE_PLACES_API_KEY` | Required for directory-mode niches. Enable "Places API (New)" in your Google Cloud project. |
| `YOUTUBE_API_KEY` | Optional. Without it, YouTube scanner falls back to RSS for channels in the niche pack. |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Required for posting replies via the dashboard. Reddit blocks anonymous `.json` access from cloud IPs (Railway, etc.) so these are effectively required for reading too if you deploy. |
| `CWSCRAPER_REDDIT_REDIRECT_URI` | Reddit OAuth callback — must match the redirect URI in your Reddit app |

The dashboard's **pre-flight banner** introspects this list at runtime and tells you what's missing for the active niche before you click scan.

---

## API

The dashboard is built entirely on the JSON API:

```
# Health & introspection
GET    /api/health                       liveness + active niche
GET    /api/preflight                    blockers/warnings for the active niche

# Niche management
GET    /api/niches                       list bundled packs + which is active
GET    /api/niches/active                current niche detail
POST   /api/niches/active                {slug} — swap niche at runtime

# Pipeline (CRM-lite)
GET    /api/pipeline/config              stages + labels
GET    /api/pipeline/stats               counts per stage + overdue + by lead_type
GET    /api/prospects                    unified list (community + business)
GET    /api/prospects/<id>               single prospect + activity log
POST   /api/prospects/<id>/stage         change pipeline stage
POST   /api/prospects/<id>/notes         set notes
POST   /api/prospects/<id>/follow-up     {follow_up_date: YYYY-MM-DD}
POST   /api/prospects/<id>/tags          replace tag list

# Community-mode leads
GET    /api/leads?intent=high            filter by intent / status / source / search
POST   /api/leads/<id>/status            new/reviewed/contacted/dismissed
GET    /api/replies                      drafts + sent
POST   /api/replies/draft                generate draft from niche templates
POST   /api/replies/save                 store edited draft
POST   /api/replies/send                 post to Reddit via OAuth
GET    /api/export/csv                   leads -> CSV

# Directory-mode businesses
GET    /api/businesses                   filter by state / city / status / min_rating / search
GET    /api/businesses/stats             counts + enrichment coverage
POST   /api/businesses/<id>/status       qualified/contacted/dismissed
POST   /api/businesses/<id>/contact      manual email + contacts edit
POST   /api/businesses/<id>/enrich       single-business re-enrich
GET    /api/businesses/export/csv        businesses -> CSV
POST   /api/outreach/draft               cold-email draft from niche templates
GET    /api/outreach/templates           list available templates

# Scanning
POST   /api/scan                         trigger a scan (background, mode-aware)
GET    /api/scan/status                  live progress
POST   /api/enrich                       batch enrichment {enricher, only_missing_email, limit}
GET    /api/enrich/status                live enrichment progress

# Reddit OAuth
GET    /auth/reddit                      start OAuth flow
GET    /auth/reddit/callback             OAuth return
GET    /api/auth/reddit/status           connected?
POST   /api/auth/reddit/disconnect       wipe token
```

---

## Testing

```bash
pytest                          # 109 tests, ~5s
pytest --cov=src/cwscraper      # coverage report
```

Coverage spans: scoring, niche loading + switching, storage, scanner parsers, enrichment passes (incl. mocked Playwright), pipeline transitions, manual contact validation, preflight evaluator across niche modes + env permutations.

---

## Roadmap

| Phase | Status | Scope |
| --- | --- | --- |
| **0.1** | ✅ shipped | Standalone single-tenant. Reddit + YouTube + HN. JSON storage. CLI + dashboard. Caregiver niche pack. |
| **0.2** | ✅ shipped | Directory mode (Google Places). 5 B2B niches. Playwright enricher. Pipeline / CRM-lite. Pre-flight readiness. Niche dropdown w/ runtime swap. Manual contact entry. |
| **0.3** | planned | Multi-tenant: Postgres, workspaces, Stripe (Solo + Agency tiers), encrypted OAuth tokens, custom niche pack UI. |
| **0.4** | planned | Agency features: public API w/ key auth, white-label theming, seat management, webhook outputs (HubSpot/GoHighLevel/Pipedrive), Hunter.io / Apollo enrichment. |

See [docs/MONETIZATION.md](docs/MONETIZATION.md) for the SaaS productization plan.

---

## License

Proprietary. © SGiQ Business Strategies. All rights reserved.
