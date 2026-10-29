"""Daily housekeeping: storage pruning, periodic reports, probe history cleanup.

Runs all tasks once and exits, or - with --loop - repeats every 24 hours.
run.sh starts it in loop mode alongside the sendalerts worker, mirroring the
cron jobs the upstream project expects to be scheduled:

    prunetokenbucket   - drop stale rate-limiting counters
    pruneobjects       - drop orphaned ping body objects
    sendreports        - send due weekly/monthly report emails
    prune_probe_metrics - drop probe history older than the retention period
"""

from __future__ import annotations

import logging
import signal
import time
from argparse import ArgumentParser
from types import FrameType
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand

from hc.api import probes

logger = logging.getLogger("hc")


class Command(BaseCommand):
    help = "Runs housekeeping tasks (pruning, periodic reports)"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.shutdown = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Repeat the tasks every 24 hours instead of running once",
        )

    def on_signal(self, signum: int, frame: FrameType | None) -> None:
        desc = signal.Signals(signum).name
        self.stdout.write(f"housekeeping was sent {desc}!\n")
        self.shutdown = True

    def run_once(self) -> None:
        for name in ("prunetokenbucket", "pruneobjects", "sendreports"):
            try:
                call_command(name)
            except Exception:
                logger.exception("Housekeeping task %s failed", name)

        try:
            deleted = probes.prune_probe_metrics()
            self.stdout.write(f"Pruned {deleted} old probe history row(s).")
        except Exception:
            logger.exception("prune_probe_metrics failed")

        self.stdout.write("Housekeeping pass done.")

    def handle(self, loop: bool, **options: Any) -> str:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)

        self.stdout.write("housekeeping is now running\n")
        while not self.shutdown:
            self.run_once()
            if not loop:
                break
            # Sleep in 1-minute chunks so shutdown signals act promptly:
            for _ in range(24 * 60):
                if self.shutdown:
                    break
                time.sleep(60)

        return "housekeeping stopped"
