"""Main listing scraper implementation using Playwright.

Handles dynamic page loading, pagination, and data extraction
with integrated session management.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Page, Response
import structlog

from ..auth.session_manager import SessionManager
from ..auth.challenge_handler import ChallengeHandler
from ..utils.helpers import async_random_delay, ensure_directory, sanitize_filename
from .normalizer import ListingNormalizer, NormalizedListing
from .parser import HTMLParser, ListingData

logger = structlog.get_logger(__name__)


@dataclass
class ScraperConfig:
    """Configuration for the listing scraper."""

    # URLs
    listings_url: str
    base_url: str

    # Pagination
    max_pages: int = 10
    items_per_page: int = 50
    pagination_selector: str = ".pagination .next, .next-page, [data-page='next']"
    pagination_param: str = "page"

    # Timing
    page_load_timeout: int = 30000
    scroll_delay: float = 0.5
    between_pages_delay: tuple[float, float] = (2.0, 4.0)

    # Scrolling
    enable_infinite_scroll: bool = False
    max_scroll_attempts: int = 20
    scroll_wait_selector: Optional[str] = None

    # Storage
    save_raw_html: bool = True
    save_screenshots: bool = True
    storage_dir: Path = Path("data")

    # Retry
    max_retries: int = 3
    retry_delay: tuple[float, float] = (5.0, 10.0)

    # Custom selectors (override parser defaults)
    custom_selectors: dict[str, str] = field(default_factory=dict)


@dataclass
class ScrapeResult:
    """Result of a scraping operation."""

    listings: list[NormalizedListing]
    total_found: int
    pages_scraped: int
    errors: list[str]
    warnings: list[str]
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    raw_items_count: int
    filtered_count: int


class ListingScraper:
    """Scrape marketplace listings with Playwright.

    Features:
    - Dynamic content handling (JS-rendered pages)
    - Infinite scroll support
    - Pagination handling
    - Session integration
    - Raw data storage for debugging
    - Screenshot capture on errors
    - Automatic retry with backoff
    """

    def __init__(
        self,
        session_manager: SessionManager,
        config: ScraperConfig,
        normalizer: Optional[ListingNormalizer] = None,
        parser: Optional[HTMLParser] = None,
    ):
        """Initialize scraper.

        Args:
            session_manager: Authenticated session manager.
            config: Scraper configuration.
            normalizer: Optional custom normalizer.
            parser: Optional custom HTML parser.
        """
        self.session = session_manager
        self.config = config
        self.normalizer = normalizer or ListingNormalizer()
        self.parser = parser or HTMLParser(config.custom_selectors)
        self._challenge_handler = ChallengeHandler()

        # Setup storage directories
        self.raw_dir = ensure_directory(config.storage_dir / "raw")
        self.processed_dir = ensure_directory(config.storage_dir / "processed")
        self.screenshots_dir = ensure_directory(config.storage_dir / "screenshots")

    async def scrape_listings(
        self,
        search_query: Optional[str] = None,
        category: Optional[str] = None,
        filters: Optional[dict[str, Any]] = None,
    ) -> ScrapeResult:
        """Scrape listings from the marketplace.

        Args:
            search_query: Optional search term.
            category: Optional category filter.
            filters: Additional filter parameters.

        Returns:
            ScrapeResult with normalized listings and metadata.
        """
        started_at = datetime.now()
        all_raw_listings: list[ListingData] = []
        errors: list[str] = []
        warnings: list[str] = []
        pages_scraped = 0

        logger.info(
            "Starting listing scrape",
            search=search_query,
            category=category,
            max_pages=self.config.max_pages,
        )

        try:
            # Ensure authenticated
            if not await self.session.ensure_authenticated():
                errors.append("Failed to authenticate session")
                return self._create_error_result(started_at, errors)

            page = self.session.page
            if not page:
                errors.append("No page available from session")
                return self._create_error_result(started_at, errors)

            # Build initial URL
            url = self._build_url(search_query, category, filters, page_num=1)

            # Navigate to listings
            await self._navigate_with_retry(page, url)

            # Handle any challenges
            challenge = await self._challenge_handler.detect_challenge(page)
            if challenge.detected:
                result = await self._challenge_handler.handle_challenge(
                    page, challenge.challenge_type
                )
                if not result.handled:
                    errors.append(f"Challenge not handled: {challenge.message}")
                    return self._create_error_result(started_at, errors)

            # Scrape pages
            for page_num in range(1, self.config.max_pages + 1):
                logger.info("Scraping page", page=page_num)

                try:
                    # Get listings from current page
                    raw_listings = await self._scrape_current_page(page, page_num)
                    all_raw_listings.extend(raw_listings)
                    pages_scraped += 1

                    logger.debug(
                        "Page scraped",
                        page=page_num,
                        items=len(raw_listings),
                        total=len(all_raw_listings),
                    )

                    # Check for next page
                    if not await self._has_next_page(page):
                        logger.info("No more pages available")
                        break

                    # Navigate to next page
                    if page_num < self.config.max_pages:
                        await self._go_to_next_page(page, page_num + 1, search_query, category, filters)

                except Exception as e:
                    error_msg = f"Error on page {page_num}: {str(e)}"
                    errors.append(error_msg)
                    logger.warning(error_msg)

                    if self.config.save_screenshots:
                        await self._save_error_screenshot(page, f"error_page_{page_num}")

                    # Continue to next page if possible
                    if page_num < self.config.max_pages:
                        try:
                            await self._go_to_next_page(page, page_num + 1, search_query, category, filters)
                        except Exception:
                            break

        except Exception as e:
            error_msg = f"Scraping failed: {str(e)}"
            errors.append(error_msg)
            logger.error(error_msg)

        # Normalize all listings
        normalized_listings, norm_results = self.normalizer.normalize_batch(all_raw_listings)

        # Collect normalization warnings
        for result in norm_results:
            warnings.extend(result.warnings)

        # Save processed data
        await self._save_processed_data(normalized_listings)

        completed_at = datetime.now()
        duration = (completed_at - started_at).total_seconds()

        result = ScrapeResult(
            listings=normalized_listings,
            total_found=len(normalized_listings),
            pages_scraped=pages_scraped,
            errors=errors,
            warnings=warnings,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            raw_items_count=len(all_raw_listings),
            filtered_count=len(all_raw_listings) - len(normalized_listings),
        )

        logger.info(
            "Scraping complete",
            total=result.total_found,
            pages=pages_scraped,
            duration=f"{duration:.2f}s",
            errors=len(errors),
        )

        return result

    async def scrape_single_listing(self, url: str) -> Optional[NormalizedListing]:
        """Scrape a single listing detail page.

        Args:
            url: URL of the listing.

        Returns:
            NormalizedListing or None if failed.
        """
        logger.info("Scraping single listing", url=url)

        try:
            if not await self.session.ensure_authenticated():
                logger.error("Failed to authenticate for single listing")
                return None

            page = self.session.page
            if not page:
                return None

            await self._navigate_with_retry(page, url)

            # Get page content
            html = await page.content()

            # Save raw HTML
            if self.config.save_raw_html:
                await self._save_raw_html(html, url)

            # Parse listing
            raw_listing = self.parser.parse_single_listing(html, url)
            if not raw_listing:
                logger.warning("Failed to parse listing", url=url)
                return None

            # Normalize
            result = self.normalizer.normalize(raw_listing)
            if result.success and result.listing:
                return result.listing

            logger.warning(
                "Normalization failed",
                url=url,
                errors=result.errors,
            )
            return None

        except Exception as e:
            logger.error("Single listing scrape failed", url=url, error=str(e))
            return None

    async def _scrape_current_page(
        self, page: Page, page_num: int
    ) -> list[ListingData]:
        """Scrape listings from the current page.

        Args:
            page: Playwright page.
            page_num: Current page number.

        Returns:
            List of raw ListingData.
        """
        # Wait for content to load
        await page.wait_for_load_state("networkidle", timeout=self.config.page_load_timeout)

        # Handle infinite scroll if enabled
        if self.config.enable_infinite_scroll:
            await self._handle_infinite_scroll(page)

        # Get page HTML
        html = await page.content()

        # Save raw HTML
        if self.config.save_raw_html:
            await self._save_raw_html(html, f"page_{page_num}", page_num)

        # Parse listings
        listings = self.parser.parse_listings_page(html, self.config.base_url)

        return listings

    async def _handle_infinite_scroll(self, page: Page) -> None:
        """Handle infinite scroll loading.

        Args:
            page: Playwright page.
        """
        previous_height = 0
        scroll_attempts = 0

        while scroll_attempts < self.config.max_scroll_attempts:
            # Scroll to bottom
            current_height = await page.evaluate("document.body.scrollHeight")

            if current_height == previous_height:
                # No new content loaded
                break

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(self.config.scroll_delay)

            # Wait for new content
            if self.config.scroll_wait_selector:
                try:
                    await page.wait_for_selector(
                        self.config.scroll_wait_selector,
                        timeout=5000,
                    )
                except Exception:
                    pass

            previous_height = current_height
            scroll_attempts += 1

        logger.debug("Infinite scroll complete", attempts=scroll_attempts)

    async def _has_next_page(self, page: Page) -> bool:
        """Check if there's a next page.

        Args:
            page: Playwright page.

        Returns:
            True if next page exists.
        """
        next_button = await page.query_selector(self.config.pagination_selector)
        if next_button:
            # Check if it's disabled
            is_disabled = await next_button.get_attribute("disabled")
            has_disabled_class = await next_button.evaluate(
                "el => el.classList.contains('disabled')"
            )
            return not is_disabled and not has_disabled_class
        return False

    async def _go_to_next_page(
        self,
        page: Page,
        page_num: int,
        search_query: Optional[str],
        category: Optional[str],
        filters: Optional[dict[str, Any]],
    ) -> None:
        """Navigate to the next page.

        Args:
            page: Playwright page.
            page_num: Target page number.
            search_query: Search query.
            category: Category filter.
            filters: Additional filters.
        """
        # Random delay between pages
        await async_random_delay(*self.config.between_pages_delay)

        # Try clicking next button first
        next_button = await page.query_selector(self.config.pagination_selector)
        if next_button:
            try:
                await next_button.click()
                await page.wait_for_load_state("networkidle", timeout=self.config.page_load_timeout)
                return
            except Exception:
                pass

        # Fall back to URL parameter
        url = self._build_url(search_query, category, filters, page_num)
        await self._navigate_with_retry(page, url)

    async def _navigate_with_retry(self, page: Page, url: str) -> None:
        """Navigate to URL with retry logic.

        Args:
            page: Playwright page.
            url: Target URL.
        """
        for attempt in range(self.config.max_retries):
            try:
                response = await page.goto(url, timeout=self.config.page_load_timeout)

                if response:
                    # Check response status
                    challenge = await self._challenge_handler.check_response(response)
                    if challenge.detected and challenge.retry_after:
                        logger.warning(
                            "Rate limited, waiting",
                            seconds=challenge.retry_after,
                        )
                        await asyncio.sleep(challenge.retry_after)
                        continue

                await page.wait_for_load_state("domcontentloaded")
                return

            except Exception as e:
                logger.warning(
                    "Navigation failed",
                    url=url,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt < self.config.max_retries - 1:
                    await async_random_delay(*self.config.retry_delay)

        raise Exception(f"Failed to navigate to {url} after {self.config.max_retries} attempts")

    def _build_url(
        self,
        search_query: Optional[str],
        category: Optional[str],
        filters: Optional[dict[str, Any]],
        page_num: int,
    ) -> str:
        """Build URL with parameters.

        Args:
            search_query: Search query.
            category: Category filter.
            filters: Additional filters.
            page_num: Page number.

        Returns:
            Complete URL string.
        """
        from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

        # Parse base URL
        parsed = urlparse(self.config.listings_url)
        params = parse_qs(parsed.query)

        # Add parameters
        if search_query:
            params["q"] = [search_query]
        if category:
            params["category"] = [category]
        if page_num > 1:
            params[self.config.pagination_param] = [str(page_num)]
        if filters:
            for key, value in filters.items():
                params[key] = [str(value)]

        # Flatten params
        flat_params = {k: v[0] for k, v in params.items()}
        query_string = urlencode(flat_params)

        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            query_string,
            parsed.fragment,
        ))

    async def _save_raw_html(
        self,
        html: str,
        identifier: str,
        page_num: Optional[int] = None,
    ) -> None:
        """Save raw HTML for debugging.

        Args:
            html: HTML content.
            identifier: File identifier.
            page_num: Optional page number.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = sanitize_filename(f"{timestamp}_{identifier}.html")
        filepath = self.raw_dir / filename

        try:
            filepath.write_text(html, encoding="utf-8")
            logger.debug("Saved raw HTML", path=str(filepath))
        except Exception as e:
            logger.warning("Failed to save raw HTML", error=str(e))

    async def _save_processed_data(
        self, listings: list[NormalizedListing]
    ) -> None:
        """Save processed listings data.

        Args:
            listings: List of normalized listings.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"listings_{timestamp}.json"
        filepath = self.processed_dir / filename

        try:
            data = [listing.model_dump(mode="json") for listing in listings]
            filepath.write_text(json.dumps(data, indent=2, default=str))
            logger.info("Saved processed listings", path=str(filepath), count=len(listings))
        except Exception as e:
            logger.warning("Failed to save processed data", error=str(e))

    async def _save_error_screenshot(self, page: Page, identifier: str) -> None:
        """Save screenshot on error.

        Args:
            page: Playwright page.
            identifier: Screenshot identifier.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{identifier}.png"
        filepath = self.screenshots_dir / filename

        try:
            await page.screenshot(path=str(filepath), full_page=True)
            logger.debug("Saved error screenshot", path=str(filepath))
        except Exception as e:
            logger.warning("Failed to save screenshot", error=str(e))

    def _create_error_result(
        self, started_at: datetime, errors: list[str]
    ) -> ScrapeResult:
        """Create error result.

        Args:
            started_at: Start time.
            errors: Error messages.

        Returns:
            ScrapeResult with errors.
        """
        completed_at = datetime.now()
        return ScrapeResult(
            listings=[],
            total_found=0,
            pages_scraped=0,
            errors=errors,
            warnings=[],
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=(completed_at - started_at).total_seconds(),
            raw_items_count=0,
            filtered_count=0,
        )
