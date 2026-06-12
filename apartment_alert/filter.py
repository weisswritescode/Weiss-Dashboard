"""
Filtering and neighborhood-detection logic.

Two-phase approach:
  quick_filter()  — runs on search-result fields only (fast, no HTTP)
  full_filter()   — runs after detail page is fetched (description + coordinates)
"""

import re
import math
import logging
from typing import Optional

from .models import Listing

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Neighborhood tiers                                                   #
# ------------------------------------------------------------------ #

TIER_1: set[str] = {
    "noe valley",
    "pacific heights",
}

TIER_2: set[str] = {
    "castro",
    "eureka valley",
    "castro/eureka valley",
    "castro / eureka valley",
    "dolores heights",
    "cole valley",
    "glen park",
    "presidio heights",
    "lower pacific heights",
    "cow hollow",
    "marina",
    "russian hill",
    "nob hill",
}

# Confirmed reject — will never email listings in these neighborhoods
EXCLUDED: set[str] = {
    "mission",
    "the mission",
    "mission district",
    "soma",
    "so ma",
    "south of market",
    "tenderloin",
    "tenderloin district",
    "the tenderloin",
    "mid-market",
    "mid market",
    "6th street",
}

# Approximate centroids (lat, lng) — used when text matching is ambiguous.
# Ordered: ALL_TIER neighborhoods first, then EXCLUDED, then others SF nhoods
# so nearest-centroid picks the right label.
_CENTROIDS: dict[str, tuple[float, float]] = {
    # Tier 1
    "noe valley":           (37.7517, -122.4312),
    "pacific heights":      (37.7925, -122.4382),
    # Tier 2
    "castro":               (37.7609, -122.4350),
    "dolores heights":      (37.7567, -122.4268),
    "cole valley":          (37.7660, -122.4490),
    "glen park":            (37.7335, -122.4339),
    "presidio heights":     (37.7897, -122.4530),
    "lower pacific heights":(37.7851, -122.4380),
    "cow hollow":           (37.7980, -122.4363),
    "marina":               (37.8030, -122.4361),
    "russian hill":         (37.8003, -122.4185),
    "nob hill":             (37.7930, -122.4146),
    # Excluded
    "mission":              (37.7600, -122.4194),
    "soma":                 (37.7785, -122.4038),
    "tenderloin":           (37.7840, -122.4128),
    # Other SF neighborhoods (used for nearest-centroid awareness)
    "haight-ashbury":       (37.7700, -122.4469),
    "hayes valley":         (37.7762, -122.4245),
    "inner richmond":       (37.7793, -122.4600),
    "outer richmond":       (37.7793, -122.4850),
    "inner sunset":         (37.7600, -122.4650),
    "outer sunset":         (37.7515, -122.4890),
    "potrero hill":         (37.7605, -122.4000),
    "bernal heights":       (37.7440, -122.4131),
    "excelsior":            (37.7236, -122.4280),
    "west portal":          (37.7398, -122.4657),
    "forest hill":          (37.7453, -122.4595),
    "twin peaks":           (37.7527, -122.4477),
    "upper market":         (37.7626, -122.4393),
    "duboce triangle":      (37.7690, -122.4340),
    "alamo square":         (37.7763, -122.4337),
    "north beach":          (37.8062, -122.4105),
    "chinatown":            (37.7941, -122.4078),
    "financial district":   (37.7946, -122.3999),
    "embarcadero":          (37.7955, -122.3937),
    "fishermans wharf":     (37.8085, -122.4185),
}


# ------------------------------------------------------------------ #
# Room-share / deal-breaker patterns                                   #
# ------------------------------------------------------------------ #

_ROOM_SHARE_RE = re.compile(
    r"\b("
    r"room\s+for\s+rent"
    r"|room\s+in\s+(?:a\s+)?(?:house|apt|apartment|flat|condo|home)"
    r"|roommate|housemate|room(?:ie)?s?\s+wanted"
    r"|shared\s+(?:house|apartment|apt|flat|home)"
    r"|looking\s+for\s+(?:a\s+)?roommate"
    r"|private\s+room\s+(?:in|for)"
    r"|furnished\s+room"
    r"|sublet\s+(?:of\s+a?\s+)?room|room\s+sublet|room\s+only"
    r"|one\s+room\s+available"
    r")\b",
    re.IGNORECASE,
)

# Shared entrance/kitchen/bath are hard deal-breakers per spec
_SHARED_SPACE_RE = re.compile(
    r"\b("
    r"shared\s+(?:entrance|entry|front\s+door|kitchen|bath(?:room)?)"
    r"|share\s+(?:the\s+)?(?:entrance|entry|kitchen|bath(?:room)?)"
    r"|common\s+(?:kitchen|bath(?:room)?|entrance|entry)"
    r"|shared\s+facilities"
    r")\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ #
# Neighborhood detection                                               #
# ------------------------------------------------------------------ #

def detect_neighborhood(
    location_str: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> tuple[str, Optional[int], bool]:
    """Return (name, tier, uncertain).

    tier = 1 (Tier 1), 2 (Tier 2), None (excluded/unknown).
    uncertain = True means we couldn't confidently classify — flag, don't reject.
    """
    normalized = location_str.lower().strip()

    # 1. Direct text match — most reliable
    for hood in TIER_1:
        if hood in normalized:
            return hood.title(), 1, False

    for hood in TIER_2:
        if hood in normalized:
            return hood.title(), 2, False

    for hood in EXCLUDED:
        if hood in normalized:
            return hood.title(), None, False  # confirmed excluded

    # 2. Coordinate-based nearest centroid (within 1 km)
    if lat and lng and abs(lat) > 0 and abs(lng) > 0:
        nearest_hood, nearest_dist = _nearest_centroid(lat, lng)
        if nearest_hood and nearest_dist < 1.0:
            hl = nearest_hood.lower()
            if hl in TIER_1:
                return nearest_hood.title(), 1, False
            elif hl in TIER_2:
                return nearest_hood.title(), 2, False
            elif hl in EXCLUDED:
                return nearest_hood.title(), None, False
            else:
                # Near a non-tiered SF neighborhood — flag as uncertain
                return nearest_hood.title(), None, True

    # 3. Nothing matched — flag uncertain rather than silently rejecting
    return location_str.strip() or "Unknown", None, True


def _nearest_centroid(lat: float, lng: float) -> tuple[Optional[str], float]:
    nearest_hood: Optional[str] = None
    nearest_dist = float("inf")
    for hood, (clat, clng) in _CENTROIDS.items():
        d = _haversine_km(lat, lng, clat, clng)
        if d < nearest_dist:
            nearest_dist = d
            nearest_hood = hood
    return nearest_hood, nearest_dist


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# ------------------------------------------------------------------ #
# Filter engine                                                        #
# ------------------------------------------------------------------ #

class FilterEngine:
    """Two-phase filter: quick (search result) then full (detail page)."""

    def quick_filter(self, listing: Listing) -> bool:
        """Return True if the listing should proceed to detail-fetching."""

        # --- Price ---
        if listing.price > 5000:
            logger.debug(f"REJECT price ${listing.price}: {listing.title[:60]}")
            return False

        # --- Bedrooms (when explicit in search result) ---
        if listing.bedrooms is not None and listing.bedrooms < 1:
            logger.debug(f"REJECT bedrooms={listing.bedrooms}: {listing.title[:60]}")
            return False

        # --- Room-share keywords in title ---
        if _ROOM_SHARE_RE.search(listing.title):
            logger.debug(f"REJECT room-share title: {listing.title[:60]}")
            return False

        # --- Neighborhood from location string ---
        hood_name, tier, uncertain = detect_neighborhood(listing.location)
        listing.neighborhood_name = hood_name
        listing.neighborhood_tier = tier
        listing.neighborhood_uncertain = uncertain

        if tier is None and not uncertain:
            logger.debug(
                f"REJECT excluded neighborhood '{hood_name}': {listing.title[:60]}"
            )
            return False

        return True

    def full_filter(self, listing: Listing) -> bool:
        """Return True if the listing is a genuine candidate for scoring.

        Called after detail page is fetched (description + coordinates available).
        """
        # --- Re-run neighborhood detection with coordinates if was uncertain ---
        if listing.neighborhood_uncertain and listing.lat and listing.lng:
            hood_name, tier, uncertain = detect_neighborhood(
                listing.location, listing.lat, listing.lng
            )
            listing.neighborhood_name = hood_name
            listing.neighborhood_tier = tier
            listing.neighborhood_uncertain = uncertain
            logger.debug(
                f"Coord-based neighborhood: '{hood_name}' tier={tier} "
                f"uncertain={uncertain} for {listing.title[:60]}"
            )

        # Reject confirmed excluded (coordinates may have clarified)
        if listing.neighborhood_tier is None and not listing.neighborhood_uncertain:
            logger.debug(
                f"REJECT excluded neighborhood (coord): {listing.title[:60]}"
            )
            return False

        # --- Room-share in description ---
        if listing.description and _ROOM_SHARE_RE.search(listing.description):
            logger.debug(f"REJECT room-share description: {listing.title[:60]}")
            return False

        # --- Shared entrance/kitchen/bathroom (spec hard deal-breakers) ---
        if listing.description and _SHARED_SPACE_RE.search(listing.description):
            logger.debug(f"REJECT shared space: {listing.title[:60]}")
            return False

        return True
