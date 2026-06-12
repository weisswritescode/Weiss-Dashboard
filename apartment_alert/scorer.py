"""
Claude-based listing scorer.

Downloads up to MAX_PHOTOS_PER_LISTING photos, sends them base64-encoded
alongside the listing text to claude-sonnet-4-6, and returns a structured
score dict with six rated dimensions plus an overall score.
"""

import re
import json
import base64
import logging
from typing import Optional

import httpx
import anthropic

from .models import Listing
from . import config

logger = logging.getLogger(__name__)

# Six dimensions — rated 1-10 independently, then rolled into overall_score
_SCORE_SCHEMA = """\
{
  "match": <true or false>,
  "overall_score": <integer 1-10>,
  "value_score": <integer 1-10>,
  "location_score": <integer 1-10>,
  "room_quality_score": <integer 1-10>,
  "cleanliness_score": <integer 1-10>,
  "style_score": <integer 1-10>,
  "lighting_score": <integer 1-10>,
  "privacy_notes": "<brief note about private-space signals>",
  "reasoning": "<one sentence overall summary>"
}"""

_SCORING_PROMPT = """\
You are a discerning San Francisco apartment evaluator. Score the listing below \
across six dimensions, then give a weighted overall score. Base your visual \
assessments (lighting, cleanliness, style, room quality) strictly on the PHOTOS \
provided. Where photos are absent, note it and score conservatively.

Listing details
---------------
Title:         {title}
Source:        {source}
Price:         ${price}/mo
Neighborhood:  {neighborhood} {tier_label}
Bedrooms:      {bedrooms}
Sq ft:         {sqft}
Description:
{description}

Dimension rubrics
-----------------
VALUE (1-10) — price relative to what you're getting:
  9-10  Exceptional deal for SF — well under market for this neighborhood/size/quality
  7-8   Fair price; good value
  5-6   Market rate
  3-4   Above market; needs compensating factors
  1-2   Significantly overpriced

LOCATION (1-10) — neighborhood desirability:
  9-10  Tier 1 (Noe Valley / Pacific Heights) + good street / walkability signals
  7-8   Tier 2 (Castro, Marina, Russian Hill, Nob Hill, Cow Hollow, Glen Park,
         Presidio Heights, Lower Pacific Heights, Cole Valley, Dolores Heights)
  5-6   Acceptable but outside preferred tiers
  3-4   Marginal or uncertain area
  1-2   Undesirable or excluded (Mission / SoMa / Tenderloin)

ROOM QUALITY (1-10) — layout, size, condition, ceilings, storage (from photos + desc):
  9-10  Spacious, great layout, high ceilings, excellent condition
  7-8   Good size and condition with minor issues
  5-6   Average; functional but nothing special
  3-4   Small, awkward layout, or noticeably dated
  1-2   Poor condition, very cramped, or significant structural issues

CLEANLINESS (1-10) — how clean and well-maintained the space looks in photos:
  9-10  Spotless; clearly well-maintained
  7-8   Clean with minor signs of wear
  5-6   Acceptable; average cleanliness
  3-4   Visibly dirty, stained, or neglected
  1-2   Filthy or in severe disrepair

STYLE (1-10) — aesthetic appeal and design quality from photos:
  9-10  Beautiful, cohesive design; modern or tasteful finishes; excellent character
  7-8   Attractive; nice finishes or interesting architectural details
  5-6   Neutral/plain; inoffensive but unremarkable
  3-4   Dated, mismatched, or poorly designed
  1-2   Very unappealing aesthetics

LIGHTING (1-10) — NATURAL LIGHT judged strictly from photos:
  9-10  Exceptional — large windows, direct sun, bright and airy
  7-8   Good natural light; multiple windows
  5-6   Moderate light; some windows but not standout
  3-4   Limited light; small or few windows
  1-2   Very dark; basement-level or no visible natural light

OVERALL SCORE — weighted composite (lighting and location weighted heavier):
  weight: location 25%, lighting 20%, room_quality 20%, value 15%, cleanliness 10%, style 10%
  Round to nearest integer.

Respond with ONLY valid JSON matching this exact schema — no markdown, no code fences, \
no prose outside the JSON:
{schema}"""


class ClaudeScorer:
    """Scores listings using Claude vision API across six dimensions."""

    # Dimension keys returned in the score dict — order determines display order
    DIMENSION_KEYS = [
        "overall_score",
        "value_score",
        "location_score",
        "room_quality_score",
        "cleanliness_score",
        "style_score",
        "lighting_score",
    ]
    REQUIRED_KEYS = {"match", "privacy_notes", "reasoning"} | set(DIMENSION_KEYS)

    def __init__(self):
        if not config.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file — see SETUP.md."
            )
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._http = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; apartment-alert/1.0)"},
        )

    def score(self, listing: Listing) -> Optional[dict]:
        """Return a score dict with all six dimension scores, or None on failure."""
        content = self._build_content(listing)
        try:
            response = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                messages=[{"role": "user", "content": content}],
            )
            raw = response.content[0].text.strip()
            return self._parse_response(raw, listing)
        except anthropic.APIError as exc:
            logger.error(f"Anthropic API error for {listing.cl_id}: {exc}")
        except Exception as exc:
            logger.error(f"Scoring failed for {listing.cl_id}: {exc}")
        return None

    # ---------------------------------------------------------------- #

    def _build_content(self, listing: Listing) -> list[dict]:
        content: list[dict] = []

        photo_count = 0
        for url in listing.photo_urls[: config.MAX_PHOTOS_PER_LISTING]:
            img_block = self._fetch_image_block(url)
            if img_block:
                content.append(img_block)
                photo_count += 1

        if photo_count == 0:
            logger.debug(f"No photos for {listing.cl_id} — scoring text only")

        tier_label = (
            f"[TIER {listing.neighborhood_tier}]"
            if listing.neighborhood_tier
            else "[FLAGGED UNCERTAIN — not in confirmed tier]"
            if listing.neighborhood_uncertain
            else "[EXCLUDED NEIGHBORHOOD]"
        )

        source_label = getattr(listing, "source", "craigslist").title()

        prompt = _SCORING_PROMPT.format(
            title=listing.title,
            source=source_label,
            price=listing.price,
            neighborhood=listing.neighborhood_name or listing.location,
            tier_label=tier_label,
            bedrooms=listing.bedrooms or "unknown",
            sqft=f"{listing.sqft:,}" if listing.sqft else "unknown",
            description=(listing.description or "No description available")[:3000],
            schema=_SCORE_SCHEMA,
        )
        content.append({"type": "text", "text": prompt})
        return content

    def _fetch_image_block(self, url: str) -> Optional[dict]:
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                media_type = "image/jpeg"
            b64 = base64.standard_b64encode(resp.content).decode()
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            }
        except Exception as exc:
            logger.debug(f"Could not download photo {url}: {exc}")
            return None

    def _parse_response(self, raw: str, listing: Listing) -> Optional[dict]:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            data = json.loads(cleaned)
            missing = self.REQUIRED_KEYS - set(data.keys())
            if missing:
                logger.warning(f"Score response missing keys {missing} for {listing.cl_id}")
                return None
            data["match"] = bool(data["match"])
            for key in self.DIMENSION_KEYS:
                data[key] = max(1, min(10, int(data[key])))
            return data
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.error(
                f"Failed to parse score JSON for {listing.cl_id}: {exc}\nRaw: {raw[:300]}"
            )
            return None
