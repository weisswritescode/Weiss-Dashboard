"""
Claude-based listing scorer.

Downloads up to MAX_PHOTOS_PER_LISTING photos, sends them base64-encoded
alongside the listing text to claude-sonnet-4-6, and returns a structured
score dict.
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

_SCORE_SCHEMA = """\
{
  "match": <true or false>,
  "score": <integer 1-10>,
  "lighting_score": <integer 1-10>,
  "privacy_notes": "<brief note about private-space signals>",
  "reasoning": "<one sentence summary>"
}"""

_SCORING_PROMPT = """\
You are evaluating a San Francisco apartment listing for a renter with these priorities \
(in order): natural light, privacy / having their own complete space, and a desirable \
neighborhood (Tier 1 = Noe Valley / Pacific Heights; Tier 2 = Castro, Marina, Russian \
Hill, Nob Hill, Cow Hollow, Glen Park, Presidio Heights, Lower Pacific Heights, Cole \
Valley, Dolores Heights).

Listing details
---------------
Title:         {title}
Price:         ${price}/mo
Neighborhood:  {neighborhood} {tier_label}
Bedrooms:      {bedrooms}
Description:
{description}

Instructions
------------
- Assess natural light FROM THE PHOTOS: window size, number of windows, brightness, \
sun exposure, south/west facing indicators.
- Note any privacy concerns: live-in landlord, shared entrances not yet flagged, ADU \
setup, basement unit, etc.
- Apply a small score bonus for Tier 1 neighborhoods.

Respond with ONLY valid JSON matching this exact schema — no markdown, no prose outside \
the JSON:
{schema}

Score rubric:
9-10  Exceptional (great light, Tier 1, no concerns)
7-8   Strong (good light, solid neighborhood)
5-6   Average (some concerns)
3-4   Below average (poor light OR significant privacy issue)
1-2   Not recommended"""


class ClaudeScorer:
    """Scores listings using Claude vision API."""

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
        """Return a score dict or None on failure."""
        content = self._build_content(listing)
        try:
            response = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
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

        # Attach up to MAX_PHOTOS_PER_LISTING photos
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
            else "[NOT IN TIER — FLAGGED UNCERTAIN]"
            if listing.neighborhood_uncertain
            else "[EXCLUDED NEIGHBORHOOD]"
        )

        prompt = _SCORING_PROMPT.format(
            title=listing.title,
            price=listing.price,
            neighborhood=listing.neighborhood_name or listing.location,
            tier_label=tier_label,
            bedrooms=listing.bedrooms or "unknown",
            description=(listing.description or "No description")[:3000],
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
        # Strip markdown code fences if model wrapped them anyway
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()
        try:
            data = json.loads(cleaned)
            # Validate required keys
            required = {"match", "score", "lighting_score", "privacy_notes", "reasoning"}
            missing = required - set(data.keys())
            if missing:
                logger.warning(
                    f"Score response missing keys {missing} for {listing.cl_id}"
                )
                return None
            # Coerce types defensively
            data["match"] = bool(data["match"])
            data["score"] = max(1, min(10, int(data["score"])))
            data["lighting_score"] = max(1, min(10, int(data["lighting_score"])))
            return data
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.error(
                f"Failed to parse score JSON for {listing.cl_id}: {exc}\nRaw: {raw[:300]}"
            )
            return None
