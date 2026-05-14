from cwscraper.enrichment.base import BaseEnricher, EnrichmentContext, EnrichmentResult
from cwscraper.enrichment.website_scraper import WebsiteScraper

ALL_ENRICHERS: dict[str, type[BaseEnricher]] = {
    "website": WebsiteScraper,
}

__all__ = [
    "BaseEnricher",
    "EnrichmentContext",
    "EnrichmentResult",
    "WebsiteScraper",
    "ALL_ENRICHERS",
]
