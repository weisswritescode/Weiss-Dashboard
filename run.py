#!/usr/bin/env python3
"""
Entry point for the SF Apartment Alert agent.

Usage:
  python run.py                   Run full pipeline (fetch, filter, score, email)
  python run.py --dry-run         Fetch + filter only; no Claude, no email
  python run.py --pages N         Fetch N pages of results (default: 3, 120/page)
  python run.py --reset-db        Delete the seen-listings database and start fresh
  python run.py --stats           Show database statistics and exit
"""

import argparse
import logging
import os
import sys


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Quiet noisy libraries
    for lib in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="SF Craigslist Apartment Alert Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and filter only; do not score or email")
    parser.add_argument("--pages", type=int, default=3, metavar="N",
                        help="Number of CL search pages to fetch (default: 3)")
    parser.add_argument("--max-score", type=int, default=25, metavar="N",
                        help="Max listings to score with Claude per run (default: 25)")
    parser.add_argument("--reset-db", action="store_true",
                        help="Delete the SQLite database and start fresh")
    parser.add_argument("--stats", action="store_true",
                        help="Print database statistics and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    # Lazy import so logging is configured first
    from apartment_alert import config
    from apartment_alert.database import ListingDatabase
    from apartment_alert.main import run_pipeline

    if args.reset_db:
        if os.path.exists(config.DB_PATH):
            os.remove(config.DB_PATH)
            print(f"Deleted {config.DB_PATH}")
        else:
            print(f"No database at {config.DB_PATH}")
        return

    if args.stats:
        db = ListingDatabase(config.DB_PATH)
        s = db.stats()
        db.close()
        print(f"Database: {config.DB_PATH}")
        print(f"  Total seen:    {s['total_seen']}")
        print(f"  Total emailed: {s['total_emailed']}")
        return

    matches = run_pipeline(
        dry_run=args.dry_run,
        max_pages=args.pages,
        max_to_score=args.max_score,
    )

    if args.dry_run:
        return

    if matches:
        print(f"\n✓ Emailed {len(matches)} matches to {config.GMAIL_TO}")
    else:
        print("\n✓ No new matches this run.")


if __name__ == "__main__":
    main()
