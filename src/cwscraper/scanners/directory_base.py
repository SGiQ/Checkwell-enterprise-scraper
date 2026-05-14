"""Base class for directory-mode scanners (Google Places, Yelp, etc.).

Different output type than community BaseScanner — directory scanners
return BusinessLead, not Lead. Engine routing in core/engine.py dispatches
based on niche.mode.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cwscraper.core.models import BusinessLead
from cwscraper.core.niche import NichePack

logger = logging.getLogger("cwscraper.directory_scanner")


@dataclass
class DirectoryContext:
    errors: list[str] = field(default_factory=list)
    queries_run: int = 0

    def log_error(self, source: str, msg: str) -> None:
        full = f"{source}: {msg}"
        logger.error(full)
        self.errors.append(full)


class BaseDirectoryScanner(ABC):
    """One subclass per discovery source (Google Places, Yelp, ...)."""

    source: str = ""

    def __init__(self, niche: NichePack):
        self.niche = niche

    @abstractmethod
    def scan(self, ctx: DirectoryContext) -> list[BusinessLead]:
        """Return list of discovered businesses."""
        ...

    @property
    def name(self) -> str:
        return self.source.title().replace("_", " ") if self.source else self.__class__.__name__
