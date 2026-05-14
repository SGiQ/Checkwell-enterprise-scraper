"""Background auto-scan loop."""
from __future__ import annotations

import logging
import threading

from cwscraper.core.engine import ScanEngine
from cwscraper.core.store import Repository

logger = logging.getLogger("cwscraper.scheduler")


class AutoScanner:
    def __init__(self, engine: ScanEngine, repo: Repository):
        self.engine = engine
        self.repo = repo
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Auto-scan thread started")

    def stop(self) -> None:
        self._stop.set()
        logger.info("Auto-scan thread stop signal sent")

    def _loop(self) -> None:
        while not self._stop.is_set():
            config = self.repo.get_config()
            if not config.get("auto_scan_enabled"):
                self._stop.wait(60)
                continue

            interval = config.get("scan_interval_hours", 6) * 3600
            logger.info("Auto-scan triggered")
            try:
                self.engine.run_full_scan()
            except Exception as e:
                logger.error("Auto-scan failed: %s", e)

            self._stop.wait(interval)
