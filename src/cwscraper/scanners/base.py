from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cwscraper.core.models import Lead
from cwscraper.core.niche import NichePack

logger = logging.getLogger("cwscraper.scanner")


@dataclass
class ScannerContext:
    """Per-scan state shared with progress reporters."""

    seen_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def log_error(self, platform: str, msg: str) -> None:
        full = f"{platform}: {msg}"
        logger.error(full)
        self.errors.append(full)


class BaseScanner(ABC):
    """Subclass once per platform.

    Scanners are constructed with the active NichePack so keywords,
    sources, and queries all flow from one config.
    """

    platform: str = ""

    def __init__(self, niche: NichePack):
        self.niche = niche

    @abstractmethod
    def scan(self, ctx: ScannerContext) -> tuple[list[Lead], dict[str, float]]:
        """Return (new_leads, new_seen_ids)."""
        ...

    @property
    def name(self) -> str:
        return self.platform.title() if self.platform else self.__class__.__name__
