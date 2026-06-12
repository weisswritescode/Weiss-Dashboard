"""
Zillow SF apartment listing fetcher.

Uses the __NEXT_DATA__ JSON blob that Zillow embeds in every search-results
page — no undocumented API calls needed.  The JSON path changes occasionally;
the parser tries multiple known paths and degrades gracefully to an HTML
link-extraction fallback if all else fails.

Listing IDs are prefixed with "zl_" so they don't collide with CL IDs in
the shared SQLite database.
"""

import json
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .models import Listing
from . import config

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.zillow.com/san-francisco-ca/rentals/"
DETAIL_BASE = "https://www.zillow.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Upgrade-Insecure-Requests": "1",
}

# Known JSON paths where Zillow stores listing arrays in __NEXT_DATA__
_LISTING_PATHS = [
    ["props", "pageProps", "searchPageState", "cat1", "searchResults", "listResults"],
    ["props", "pageProps", "searchPageState", "cat1", "searchResults", "mapResults"],
    ["props", "pageProps", "gdpClientCache"],
    ["props", "pageProps", "initialData", "cat1", "searchResults", "listResults"],
]


class ZillowFetcher:
    """Fetches SF apartment listings from Zillow.

    Note: Zillow aggressively blocks datacenter IPs; this works reliably
    from a residential machine or residential-IP VPS.
    """

    def __init__(self, delay: float = config.REQUEST_DELAY):
        self.delay = delay
        self.source = "zillow"
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self._last_request: float = 0.0

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def fetch_listings(
        self,
        max_price: int = config.MAX_PRICE,
        min_bedrooms: int = config.MIN_BEDROOMS,
        max_pages: int = 2,
    ) -> list[Listing]:
        """Return listing summaries from Zillow SF rentals search."""
        all_listings: list[Listing] = []

        # Prime cookies by loading homepage first
        try:
            self._get("https://www.zillow.com/")
        except Exception:
            pass

        for page in range(1, max_pages + 1):
            params = {
                "min-bedrooms": min_bedrooms,
                "max-price": max_price,
                "p": page,
            }
            logger.info(f"[Zillow] Fetching page {page}")
            try:
                resp = self._get(SEARCH_URL, params=params)
                items = self._parse_search_page(resp.text)
                if not items:
                    logger.info("[Zillow] No results on this page — stopping")
                    break
                all_listings.extend(items)
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response else "?"
                if code == 403:
                    logger.error(
                        "[Zillow] 403 Forbidden — Zillow is blocking this IP. "
                        "This is normal from datacenter IPs. Run from a residential "
                        "machine or residential-IP VPS. See SETUP.md §4."
                    )
                else:
                    logger.error(f"[Zillow] HTTP {code} on page {page}: {exc}")
                break
            except Exception as exc:
                logger.error(f"[Zillow] Error on page {page}: {exc}")
                break

        logger.info(f"[Zillow] Total listings fetched: {len(all_listings)}")
        return all_listings

    def fetch_listing_detail(self, listing: Listing) -> Listing:
        """Enrich a Zillow listing with full description and more photos."""
        if not listing.url:
            return listing
        try:
            resp = self._get(listing.url)
            self._enrich_from_detail(listing, resp.text)
        except Exception as exc:
            logger.debug(f"[Zillow] Could not fetch detail for {listing.cl_id}: {exc}")
        return listing

    # ------------------------------------------------------------------ #
    # Private — search page                                                #
    # ------------------------------------------------------------------ #

    def _parse_search_page(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")

        # Try __NEXT_DATA__ first (most complete data)
        next_data = self._extract_next_data(soup)
        if next_data:
            listings = self._parse_next_data(next_data)
            if listings:
                return listings

        # Fallback: scan for JSON-LD or article cards
        listings = self._parse_html_cards(soup)
        if listings:
            return listings

        logger.warning("[Zillow] Could not extract any listings from search page")
        return []

    def _extract_next_data(self, soup: BeautifulSoup) -> Optional[dict]:
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return None
        try:
            return json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            return None

    def _parse_next_data(self, data: dict) -> list[Listing]:
        for path in _LISTING_PATHS:
            node = data
            try:
                for key in path:
                    node = node[key]
                if isinstance(node, list) and node:
                    results = [self._map_zillow_item(item) for item in node]
                    good = [r for r in results if r is not None]
                    if good:
                        logger.debug(f"[Zillow] Found {len(good)} listings via path {path}")
                        return good
            except (KeyError, TypeError, IndexError):
                continue

        # Fallback: search the entire JSON string for listing arrays
        return self._parse_embedded_json(data)

    def _parse_embedded_json(self, data: dict) -> list[Listing]:
        """Walk the whole __NEXT_DATA__ tree looking for objects with zpid."""
        found: list[Listing] = []
        seen_ids: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                if "zpid" in node and "price" in node and node.get("zpid"):
                    lst = self._map_zillow_item(node)
                    if lst and lst.cl_id not in seen_ids:
                        seen_ids.add(lst.cl_id)
                        found.append(lst)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        if found:
            logger.debug(f"[Zillow] Deep scan found {len(found)} listings")
        return found

    def _map_zillow_item(self, item: dict) -> Optional[Listing]:
        """Map a Zillow listing dict to our Listing model."""
        try:
            zpid = str(item.get("zpid") or item.get("id") or "")
            if not zpid:
                return None
            cl_id = f"zl_{zpid}"

            # URL
            detail_path = (
                item.get("detailUrl")
                or item.get("hdpUrl")
                or f"/homedetails/{zpid}_zpid/"
            )
            url = detail_path if detail_path.startswith("http") else DETAIL_BASE + detail_path

            # Title — build from address
            street = item.get("streetAddress") or item.get("address") or ""
            unit  = item.get("unit") or ""
            title = f"{street}{' ' + unit if unit else ''}".strip() or f"Zillow {zpid}"

            # Price — handle "$X,XXX/mo" strings or raw int
            raw_price = item.get("price") or item.get("priceForHDP") or item.get("unformattedPrice") or 0
            price = _parse_price(str(raw_price))

            # Beds/sqft
            beds = item.get("beds") or item.get("bedrooms")
            sqft = item.get("area") or item.get("livingArea")

            # Neighbourhood / location
            location = (
                item.get("neighborhood")
                or item.get("addressNeighborhood")
                or item.get("addressCity")
                or ""
            )

            # Coordinates
            lat = lng = None
            ll = item.get("latLong") or item.get("hdpData", {}).get("homeInfo", {}).get("latLong")
            if ll:
                lat = ll.get("latitude") or ll.get("lat")
                lng = ll.get("longitude") or ll.get("lng")

            # Photos — prefer higher-res thumbnail
            photos: list[str] = []
            img_src = item.get("imgSrc") or item.get("miniCardPhotos", [{}])[0].get("url", "")
            if img_src:
                photos.append(img_src)
            for p in item.get("carouselPhotos") or item.get("photos") or []:
                src = p.get("url") or p.get("src") or ""
                if src and src not in photos:
                    photos.append(src)

            # Description (sometimes in hdpData)
            desc = (
                item.get("description")
                or (item.get("hdpData") or {}).get("homeInfo", {}).get("description")
                or ""
            )

            # Posted date
            posted_at = str(item.get("listedDate") or item.get("dateSold") or "")

            lst = Listing(
                cl_id=cl_id,
                title=title,
                price=price,
                location=location,
                url=url,
                lat=float(lat) if lat else None,
                lng=float(lng) if lng else None,
                bedrooms=int(beds) if beds else None,
                sqft=int(sqft) if sqft else None,
                description=desc,
                photo_urls=photos[:10],
                posted_at=posted_at,
            )
            lst.source = "zillow"
            return lst

        except Exception as exc:
            logger.debug(f"[Zillow] Error mapping item: {exc}")
            return None

    def _parse_html_cards(self, soup: BeautifulSoup) -> list[Listing]:
        """Last-resort: find any Zillow listing links in the HTML."""
        listings: list[Listing] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=re.compile(r"/homedetails/")):
            href = a.get("href", "")
            zpid_m = re.search(r"(\d{7,12})_zpid", href)
            if not zpid_m:
                continue
            zpid = zpid_m.group(1)
            if zpid in seen:
                continue
            seen.add(zpid)
            url = href if href.startswith("http") else DETAIL_BASE + href
            title = a.get_text(strip=True) or f"Zillow {zpid}"
            lst = Listing(
                cl_id=f"zl_{zpid}",
                title=title,
                price=0,
                location="San Francisco",
                url=url,
            )
            lst.source = "zillow"
            listings.append(lst)
        return listings

    # ------------------------------------------------------------------ #
    # Private — detail page                                                #
    # ------------------------------------------------------------------ #

    def _enrich_from_detail(self, listing: Listing, html: str) -> None:
        soup = BeautifulSoup(html, "lxml")
        data = self._extract_next_data(soup)
        if not data:
            return

        # Navigate to the property info — various known paths
        hdp = (
            _get_nested(data, "props", "pageProps", "componentProps", "gdpClientCache")
            or _get_nested(data, "props", "pageProps", "initialData")
        )

        if isinstance(hdp, dict):
            # gdpClientCache is keyed by "{zpid}_zpid"
            for v in hdp.values():
                if isinstance(v, dict) and "property" in v:
                    prop = v["property"]
                    self._apply_property_fields(listing, prop)
                    return

        # Try the top-level propertyDetails
        prop = _get_nested(data, "props", "pageProps", "propertyDetails")
        if prop:
            self._apply_property_fields(listing, prop)

    def _apply_property_fields(self, listing: Listing, prop: dict) -> None:
        if not listing.description:
            listing.description = prop.get("description") or ""

        # More photos
        for photo in prop.get("photos") or prop.get("carouselPhotos") or []:
            src = photo.get("url") or photo.get("src") or ""
            if src and src not in listing.photo_urls:
                listing.photo_urls.append(src)

        listing.photo_urls = listing.photo_urls[:10]

        if not listing.bedrooms:
            listing.bedrooms = prop.get("bedrooms") or prop.get("beds")
        if not listing.sqft:
            listing.sqft = prop.get("livingArea") or prop.get("area")
        if not listing.lat and prop.get("latitude"):
            listing.lat = float(prop["latitude"])
            listing.lng = float(prop.get("longitude", 0))

    # ------------------------------------------------------------------ #
    # Private — HTTP                                                       #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, **kwargs) -> requests.Response:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=20, **kwargs)
                self._last_request = time.monotonic()
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response else 0
                if code in (403, 429):
                    wait = (attempt + 1) * 8
                    logger.warning(f"[Zillow] HTTP {code} — waiting {wait}s")
                    time.sleep(wait)
                    if code == 403 and attempt == 2:
                        raise
                else:
                    raise
            except requests.RequestException as exc:
                if attempt == 2:
                    raise
                time.sleep((attempt + 1) * 3)
        raise RuntimeError(f"Exhausted retries for {url}")


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _parse_price(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _get_nested(d: dict, *keys):
    node = d
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node
