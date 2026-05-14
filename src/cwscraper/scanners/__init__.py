from cwscraper.scanners.base import BaseScanner, ScannerContext
from cwscraper.scanners.directory_base import BaseDirectoryScanner, DirectoryContext
from cwscraper.scanners.google_places import GooglePlacesScanner
from cwscraper.scanners.hackernews import HackerNewsScanner
from cwscraper.scanners.reddit import RedditScanner
from cwscraper.scanners.youtube import YouTubeScanner

# Community-mode scanners: act on community posts/comments.
ALL_SCANNERS: dict[str, type[BaseScanner]] = {
    "reddit": RedditScanner,
    "youtube": YouTubeScanner,
    "hackernews": HackerNewsScanner,
}

# Directory-mode scanners: discover businesses in a geography.
ALL_DIRECTORY_SCANNERS: dict[str, type[BaseDirectoryScanner]] = {
    "google_places": GooglePlacesScanner,
}

__all__ = [
    "BaseScanner",
    "ScannerContext",
    "BaseDirectoryScanner",
    "DirectoryContext",
    "RedditScanner",
    "YouTubeScanner",
    "HackerNewsScanner",
    "GooglePlacesScanner",
    "ALL_SCANNERS",
    "ALL_DIRECTORY_SCANNERS",
]
