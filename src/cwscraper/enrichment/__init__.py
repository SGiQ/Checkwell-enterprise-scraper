from cwscraper.enrichment.base import BaseEnricher, EnrichmentContext, EnrichmentResult
from cwscraper.enrichment.playwright_scraper import PlaywrightScraper
from cwscraper.enrichment.website_scraper import WebsiteScraper

ALL_ENRICHERS: dict[str, type[BaseEnricher]] = {
    "website": WebsiteScraper,
    "playwright": PlaywrightScraper,
}

__all__ = [
    "BaseEnricher",
    "EnrichmentContext",
    "EnrichmentResult",
    "WebsiteScraper",
    "PlaywrightScraper",
    "ALL_ENRICHERS",
]
