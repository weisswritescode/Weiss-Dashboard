"""
Main pipeline: fetch → filter → deduplicate → score → email.

Supports multiple listing sources (Craigslist + Zillow). Each source is
fetched independently; listings are merged and deduped in the shared database
before filtering and scoring.
"""

import logging
from typing import Protocol

from .fetcher import CraigslistFetcher
from .filter import FilterEngine
from .scorer import ClaudeScorer
from .database import ListingDatabase
from . import emailer, config
from .models import Listing

logger = logging.getLogger(__name__)


class ListingSource(Protocol):
    """Interface that any listing source must implement."""
    source: str
    def fetch_listings(self, max_price: int, min_bedrooms: int, max_pages: int) -> list[Listing]: ...
    def fetch_listing_detail(self, listing: Listing) -> Listing: ...


def _build_sources() -> list:
    sources = []
    if config.ENABLE_CRAIGSLIST:
        sources.append(CraigslistFetcher())
        logger.info("Source enabled: Craigslist")
    if config.ENABLE_ZILLOW:
        try:
            from .zillow_fetcher import ZillowFetcher
            sources.append(ZillowFetcher())
            logger.info("Source enabled: Zillow")
        except ImportError:
            logger.warning("ZillowFetcher unavailable — skipping Zillow")
    if not sources:
        raise RuntimeError("No listing sources are enabled. Check ENABLE_CRAIGSLIST/ENABLE_ZILLOW in .env")
    return sources


def run_pipeline(
    dry_run: bool = False,
    max_pages: int = 3,
    max_to_score: int = config.MAX_LISTINGS_TO_SCORE,
) -> list[tuple[Listing, dict]]:
    """Run one full fetch-filter-score-email cycle across all enabled sources.

    Args:
        dry_run:      Fetch and filter only; no Claude calls, no email.
        max_pages:    Pages to fetch per source (CL: 120/page; Zillow: ~40/page).
        max_to_score: Cap on total Claude scoring calls per run.

    Returns:
        List of (Listing, score_dict) tuples that were emailed.
    """
    db = ListingDatabase(config.DB_PATH)
    filt = FilterEngine()
    sources = _build_sources()

    # ------------------------------------------------------------------ #
    # 1. Fetch from all sources                                            #
    # ------------------------------------------------------------------ #
    logger.info(f"Step 1: Fetching listings from {len(sources)} source(s)...")
    all_raw: list[Listing] = []
    for source in sources:
        try:
            raw = source.fetch_listings(
                max_price=config.MAX_PRICE,
                min_bedrooms=config.MIN_BEDROOMS,
                max_pages=max_pages,
            )
            logger.info(f"  {len(raw):3d} raw from {getattr(source, 'source', type(source).__name__)}")
            all_raw.extend(raw)
        except Exception as exc:
            logger.error(f"  Source {source.__class__.__name__} failed: {exc}")

    logger.info(f"  {len(all_raw)} total raw listings")

    # ------------------------------------------------------------------ #
    # 2. Dedup + quick filter                                              #
    # ------------------------------------------------------------------ #
    logger.info("Step 2: Deduplicating and quick-filtering...")
    new_unseen: list[Listing] = []
    for lst in all_raw:
        if db.is_seen(lst.cl_id):
            continue
        if db.is_duplicate(lst):
            db.mark_seen(lst)
            logger.debug(f"  Fuzzy-dup: {lst.cl_id} {lst.title[:55]}")
            continue
        if not filt.quick_filter(lst):
            db.mark_seen(lst)
            continue
        new_unseen.append(lst)

    logger.info(f"  {len(new_unseen)} pass quick filter")

    if not new_unseen:
        logger.info("No new candidates — done.")
        db.close()
        return []

    # ------------------------------------------------------------------ #
    # 3. Fetch full details (grouped by source to reuse session)          #
    # ------------------------------------------------------------------ #
    logger.info("Step 3: Fetching listing detail pages...")
    source_map = {getattr(s, "source", s.__class__.__name__): s for s in sources}

    detailed: list[Listing] = []
    for lst in new_unseen:
        db.mark_seen(lst)  # mark before detail fetch so a crash doesn't reprocess
        if not dry_run:
            src = source_map.get(getattr(lst, "source", "craigslist"))
            if src:
                src.fetch_listing_detail(lst)
        detailed.append(lst)

    # ------------------------------------------------------------------ #
    # 4. Full filter (description + coordinates)                          #
    # ------------------------------------------------------------------ #
    logger.info("Step 4: Applying full filter...")
    candidates = [lst for lst in detailed if filt.full_filter(lst)]
    logger.info(f"  {len(candidates)} pass full filter")

    if not candidates or dry_run:
        if dry_run:
            logger.info("Dry-run: skipping scoring and email")
            _print_candidates(candidates)
        db.close()
        return []

    # ------------------------------------------------------------------ #
    # 5. Score with Claude                                                 #
    # ------------------------------------------------------------------ #
    to_score = candidates[:max_to_score]
    if len(candidates) > max_to_score:
        logger.info(f"  Capping at {max_to_score} of {len(candidates)} candidates")

    logger.info(f"Step 5: Scoring {len(to_score)} listings with Claude...")
    try:
        scorer = ClaudeScorer()
    except ValueError as exc:
        logger.error(str(exc))
        db.close()
        return []

    matches: list[tuple[Listing, dict]] = []
    for i, lst in enumerate(to_score, 1):
        src_tag = getattr(lst, "source", "cl").upper()
        logger.info(f"  [{i}/{len(to_score)}] [{src_tag}] {lst.title[:55]}")
        result = scorer.score(lst)
        if result is None:
            continue
        overall = result.get("overall_score", result.get("score", 0))
        if overall >= config.MIN_SCORE_TO_EMAIL:
            matches.append((lst, result))
        else:
            logger.debug(f"  Below threshold (overall={overall}): {lst.title[:55]}")

    # Sort: overall_score first, then lighting as tiebreaker
    matches.sort(
        key=lambda x: (
            x[1].get("overall_score", x[1].get("score", 0)),
            x[1].get("lighting_score", 0),
        ),
        reverse=True,
    )
    logger.info(f"  {len(matches)} listings meet threshold")

    # ------------------------------------------------------------------ #
    # 6. Email                                                             #
    # ------------------------------------------------------------------ #
    if matches:
        if not config.GMAIL_APP_PASSWORD:
            logger.error(
                "GMAIL_APP_PASSWORD not set — cannot send email. See SETUP.md §2."
            )
        else:
            logger.info(f"Step 6: Sending email ({len(matches)} matches)...")
            emailer.send_alert_email(
                matches=matches,
                to_addr=config.GMAIL_TO,
                from_addr=config.GMAIL_FROM,
                password=config.GMAIL_APP_PASSWORD,
            )
            for lst, result in matches:
                overall = result.get("overall_score", result.get("score", 0))
                db.mark_emailed(lst, overall, result)
    else:
        logger.info("Step 6: No matches to email this run.")

    s = db.stats()
    logger.info(f"Done. DB: {s['total_seen']} seen · {s['total_emailed']} emailed")
    db.close()
    return matches


def _print_candidates(candidates: list[Listing]) -> None:
    print(f"\n{'='*65}")
    print(f"DRY RUN — {len(candidates)} candidates after filtering")
    print(f"{'='*65}")
    for lst in candidates:
        tier = (
            f"T{lst.neighborhood_tier}"
            if lst.neighborhood_tier
            else "???" if lst.neighborhood_uncertain else "EXCL"
        )
        src = getattr(lst, "source", "cl").upper()
        print(
            f"\n  [{src}] [{tier}] ${lst.price:,}/mo · {lst.bedrooms or '?'}BR · "
            f"{lst.neighborhood_name or lst.location}"
        )
        print(f"  {lst.title[:72]}")
        print(f"  {lst.url}")
        print(f"  Photos: {len(lst.photo_urls)} · Coords: {lst.lat}, {lst.lng}")
