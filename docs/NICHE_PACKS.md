# Authoring a Niche Pack

A niche pack is a YAML file that tells the engine what to look for, where to look, and what to say in reply.

## Anatomy

```yaml
slug: hvac                          # required, used as CWSCRAPER_NICHE value
display_name: HVAC & Home Comfort   # shown in dashboard header
description: |
  Optional. Help future-you remember who this pack is for.

high_intent_keywords:               # match anywhere in title+body, lower-cased
  - ac broke
  - furnace not working
  - heat won't turn on
  - hvac repair near me

medium_intent_keywords:
  - high energy bill
  - thermostat issues
  - replacing hvac
  - heat pump questions

subreddits:                         # Reddit communities to scan
  - { name: HVAC,           category: primary,   enabled: true  }
  - { name: HomeImprovement, category: general,   enabled: true  }
  - { name: HVACAdvice,     category: primary,   enabled: true  }
  - { name: Plumbing,       category: adjacent,  enabled: false }

youtube_queries:                    # used when YOUTUBE_API_KEY is set
  - hvac repair tutorial
  - furnace troubleshooting
  - heat pump installation

youtube_channels:                   # RSS fallback when no API key
  - { name: This Old House, id: UC...replace-with-real-channel-id... }

hackernews_queries:                 # HN Algolia full-text search
  - smart thermostat
  - heat pump efficiency

default_reply_template: general_helpful

reply_templates:
  - key: ac_broke
    name: AC Emergency
    triggers: [ac broke, ac not working, no cool air]
    template: |
      Sorry you're dealing with this — AC failure in summer is the worst.
      A few quick things to check before you call a tech:
      1. Replace the air filter (sounds dumb, fixes ~10% of calls)
      2. Verify the breaker hasn't tripped
      3. Make sure the outdoor unit isn't blocked by debris

      If those don't help, get a tech out same-day — it's worth the
      emergency rate vs. losing food in the fridge.

  - key: general_helpful
    name: General Helpful
    triggers: []                    # empty = catch-all fallback
    template: |
      Replace this with something useful for the vertical.
```

## Where it lives

Bundled packs: `src/cwscraper/niches/<slug>.yaml`. These are installed with the package.

User packs: any absolute path passed as the slug — `cwscraper serve --niche /path/to/mine.yaml`.

## How matching works

For every post (or comment, or video, or HN hit) the scanner builds a `title + body` string, lowercases it, and:

1. Tries each `high_intent_keyword` as a substring match. First hit → `intent_level=high`.
2. If no high hit, tries each `medium_intent_keyword`. First hit → `intent_level=medium`.
3. If neither, the lead is dropped.

YouTube search-API hits are special — the search query itself is already targeted, so a hit gets `medium` even with no keyword match. RSS fallback requires a real keyword match.

## How reply templates pick

For each lead, `replies/drafter.py` walks `reply_templates` in order and picks the first one whose `triggers` list has any substring matching the lead's title+preview. If none match, it falls back to `default_reply_template`.

Templates support two placeholders today: `{parent}` and `{parent_name}`. Add your own by editing `replies/drafter.py:_personalize`.

## Iteration loop

1. Edit your YAML
2. `cwscraper niches` to confirm it loads
3. `cwscraper scan --niche <slug>` to dry-run
4. Tune keywords based on what got matched (or didn't)
5. Open the dashboard at `/api/leads?intent=high` to spot-check quality

## Anti-patterns

- **Generic keywords**: `"car"` or `"help"` match everything. Use multi-word phrases that capture *intent* (`"my car won't start"`, `"need help with leaky pipe"`).
- **Too many subreddits**: Each one adds a request + rate-limit risk. Start with 4–6 high-signal subs; expand only after you see real lead flow.
- **Salesy templates**: Reddit detects and downvotes. Templates should sound like a human who's been through it, mentioning your solution only as part of a broader recommendation.
