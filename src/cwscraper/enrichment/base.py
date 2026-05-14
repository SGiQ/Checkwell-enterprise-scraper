"""Base class for enrichers — code that fills in `email` / `contacts` on
already-discovered BusinessLeads.

Enrichers operate on businesses, not on the raw web. They run as a separate
pass after a directory scan (or on demand from the dashboard).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("cwscraper.enrichment")


@dataclass
class EnrichmentResult:
    """What an enricher returns for one business. Empty fields = no data."""

    email: str = ""
    contacts: list[dict] = field(default_factory=list)   # [{name, email, source_url}]
    source: str = ""                                     # which enricher produced this


@dataclass
class EnrichmentContext:
    """Run-wide state — progress reporting + error capture."""

    businesses_total: int = 0
    businesses_done: int = 0
    emails_found: int = 0
    errors: list[str] = field(default_factory=list)

    def log_error(self, business_name: str, msg: str) -> None:
        full = f"{business_name}: {msg}"
        logger.warning(full)
        self.errors.append(full)


class BaseEnricher(ABC):
    """Subclass once per enrichment source (website scrape, Hunter, Apollo)."""

    source: str = ""

    @abstractmethod
    def enrich(self, business: dict, ctx: EnrichmentContext) -> EnrichmentResult | None:
        """Return enrichment data for one business, or None if nothing applicable."""
        ...

    @property
    def name(self) -> str:
        return self.source.replace("_", " ").title() if self.source else self.__class__.__name__
