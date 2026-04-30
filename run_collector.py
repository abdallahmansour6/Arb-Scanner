"""Collector entry point.

Test drive:
    python run_collector.py --once --print

Continuous (matches deployment cadence):
    python run_collector.py --interval 60
"""

import argparse
import asyncio
import logging
import sys
import time

from collector import collect_once
from config import POLL_INTERVAL_S
from storage import write_observations

log = logging.getLogger("scanner")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


async def _one_cycle(print_summary: bool) -> int:
    t0 = time.time()
    rows = await collect_once()
    path = write_observations(rows)
    elapsed = time.time() - t0
    log.info("cycle: %d rows in %.1fs -> %s", len(rows), elapsed, path)
    if print_summary and rows:
        from collections import Counter
        c = Counter(r["exchange"] for r in rows)
        log.info("per-venue counts: %s", dict(sorted(c.items())))
    return len(rows)


async def _continuous(interval_s: int, print_summary: bool) -> None:
    while True:
        t0 = time.time()
        try:
            await _one_cycle(print_summary)
        except Exception:
            log.exception("cycle failed; continuing")
        sleep_s = max(0.0, interval_s - (time.time() - t0))
        await asyncio.sleep(sleep_s)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit (test drive).")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL_S, help="Continuous cadence in seconds.")
    p.add_argument("--print", dest="print_summary", action="store_true", help="Print per-venue row counts.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    _setup_logging(args.verbose)

    if args.once:
        asyncio.run(_one_cycle(args.print_summary))
    else:
        asyncio.run(_continuous(args.interval, args.print_summary))


if __name__ == "__main__":
    main()
