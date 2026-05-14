from cwscraper.scanners.base import BaseScanner, ScannerContext
from cwscraper.scanners.hackernews import HackerNewsScanner
from cwscraper.scanners.reddit import RedditScanner
from cwscraper.scanners.youtube import YouTubeScanner

ALL_SCANNERS: dict[str, type[BaseScanner]] = {
    "reddit": RedditScanner,
    "youtube": YouTubeScanner,
    "hackernews": HackerNewsScanner,
}

__all__ = [
    "BaseScanner",
    "ScannerContext",
    "RedditScanner",
    "YouTubeScanner",
    "HackerNewsScanner",
    "ALL_SCANNERS",
]
