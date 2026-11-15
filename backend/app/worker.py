"""Standalone worker entry: ``python -m app.worker`` runs the job queue
outside the API server (optional; the API starts one automatically)."""
from __future__ import annotations

import logging
import time

from .config import settings
from .db import init_db
from .services.jobs import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("arcusclips.worker")


def main() -> None:
    settings.ensure_dirs()
    init_db()
    worker = Worker()
    worker.start()
    logger.info("Standalone worker running (poll interval %.1fs)", settings.job_poll_interval)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Stopping worker…")
        worker.stop()


if __name__ == "__main__":
    main()
