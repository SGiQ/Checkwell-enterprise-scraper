# Monetization & Productization Plan

> What this is, who buys it, and how we ship it from open-source code to recurring revenue.

## Positioning

A **community lead engine** for niches where prospects publicly describe their problem before they search for a solution. Caregiving, home services, B2B SaaS — anywhere people post "has anyone dealt with…" on Reddit, YouTube, or HackerNews.

What makes this different from F5Bot / GummySearch / Brand24:
- **Niche packs**, not raw keyword alerts. Pre-tuned scoring + reply templates per vertical.
- **Reply drafting + posting**, not just monitoring. Closes the loop from signal to outreach.
- **Self-hostable**, so customers own their OAuth tokens and lead data.

## Distribution: SaaS-hosted

Decided in initial planning. SGiQ operates the infrastructure; customers sign up at `scraper.checkwellcall.com` (or a dedicated subdomain) and never deploy anything.

Trade-offs to manage:
- **Reddit OAuth tokens are per-customer**, encrypted at rest, scoped to `submit identity`. We are a tool that helps customers post as themselves — not a broker that resells their content.
- **YouTube API quota** is per-project; we'll either ship a shared quota for Solo and require customer-supplied keys for Agency, or proxy through a billed Google Cloud project and meter usage.
- **Multi-tenant Postgres** replaces the JSON store. The `Repository` interface in `cwscraper/core/store.py` is the seam.

## Target customers

Two segments, one codebase:

### Caregiver / senior-care vendors (Solo tier)
- Wellness-check services, home-care agencies, AgeTech startups, fall-detection vendors
- Buyer: founder or growth lead
- Pain: high-intent caregivers describe their problem on Reddit daily, but manual monitoring doesn't scale
- Sold against: manual Reddit browsing, F5Bot keyword alerts, no targeted outreach tool exists
- **Price target: $49–$99/mo**

### B2B SaaS growth teams + marketing agencies (Agency tier)
- Growth teams looking for Reddit lead signals for their own product
- Agencies running Reddit outreach for multiple clients
- Buyer: growth ops lead or agency owner
- Pain: needs multi-tenant workspaces, custom niche packs per client, API access for downstream tools
- Sold against: custom-built scrapers, manual VAs, expensive social listening platforms (Sprinklr, Brandwatch)
- **Price target: $299–$499/mo**

## Pricing tiers

| Feature | Solo | Agency |
| --- | --- | --- |
| Workspaces | 1 | Unlimited |
| Niche packs | Caregiver (pre-loaded) | Custom (build your own) |
| Reddit accounts | 1 OAuth | N OAuth |
| Scans/month | 60 (cap) | Unlimited |
| User seats | 1 | 5 (then $20/seat) |
| Public API access | – | ✓ |
| White-label theming | – | ✓ |
| CSV export | ✓ | ✓ |
| Webhook outputs | – | ✓ |
| Priority support | – | ✓ |
| **MSRP** | **$79/mo** | **$399/mo** |

## Roadmap

### Phase 1 — Standalone v0.1 *(complete)*
- Fork lead-agent code into standalone repo
- Extract scanners from the 1,800-line monolith into clean per-platform modules
- Serialize caregiver keywords as `niches/caregiver.yaml`
- JSON storage behind `Repository` interface
- CLI + Flask dashboard + Docker + Railway config
- Goal: engine works detached from the CheckWellCall main app

### Phase 2 — Multi-tenant SaaS v0.2
- **Postgres + SQLAlchemy** — add `SqlAlchemyRepository` implementing `cwscraper/core/store.py:Repository`
- **Workspaces + users** — every row scoped by `workspace_id`; users belong to workspaces
- **Auth** — magic-link email login (Postmark/SES), Google OAuth for sign-up
- **Stripe billing** — Solo + Agency products, usage caps enforced server-side
- **OAuth token vault** — Fernet-encrypt at rest, per-workspace KMS key in Phase 3
- **Background workers** — RQ + Redis for scans; Flask process stays responsive
- **Migrate dashboard** to be workspace-scoped (current dashboard assumes single-tenant)
- Goal: first paying customer onboarded

### Phase 3 — Agency features v0.3
- **Public API** with bearer-token auth, per-key rate limits
- **Webhook outputs** — fire on new high-intent lead, on reply sent, on scan complete
- **White-label theming** — logo / accent color per workspace, custom subdomain
- **Seat management** — invite flow, roles (admin / reviewer / read-only)
- **Niche pack builder UI** — point-and-click keyword/subreddit editor backed by the YAML loader
- Goal: agency tier ready, first multi-client agency on the platform

## Risks & how we handle them

| Risk | Mitigation |
| --- | --- |
| Reddit ToS / scraping concerns | Customers post via their own OAuth; we never resell lead data; rate-limit the unauthenticated `.json` calls; clearly position as a tool, not a data broker |
| Scanner fragility (HTML changes break parsers) | Phase 1 ships only the stable scanners (Reddit JSON, YouTube API/RSS, HN Algolia). Quora and AgingCare were dropped from v1 — DDG dependency was already breaking. Re-add later with Playwright + proxies only if customer demand warrants. |
| YouTube quota costs at scale | Charge Agency tier for API access; Solo tier defaults to RSS fallback (no quota) |
| Pirating self-hosted version | Ship a `LICENSE_KEY` env var checked against a license server (phone-home weekly). Open-core consideration if community demand emerges. |
| Customer support load | In-app docs, video walk-throughs, niche pack templates. Tier pricing assumes self-serve onboarding for Solo. |

## Eat our own dogfood

CheckWellCall's existing `app/services/lead_monitor_service.py` becomes a thin client that hits the public API. CheckWellCall is customer #1. This forces the API to be good before we sell it.

## Open questions to revisit before Phase 2

- Hosting target: Railway (current) vs Fly.io (better multi-region) vs a single Hetzner box (cheapest)
- Auth provider: WorkOS (enterprise sales-ready) vs roll-our-own magic links (cheaper, slower)
- Background workers: RQ (simpler, fewer deps) vs Celery (battle-tested, heavier)
- Whether to ship a "Bring Your Own Postgres" tier for privacy-conscious enterprise leads
