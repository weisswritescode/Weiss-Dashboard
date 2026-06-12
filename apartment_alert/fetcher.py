"""
Craigslist SF apartment listing fetcher.

Designed as a standalone module so additional sources (Zillow, Apartments.com, etc.)
can be added later by implementing the same interface:
    fetch_listings() -> list[Listing]
    fetch_listing_detail(listing) -> Listing
"""

import re
import time
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .models import Listing
from . import config

logger = logging.getLogger(__name__)

BASE_URL = "https://sfbay.craigslist.org"
# sfc = San Francisco proper (not whole Bay Area)
SEARCH_URL = f"{BASE_URL}/search/sfc/apa"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "DNT": "1",
}


class CraigslistFetcher:
    """Fetches and parses SF Craigslist apartment listings."""

    def __init__(self, delay: float = config.REQUEST_DELAY):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def fetch_listings(
        self,
        max_price: int = config.MAX_PRICE,
        min_bedrooms: int = config.MIN_BEDROOMS,
        max_pages: int = 3,
    ) -> list[Listing]:
        """Return a flat list of Listing summaries from search results.

        Pagination: CL returns 120 results per page; we walk up to max_pages.

        Note: Craigslist blocks requests from cloud datacenter IPs (AWS, GCP, etc.)
        with 403. This works reliably from a residential IP or a residential-IP VPS.
        """
        all_listings: list[Listing] = []
        page_size = 120

        for page in range(max_pages):
            offset = page * page_size
            params = {
                "max_price": max_price,
                "min_bedrooms": min_bedrooms,
                "availabilityMode": 0,
                "s": offset,
            }
            logger.info(f"Fetching search page {page + 1} (offset {offset})")
            try:
                resp = self._get(SEARCH_URL, params=params)
                items = self._parse_search_page(resp.text)
                if not items:
                    logger.info("No more results — stopping pagination")
                    break
                all_listings.extend(items)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 403:
                    logger.error(
                        "Craigslist returned 403 Forbidden. This almost always means "
                        "the request is coming from a datacenter IP. "
                        "Run this agent from your home machine or a residential-IP VPS. "
                        "See SETUP.md §4 for scheduling options."
                    )
                else:
                    logger.error(f"HTTP error fetching page {page + 1}: {exc}")
                break
            except Exception as exc:
                logger.error(f"Unexpected error fetching page {page + 1}: {exc}")
                break

        logger.info(f"Total listings fetched: {len(all_listings)}")
        return all_listings

    def fetch_listing_detail(self, listing: Listing) -> Listing:
        """Enrich a Listing with description, photos, and map coordinates."""
        try:
            resp = self._get(listing.url)
            self._parse_detail_page(listing, resp.text)
        except Exception as exc:
            logger.error(f"Failed to fetch detail for {listing.cl_id}: {exc}")
        return listing

    # ------------------------------------------------------------------ #
    # Private — search page parsing                                        #
    # ------------------------------------------------------------------ #

    def _parse_search_page(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[Listing] = []

        # New CL design (2023+): <li class="cl-search-result" data-pid="...">
        items = soup.select("li.cl-search-result[data-pid]")
        if items:
            logger.debug(f"Using new CL search result format ({len(items)} items)")
            for item in items:
                lst = self._parse_new_item(item)
                if lst:
                    listings.append(lst)
            return listings

        # Fallback — old design: <li class="result-row" data-pid="...">
        items = soup.select("li.result-row[data-pid]")
        if items:
            logger.debug(f"Using old CL search result format ({len(items)} items)")
            for item in items:
                lst = self._parse_old_item(item)
                if lst:
                    listings.append(lst)
            return listings

        # Last-resort: find all <a> links pointing to /sfc/apa/d/ and build stubs
        links = soup.find_all("a", href=re.compile(r"/sfc/apa/d/"))
        seen_ids: set[str] = set()
        logger.debug(f"Fallback: found {len(links)} apartment links")
        for a in links:
            href = a.get("href", "")
            cl_id_match = re.search(r"/(\d{7,12})\.html", href)
            if not cl_id_match:
                continue
            cl_id = cl_id_match.group(1)
            if cl_id in seen_ids:
                continue
            seen_ids.add(cl_id)
            url = href if href.startswith("http") else BASE_URL + href
            title = a.get_text(strip=True) or f"CL listing {cl_id}"
            listings.append(
                Listing(cl_id=cl_id, title=title, price=0, location="", url=url)
            )

        return listings

    def _parse_new_item(self, item) -> Optional[Listing]:
        try:
            cl_id = item.get("data-pid", "").strip()
            if not cl_id:
                return None

            # Link + title — try several selector patterns
            anchor = (
                item.select_one("a.cl-app-anchor")
                or item.select_one("a.posting-title")
                or item.select_one("a[href*='/sfc/apa/d/']")
            )
            if not anchor:
                return None

            href = anchor.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href

            title_el = (
                item.select_one(".label")
                or item.select_one(".posting-title .title")
                or anchor
            )
            title = title_el.get_text(strip=True)

            # Price
            price_el = item.select_one(".price") or item.select_one(".priceinfo")
            price = _parse_price(price_el.get_text() if price_el else "")

            # Housing (beds / sqft)
            housing_el = item.select_one(".housing") or item.select_one(".meta .housing")
            beds, sqft = _parse_housing(housing_el.get_text() if housing_el else "")

            # Neighborhood
            hood_el = (
                item.select_one(".hood")
                or item.select_one(".result-hood")
                or item.select_one(".location")
            )
            location = _clean_location(hood_el.get_text() if hood_el else "")

            return Listing(
                cl_id=cl_id,
                title=title,
                price=price,
                location=location,
                url=url,
                bedrooms=beds,
                sqft=sqft,
            )
        except Exception as exc:
            logger.debug(f"Error parsing new item: {exc}")
            return None

    def _parse_old_item(self, item) -> Optional[Listing]:
        try:
            cl_id = item.get("data-pid", "").strip()
            if not cl_id:
                return None

            anchor = item.select_one("a.result-title") or item.select_one("a.hdrlnk")
            if not anchor:
                return None

            href = anchor.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href
            title = anchor.get_text(strip=True)

            price_el = item.select_one(".result-price")
            price = _parse_price(price_el.get_text() if price_el else "")

            housing_el = item.select_one(".result-housing")
            beds, sqft = _parse_housing(housing_el.get_text() if housing_el else "")

            hood_el = item.select_one(".result-hood")
            location = _clean_location(hood_el.get_text() if hood_el else "")

            return Listing(
                cl_id=cl_id,
                title=title,
                price=price,
                location=location,
                url=url,
                bedrooms=beds,
                sqft=sqft,
            )
        except Exception as exc:
            logger.debug(f"Error parsing old item: {exc}")
            return None

    # ------------------------------------------------------------------ #
    # Private — detail page parsing                                        #
    # ------------------------------------------------------------------ #

    def _parse_detail_page(self, listing: Listing, html: str) -> None:
        soup = BeautifulSoup(html, "lxml")

        # ---- Title (fill in if stub) ----
        if not listing.title or listing.title.startswith("CL listing"):
            t = soup.select_one("#titletextonly") or soup.select_one(
                "span.postingtitletext"
            )
            if t:
                listing.title = t.get_text(strip=True)

        # ---- Price ----
        if listing.price == 0:
            p = soup.select_one("span.price")
            if p:
                listing.price = _parse_price(p.get_text())

        # ---- Description ----
        body = soup.select_one("#postingbody")
        if body:
            for el in body.select(".print-qrcode-label, .print-qrcode-container"):
                el.decompose()
            listing.description = body.get_text(separator="\n", strip=True)

        # ---- Map coordinates ----
        map_el = soup.select_one("#map")
        if map_el:
            try:
                listing.lat = float(map_el.get("data-latitude") or 0)
                listing.lng = float(map_el.get("data-longitude") or 0)
            except (ValueError, TypeError):
                pass

        # ---- Photos ----
        photo_urls: list[str] = []

        # Method 1: #thumbs links → full-size originals
        for a in soup.select("#thumbs a[href]"):
            href = a.get("href", "")
            if "craigslist.org" in href and href not in photo_urls:
                photo_urls.append(href)

        # Method 2: gallery swipe images
        if not photo_urls:
            for img in soup.select(".swipe-image img[src], .gallery img[src]"):
                src = img.get("src", "")
                if "craigslist.org" in src:
                    # Upgrade thumbnail to 600x450 if URL contains a size token
                    src = re.sub(r"\d+x\d+[a-z]*/", "600x450/", src)
                    if src not in photo_urls:
                        photo_urls.append(src)

        # Method 3: any img pointing at images.craigslist.org
        if not photo_urls:
            for img in soup.find_all("img", src=re.compile(r"images\.craigslist\.org")):
                src = img.get("src", "")
                if src not in photo_urls:
                    photo_urls.append(src)

        listing.photo_urls = photo_urls[:10]  # keep up to 10; scorer takes first 4

        # ---- Bedrooms (from attrs section if not set) ----
        if not listing.bedrooms:
            for span in soup.select(".attrgroup span, .shared-line-bubble span"):
                m = re.match(r"(\d+)br", span.get_text(strip=True), re.IGNORECASE)
                if m:
                    listing.bedrooms = int(m.group(1))
                    break

        # ---- Posted timestamp ----
        dt_el = soup.select_one("time.timeago") or soup.select_one(
            "p.postinginfo time"
        )
        if dt_el:
            listing.posted_at = dt_el.get("datetime", "")

    # ------------------------------------------------------------------ #
    # Private — rate-limited HTTP                                          #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, **kwargs) -> requests.Response:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=20, **kwargs)
                self._last_request_time = time.monotonic()
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    wait = (attempt + 1) * 5
                    logger.warning(f"Rate-limited by CL — waiting {wait}s")
                    time.sleep(wait)
                else:
                    raise
            except requests.RequestException as exc:
                if attempt == 2:
                    raise
                wait = (attempt + 1) * 3
                logger.warning(f"Request error (attempt {attempt+1}): {exc} — retry in {wait}s")
                time.sleep(wait)

        raise RuntimeError(f"Exhausted retries for {url}")


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _parse_price(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _parse_housing(text: str) -> tuple[Optional[int], Optional[int]]:
    beds = sqft = None
    bed_m = re.search(r"(\d+)\s*br", text, re.IGNORECASE)
    if bed_m:
        beds = int(bed_m.group(1))
    sqft_m = re.search(r"([\d,]+)\s*(?:ft²|ft2|sqft|sq\s*ft)", text, re.IGNORECASE)
    if sqft_m:
        sqft = int(sqft_m.group(1).replace(",", ""))
    return beds, sqft


def _clean_location(text: str) -> str:
    return re.sub(r"[()]+", "", text).strip()
