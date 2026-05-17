from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml


@dataclass
class SubredditConfig:
    name: str
    category: str = "general"
    enabled: bool = True


@dataclass
class ReplyTemplate:
    key: str
    name: str
    triggers: list[str] = field(default_factory=list)
    template: str = ""


@dataclass
class DirectoryConfig:
    """Directory-mode config: where to look for businesses."""

    search_queries: list[str] = field(default_factory=list)   # e.g. "home care agency"
    locations: list[str] = field(default_factory=list)        # e.g. "Tampa, FL"
    min_rating: float = 0.0
    max_per_query: int = 20
    category_label: str = ""                                  # tagged onto every BusinessLead


@dataclass
class OutreachTemplate:
    """Cold-email template for B2B outreach. Subject + body."""

    key: str
    name: str
    subject: str = ""
    body: str = ""


@dataclass
class NichePack:
    """A pluggable configuration bundle for a vertical (caregiver, hvac, etc.).

    Loaded from a YAML file under src/cwscraper/niches/. Drives keyword
    matching, default sources, and reply drafting per install.

    Two modes:
      - community  (default) — Reddit/YouTube/HN consumer-pain scanning
      - directory  — Google Places/Yelp business discovery for B2B outreach
    """

    slug: str
    display_name: str
    description: str = ""
    mode: str = "community"

    # --- community mode ---
    high_intent_keywords: list[str] = field(default_factory=list)
    medium_intent_keywords: list[str] = field(default_factory=list)
    subreddits: list[SubredditConfig] = field(default_factory=list)
    youtube_queries: list[str] = field(default_factory=list)
    youtube_channels: list[dict] = field(default_factory=list)
    hackernews_queries: list[str] = field(default_factory=list)
    reply_templates: list[ReplyTemplate] = field(default_factory=list)
    default_reply_template: str = "general_helpful"

    # --- directory mode ---
    directory: DirectoryConfig = field(default_factory=DirectoryConfig)
    outreach_templates: list[OutreachTemplate] = field(default_factory=list)
    default_outreach_template: str = "cold_intro"

    def enabled_subreddits(self) -> list[str]:
        return [s.name for s in self.subreddits if s.enabled]

    def reply_template(self, key: str) -> ReplyTemplate | None:
        for t in self.reply_templates:
            if t.key == key:
                return t
        return None

    def outreach_template(self, key: str) -> OutreachTemplate | None:
        for t in self.outreach_templates:
            if t.key == key:
                return t
        return None


def category_to_niche_map() -> dict[str, str]:
    """Build a {category_label -> niche_slug} index from every bundled niche.

    Used to backfill `source_niches` on businesses that were discovered
    before the field existed: the niche scanner had already stamped its
    `category_label` onto each business's `category` field, so we can
    reverse-look-up what niche they came from.

    If two niches share a category_label (shouldn't happen in practice
    but could), the first one wins (alphabetical order).
    """
    mapping: dict[str, str] = {}
    for meta in list_bundled_niches():
        try:
            pack = load_niche(meta["slug"])
        except Exception:
            continue
        label = (pack.directory.category_label or "").strip()
        if label and label not in mapping:
            mapping[label] = pack.slug
    return mapping


def list_bundled_niches() -> list[dict]:
    """Discover every niche pack bundled in cwscraper/niches/*.yaml.

    Returns a small metadata dict per pack (slug, display_name, mode,
    description). Used by the dashboard dropdown — full pack loading
    happens only when one is selected.
    """
    pkg_files = resources.files("cwscraper.niches")
    out: list[dict] = []
    for f in sorted(pkg_files.iterdir()):
        if not f.name.endswith(".yaml"):
            continue
        slug = f.name.removesuffix(".yaml")
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception as e:
            out.append({
                "slug": slug, "display_name": slug, "mode": "unknown",
                "description": f"(failed to parse: {e})",
                "error": str(e),
            })
            continue
        out.append({
            "slug": data.get("slug", slug),
            "display_name": data.get("display_name", slug),
            "mode": data.get("mode", "community"),
            "description": (data.get("description") or "").strip(),
        })
    return out


def load_niche(slug: str | None = None) -> NichePack:
    """Load a niche pack by slug.

    Resolution order:
      1. If `slug` is an absolute path or starts with ./, load that YAML file directly.
      2. Otherwise look up CWSCRAPER_NICHE env var (defaults to "caregiver").
      3. Load src/cwscraper/niches/<slug>.yaml from the installed package.
    """
    if slug and (slug.startswith("/") or slug.startswith("./") or slug.endswith(".yaml")):
        path = Path(slug).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Niche pack not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return _from_dict(data)

    slug = slug or os.getenv("CWSCRAPER_NICHE", "caregiver")
    pkg_files = resources.files("cwscraper.niches")
    yaml_file = pkg_files / f"{slug}.yaml"
    if not yaml_file.is_file():
        raise FileNotFoundError(f"Niche pack '{slug}' not bundled in cwscraper.niches")
    data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    return _from_dict(data)


def _from_dict(data: dict) -> NichePack:
    directory_block = data.get("directory") or {}
    directory = DirectoryConfig(
        search_queries=list(directory_block.get("search_queries", [])),
        locations=list(directory_block.get("locations", [])),
        min_rating=float(directory_block.get("min_rating", 0.0)),
        max_per_query=int(directory_block.get("max_per_query", 20)),
        category_label=directory_block.get("category_label", ""),
    )
    return NichePack(
        slug=data["slug"],
        display_name=data.get("display_name", data["slug"]),
        description=data.get("description", ""),
        mode=data.get("mode", "community"),
        high_intent_keywords=[k.lower() for k in data.get("high_intent_keywords", [])],
        medium_intent_keywords=[k.lower() for k in data.get("medium_intent_keywords", [])],
        subreddits=[SubredditConfig(**s) for s in data.get("subreddits", [])],
        youtube_queries=list(data.get("youtube_queries", [])),
        youtube_channels=list(data.get("youtube_channels", [])),
        hackernews_queries=list(data.get("hackernews_queries", [])),
        reply_templates=[
            ReplyTemplate(
                key=t["key"],
                name=t.get("name", t["key"]),
                triggers=[s.lower() for s in t.get("triggers", [])],
                template=t.get("template", ""),
            )
            for t in data.get("reply_templates", [])
        ],
        default_reply_template=data.get("default_reply_template", "general_helpful"),
        directory=directory,
        outreach_templates=[
            OutreachTemplate(
                key=t["key"],
                name=t.get("name", t["key"]),
                subject=t.get("subject", ""),
                body=t.get("body", ""),
            )
            for t in data.get("outreach_templates", [])
        ],
        default_outreach_template=data.get("default_outreach_template", "cold_intro"),
    )
