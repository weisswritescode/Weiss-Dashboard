"""
Main pipeline: fetch → filter → deduplicate → score → email.
"""

import logging
import sys
from typing import Optional

from .fetcher import CraigslistFetcher
from .filter import FilterEngine
from .scorer import ClaudeScorer
from .database import ListingDatabase
from . import emailer, config
from .models import Listing

logger = logging.getLogger(__name__)


def run_pipeline(
    dry_run: bool = False,
    max_pages: int = 3,
    max_to_score: int = config.MAX_LISTINGS_TO_SCORE,
) -> list[tuple[Listing, dict]]:
    """Run one full fetch-filter-score-email cycle.

    Args:
        dry_run:     Fetch and filter but do NOT call Claude or send email.
        max_pages:   Craigslist search pages to fetch (120 results each).
        max_to_score: Cap on Claude scoring calls per run (cost control).

    Returns:
        List of (Listing, score_dict) tuples that were emailed (or would be).
    """
    db = ListingDatabase(config.DB_PATH)
    fetcher = CraigslistFetcher()
    filt = FilterEngine()

    # ------------------------------------------------------------------ #
    # 1. Fetch search results                                              #
    # ------------------------------------------------------------------ #
    logger.info("Step 1: Fetching Craigslist listings...")
    raw = fetcher.fetch_listings(
        max_price=config.MAX_PRICE,
        min_bedrooms=config.MIN_BEDROOMS,
        max_pages=max_pages,
    )
    logger.info(f"  {len(raw)} raw listings fetched")

    # ------------------------------------------------------------------ #
    # 2. Dedup + quick filter (no HTTP needed)                            #
    # ------------------------------------------------------------------ #
    logger.info("Step 2: Deduplicating and quick-filtering...")
    new_unseen: list[Listing] = []
    for lst in raw:
        if db.is_seen(lst.cl_id):
            continue
        if db.is_duplicate(lst):
            db.mark_seen(lst)  # mark so we don't re-check next run
            logger.debug(f"  Fuzzy-dup skipped: {lst.title[:60]}")
            continue
        if not filt.quick_filter(lst):
            db.mark_seen(lst)  # mark rejected as seen too
            continue
        new_unseen.append(lst)

    logger.info(f"  {len(new_unseen)} pass quick filter")

    if not new_unseen:
        logger.info("No new candidates — done.")
        db.close()
        return []

    # ------------------------------------------------------------------ #
    # 3. Fetch full details for candidates                                 #
    # ------------------------------------------------------------------ #
    logger.info("Step 3: Fetching listing detail pages...")
    detailed: list[Listing] = []
    for lst in new_unseen:
        db.mark_seen(lst)  # mark seen before detail fetch so crashes don't re-process
        if not dry_run:
            fetcher.fetch_listing_detail(lst)
        detailed.append(lst)

    # ------------------------------------------------------------------ #
    # 4. Full filter (description + coordinates)                          #
    # ------------------------------------------------------------------ #
    logger.info("Step 4: Applying full filter...")
    candidates = [lst for lst in detailed if filt.full_filter(lst)]
    logger.info(f"  {len(candidates)} pass full filter")

    if not candidates or dry_run:
        if dry_run:
            logger.info("Dry-run mode — skipping scoring and email")
            _print_candidates(candidates)
        db.close()
        return []

    # ------------------------------------------------------------------ #
    # 5. Score with Claude                                                 #
    # ------------------------------------------------------------------ #
    # Score the most recent listings first (they posted at the top of CL)
    to_score = candidates[:max_to_score]
    if len(candidates) > max_to_score:
        logger.info(
            f"  Capping scoring at {max_to_score} of {len(candidates)} candidates"
        )

    logger.info(f"Step 5: Scoring {len(to_score)} listings with Claude...")
    try:
        scorer = ClaudeScorer()
    except ValueError as exc:
        logger.error(str(exc))
        db.close()
        return []

    matches: list[tuple[Listing, dict]] = []
    for i, lst in enumerate(to_score, 1):
        logger.info(f"  [{i}/{len(to_score)}] Scoring: {lst.title[:60]}")
        result = scorer.score(lst)
        if result is None:
            continue
        if result["score"] >= config.MIN_SCORE_TO_EMAIL:
            matches.append((lst, result))
        else:
            logger.debug(
                f"  Below threshold (score={result['score']}): {lst.title[:60]}"
            )

    # Sort highest score first
    matches.sort(key=lambda x: (x[1]["score"], x[1]["lighting_score"]), reverse=True)
    logger.info(f"  {len(matches)} listings meet score threshold")

    # ------------------------------------------------------------------ #
    # 6. Email                                                             #
    # ------------------------------------------------------------------ #
    if matches:
        if not config.GMAIL_APP_PASSWORD:
            logger.error(
                "GMAIL_APP_PASSWORD not set — cannot send email. "
                "See SETUP.md for instructions."
            )
        else:
            logger.info(f"Step 6: Sending email with {len(matches)} matches...")
            emailer.send_alert_email(
                matches=matches,
                to_addr=config.GMAIL_TO,
                from_addr=config.GMAIL_FROM,
                password=config.GMAIL_APP_PASSWORD,
            )
            for lst, result in matches:
                db.mark_emailed(lst, result["score"], result)
    else:
        logger.info("Step 6: No matches to email this run.")

    stats = db.stats()
    logger.info(
        f"Done. DB: {stats['total_seen']} total seen, {stats['total_emailed']} emailed."
    )
    db.close()
    return matches


def _print_candidates(candidates: list[Listing]) -> None:
    """Pretty-print candidates in dry-run mode."""
    print(f"\n{'='*60}")
    print(f"DRY RUN — {len(candidates)} candidates after filtering")
    print(f"{'='*60}")
    for lst in candidates:
        tier = f"T{lst.neighborhood_tier}" if lst.neighborhood_tier else ("???" if lst.neighborhood_uncertain else "EXCL")
        print(
            f"\n  [{tier}] ${lst.price:,}/mo · {lst.bedrooms or '?'}BR · "
            f"{lst.neighborhood_name or lst.location}"
        )
        print(f"  {lst.title[:70]}")
        print(f"  {lst.url}")
        print(f"  Photos: {len(lst.photo_urls)} · Coords: {lst.lat}, {lst.lng}")
