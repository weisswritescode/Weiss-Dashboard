"""
SQLite-backed deduplication store.

Tables:
  seen_listings   — every listing we've ever encountered (prevents re-scoring)
  emailed_batches — full JSON of each emailed batch, with scores
"""

import json
import sqlite3
import logging
from typing import Optional

from rapidfuzz import fuzz, utils

from .models import Listing

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_listings (
    cl_id       TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    price       INTEGER NOT NULL,
    location    TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    emailed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS emailed_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cl_id       TEXT NOT NULL,
    score       INTEGER NOT NULL,
    data        TEXT NOT NULL,
    emailed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_seen_price ON seen_listings(price);
"""


class ListingDatabase:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def is_seen(self, cl_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_listings WHERE cl_id = ?", (cl_id,)
        ).fetchone()
        return row is not None

    def is_duplicate(self, listing: Listing, title_threshold: int = 85) -> bool:
        """Fuzzy-match title + price to catch reposts.

        Uses WRatio (best of several fuzz strategies) so it handles both
        reposts with extra words ("…still available!") and minor rewrites.
        Requires same price to avoid false positives across unrelated listings.
        """
        rows = self.conn.execute(
            "SELECT cl_id, title, location FROM seen_listings WHERE price = ?",
            (listing.price,),
        ).fetchall()

        for row in rows:
            if row["cl_id"] == listing.cl_id:
                continue
            title_score = fuzz.WRatio(
                listing.title, row["title"], processor=utils.default_process
            )
            if title_score >= title_threshold:
                logger.debug(
                    f"Fuzzy-duplicate detected (WRatio={title_score:.0f}%): "
                    f"'{listing.title[:50]}' ~ '{row['title'][:50]}'"
                )
                return True
        return False

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def mark_seen(self, listing: Listing) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_listings (cl_id, title, price, location, url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                listing.cl_id,
                listing.title,
                listing.price,
                listing.location,
                listing.url,
            ),
        )
        self.conn.commit()

    def mark_emailed(self, listing: Listing, score: int, data: dict) -> None:
        self.conn.execute(
            "UPDATE seen_listings SET emailed = 1 WHERE cl_id = ?",
            (listing.cl_id,),
        )
        self.conn.execute(
            "INSERT INTO emailed_batches (cl_id, score, data) VALUES (?, ?, ?)",
            (listing.cl_id, score, json.dumps(data, default=str)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Diagnostics                                                          #
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM seen_listings"
        ).fetchone()[0]
        emailed = self.conn.execute(
            "SELECT COUNT(*) FROM seen_listings WHERE emailed = 1"
        ).fetchone()[0]
        return {"total_seen": total, "total_emailed": emailed}

    def close(self) -> None:
        self.conn.close()
