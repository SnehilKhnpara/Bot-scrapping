"""HTML parsing utilities for extracting listing data.

Uses BeautifulSoup for robust HTML parsing after Playwright captures the page.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag
import structlog

from ..utils.helpers import parse_price

logger = structlog.get_logger(__name__)


@dataclass
class ListingData:
    """Raw listing data extracted from HTML."""

    item_id: str
    title: str
    url: str
    current_price: Optional[float] = None
    starting_price: Optional[float] = None
    buy_now_price: Optional[float] = None
    currency: str = "USD"
    bid_count: int = 0
    seller_name: Optional[str] = None
    seller_rating: Optional[float] = None
    seller_feedback_count: Optional[int] = None
    condition: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    location: Optional[str] = None
    shipping_cost: Optional[float] = None
    free_shipping: bool = False
    end_time: Optional[datetime] = None
    time_remaining: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None
    item_specifics: dict[str, str] = field(default_factory=dict)
    raw_html: Optional[str] = None
    extracted_at: datetime = field(default_factory=datetime.now)
    extraction_errors: list[str] = field(default_factory=list)


class HTMLParser:
    """Parse listing HTML into structured data.

    This class is designed to be subclassed for specific marketplaces.
    The default implementation provides generic extraction patterns.
    """

    # Default CSS selectors (override in subclasses)
    SELECTORS = {
        "listing_container": ".listing, .item, .product",
        "item_id": "[data-item-id], [data-listing-id], [data-product-id]",
        "title": ".title, .item-title, h1, h2",
        "current_price": ".price, .current-price, .bid-price",
        "starting_price": ".starting-price, .start-price",
        "buy_now_price": ".buy-now-price, .bin-price",
        "bid_count": ".bid-count, .bids",
        "seller_name": ".seller-name, .seller a",
        "seller_rating": ".seller-rating, .feedback-score",
        "condition": ".condition, .item-condition",
        "category": ".category, .breadcrumb a",
        "location": ".location, .item-location",
        "shipping": ".shipping, .shipping-cost",
        "end_time": ".end-time, .time-left, [data-end-time]",
        "image": ".item-image img, .gallery img, .main-image",
        "description": ".description, .item-description",
    }

    def __init__(self, custom_selectors: Optional[dict[str, str]] = None):
        """Initialize parser with optional custom selectors.

        Args:
            custom_selectors: Custom CSS selectors to override defaults.
        """
        self.selectors = {**self.SELECTORS}
        if custom_selectors:
            self.selectors.update(custom_selectors)

    def parse_listings_page(self, html: str, base_url: str) -> list[ListingData]:
        """Parse a page containing multiple listings.

        Args:
            html: Raw HTML content.
            base_url: Base URL for resolving relative links.

        Returns:
            List of extracted ListingData objects.
        """
        soup = BeautifulSoup(html, "lxml")
        listings = []

        containers = soup.select(self.selectors["listing_container"])
        logger.debug("Found listing containers", count=len(containers))

        for container in containers:
            try:
                listing = self._parse_listing_container(container, base_url)
                if listing and listing.item_id:
                    listings.append(listing)
            except Exception as e:
                logger.warning("Failed to parse listing container", error=str(e))

        return listings

    def parse_single_listing(self, html: str, url: str) -> Optional[ListingData]:
        """Parse a single listing detail page.

        Args:
            html: Raw HTML content of listing page.
            url: URL of the listing.

        Returns:
            ListingData or None if parsing fails.
        """
        soup = BeautifulSoup(html, "lxml")

        try:
            listing = ListingData(
                item_id=self._extract_item_id(soup, url),
                title=self._extract_text(soup, "title") or "Unknown",
                url=url,
                raw_html=html[:10000],  # Store truncated for debugging
            )

            # Extract prices
            listing.current_price = self._extract_price(soup, "current_price")
            listing.starting_price = self._extract_price(soup, "starting_price")
            listing.buy_now_price = self._extract_price(soup, "buy_now_price")

            # Extract bid info
            bid_text = self._extract_text(soup, "bid_count")
            if bid_text:
                listing.bid_count = self._parse_bid_count(bid_text)

            # Extract seller info
            listing.seller_name = self._extract_text(soup, "seller_name")
            rating_text = self._extract_text(soup, "seller_rating")
            if rating_text:
                listing.seller_rating = self._parse_rating(rating_text)

            # Extract item details
            listing.condition = self._extract_text(soup, "condition")
            listing.category = self._extract_category(soup)
            listing.location = self._extract_text(soup, "location")

            # Extract shipping
            shipping_text = self._extract_text(soup, "shipping")
            if shipping_text:
                listing.free_shipping = "free" in shipping_text.lower()
                if not listing.free_shipping:
                    listing.shipping_cost = parse_price(shipping_text)

            # Extract time
            listing.time_remaining = self._extract_text(soup, "end_time")
            listing.end_time = self._parse_end_time(soup)

            # Extract image
            listing.image_url = self._extract_image(soup)

            # Extract description
            listing.description = self._extract_text(soup, "description")

            # Extract item specifics
            listing.item_specifics = self._extract_item_specifics(soup)

            return listing

        except Exception as e:
            logger.error("Failed to parse listing", url=url, error=str(e))
            return None

    def _parse_listing_container(
        self, container: Tag, base_url: str
    ) -> Optional[ListingData]:
        """Parse a single listing from a container element.

        Args:
            container: BeautifulSoup Tag containing listing.
            base_url: Base URL for link resolution.

        Returns:
            ListingData or None.
        """
        # Extract item ID
        item_id = None
        id_elem = container.select_one(self.selectors["item_id"])
        if id_elem:
            item_id = (
                id_elem.get("data-item-id")
                or id_elem.get("data-listing-id")
                or id_elem.get("data-product-id")
            )

        # If no data attribute, try to extract from URL
        link_elem = container.select_one("a[href]")
        url = ""
        if link_elem:
            href = link_elem.get("href", "")
            url = self._resolve_url(href, base_url)
            if not item_id:
                item_id = self._extract_id_from_url(url)

        if not item_id:
            return None

        # Extract title
        title_elem = container.select_one(self.selectors["title"])
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        listing = ListingData(item_id=item_id, title=title, url=url)

        # Extract price
        price_elem = container.select_one(self.selectors["current_price"])
        if price_elem:
            listing.current_price = parse_price(price_elem.get_text())

        # Extract image
        img_elem = container.select_one("img")
        if img_elem:
            listing.image_url = img_elem.get("src") or img_elem.get("data-src")

        return listing

    def _extract_text(self, soup: BeautifulSoup, selector_key: str) -> Optional[str]:
        """Extract text content using a selector.

        Args:
            soup: BeautifulSoup object.
            selector_key: Key in selectors dict.

        Returns:
            Extracted text or None.
        """
        selector = self.selectors.get(selector_key)
        if not selector:
            return None

        elem = soup.select_one(selector)
        if elem:
            return elem.get_text(strip=True)
        return None

    def _extract_price(
        self, soup: BeautifulSoup, selector_key: str
    ) -> Optional[float]:
        """Extract and parse a price value.

        Args:
            soup: BeautifulSoup object.
            selector_key: Key in selectors dict.

        Returns:
            Price as float or None.
        """
        text = self._extract_text(soup, selector_key)
        return parse_price(text) if text else None

    def _extract_item_id(self, soup: BeautifulSoup, url: str) -> str:
        """Extract item ID from page or URL.

        Args:
            soup: BeautifulSoup object.
            url: Page URL.

        Returns:
            Item ID string.
        """
        # Try data attributes first
        id_elem = soup.select_one(self.selectors["item_id"])
        if id_elem:
            item_id = (
                id_elem.get("data-item-id")
                or id_elem.get("data-listing-id")
                or id_elem.get("data-product-id")
            )
            if item_id:
                return str(item_id)

        # Fall back to URL extraction
        return self._extract_id_from_url(url)

    def _extract_id_from_url(self, url: str) -> str:
        """Extract item ID from URL pattern.

        Args:
            url: Listing URL.

        Returns:
            Extracted ID or hash of URL.
        """
        import re
        from hashlib import md5

        # Common patterns: /item/12345, /listing/12345, ?id=12345
        patterns = [
            r"/item/(\d+)",
            r"/listing/(\d+)",
            r"/product/(\d+)",
            r"/itm/(\d+)",
            r"[?&]id=(\d+)",
            r"[?&]item_id=(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        # Fall back to URL hash
        return md5(url.encode()).hexdigest()[:12]

    def _extract_category(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract category from breadcrumb or category element.

        Args:
            soup: BeautifulSoup object.

        Returns:
            Category string or None.
        """
        category_elems = soup.select(self.selectors["category"])
        if category_elems:
            categories = [elem.get_text(strip=True) for elem in category_elems]
            return " > ".join(categories)
        return None

    def _extract_image(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract main image URL.

        Args:
            soup: BeautifulSoup object.

        Returns:
            Image URL or None.
        """
        img_elem = soup.select_one(self.selectors["image"])
        if img_elem:
            return img_elem.get("src") or img_elem.get("data-src")
        return None

    def _extract_item_specifics(self, soup: BeautifulSoup) -> dict[str, str]:
        """Extract item specifics/attributes table.

        Args:
            soup: BeautifulSoup object.

        Returns:
            Dict of item specifics.
        """
        specifics = {}

        # Common patterns for specs tables
        table = soup.select_one(".item-specifics, .specs-table, .attributes")
        if table:
            rows = table.select("tr, .spec-row, .attribute")
            for row in rows:
                cells = row.select("td, th, .label, .value")
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True).rstrip(":")
                    value = cells[1].get_text(strip=True)
                    if key and value:
                        specifics[key] = value

        return specifics

    def _parse_bid_count(self, text: str) -> int:
        """Parse bid count from text.

        Args:
            text: Text containing bid count.

        Returns:
            Bid count as integer.
        """
        import re

        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else 0

    def _parse_rating(self, text: str) -> Optional[float]:
        """Parse seller rating from text.

        Args:
            text: Text containing rating.

        Returns:
            Rating as float or None.
        """
        import re

        # Try percentage (99.5%)
        match = re.search(r"(\d+\.?\d*)\s*%", text)
        if match:
            return float(match.group(1))

        # Try star rating (4.5)
        match = re.search(r"(\d+\.?\d*)", text)
        if match:
            return float(match.group(1))

        return None

    def _parse_end_time(self, soup: BeautifulSoup) -> Optional[datetime]:
        """Parse auction end time.

        Args:
            soup: BeautifulSoup object.

        Returns:
            End time as datetime or None.
        """
        from dateutil import parser as date_parser

        # Try data attribute first
        time_elem = soup.select_one("[data-end-time]")
        if time_elem:
            end_time_str = time_elem.get("data-end-time")
            if end_time_str:
                try:
                    return date_parser.parse(end_time_str)
                except Exception:
                    pass

        return None

    def _resolve_url(self, href: str, base_url: str) -> str:
        """Resolve relative URL to absolute.

        Args:
            href: Possibly relative URL.
            base_url: Base URL for resolution.

        Returns:
            Absolute URL.
        """
        from urllib.parse import urljoin

        if href.startswith(("http://", "https://")):
            return href
        return urljoin(base_url, href)
