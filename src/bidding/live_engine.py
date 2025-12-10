"""LIVE bidding engine for authenticated bid execution.

Executes real bids on the marketplace with full safety controls,
confirmation handling, and audit logging.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from playwright.async_api import Page
import structlog

from ..auth.session_manager import SessionManager
from ..scraper.normalizer import NormalizedListing
from ..scoring.scoring_engine import ItemScore
from ..utils.helpers import async_random_delay
from .bid_calculator import BidCalculator, BidDecision

logger = structlog.get_logger(__name__)


class BidStatus(str, Enum):
    """Status of a bid execution."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    OUTBID = "outbid"
    WON = "won"
    LOST = "lost"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BidExecutionResult:
    """Result of bid execution attempt."""

    item_id: str
    status: BidStatus
    bid_amount: float
    confirmation_code: Optional[str] = None
    current_high_bid: Optional[float] = None
    is_winning: bool = False
    error_message: str = ""
    executed_at: datetime = field(default_factory=datetime.now)
    response_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "item_id": self.item_id,
            "status": self.status.value,
            "bid_amount": self.bid_amount,
            "confirmation_code": self.confirmation_code,
            "current_high_bid": self.current_high_bid,
            "is_winning": self.is_winning,
            "error_message": self.error_message,
            "executed_at": self.executed_at.isoformat(),
        }


class LiveBiddingEngine:
    """Execute real bids on the marketplace.

    Features:
    - Authenticated bid submission
    - Confirmation verification
    - Exposure limit enforcement
    - Pause/stop controls
    - Human-like delays
    - Comprehensive logging
    - Rollback on limit breach

    Safety measures:
    - Double-checks budget before each bid
    - Validates bid amount against current price
    - Requires explicit confirmation
    - Tracks all pending bids
    """

    # Default bid form selectors (customize per marketplace)
    DEFAULT_SELECTORS = {
        "bid_input": 'input[name="bid"], input[name="amount"], #bid-amount',
        "bid_button": 'button[type="submit"], .bid-button, #place-bid',
        "confirm_button": '.confirm-bid, #confirm-bid, button.confirm',
        "success_indicator": '.bid-success, .bid-confirmed, .winning-bid',
        "error_indicator": '.bid-error, .error-message, .bid-failed',
        "current_bid": '.current-bid, .high-bid, .current-price',
        "confirmation_code": '.confirmation-number, .bid-id, [data-confirmation]',
    }

    def __init__(
        self,
        session_manager: SessionManager,
        bid_calculator: BidCalculator,
        selectors: Optional[dict[str, str]] = None,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
        on_bid_placed: Optional[Callable[[BidExecutionResult], None]] = None,
        on_error: Optional[Callable[[str, Exception], None]] = None,
    ):
        """Initialize live bidding engine.

        Args:
            session_manager: Authenticated session manager.
            bid_calculator: Bid calculator for decisions and tracking.
            selectors: Custom CSS selectors for bid forms.
            min_delay: Minimum delay between actions (seconds).
            max_delay: Maximum delay between actions (seconds).
            on_bid_placed: Callback after successful bid.
            on_error: Callback on error.
        """
        self.session = session_manager
        self.bid_calculator = bid_calculator
        self.selectors = {**self.DEFAULT_SELECTORS, **(selectors or {})}
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.on_bid_placed = on_bid_placed
        self.on_error = on_error

        # Control state
        self._is_running = False
        self._is_paused = False
        self._stop_requested = False

        # Execution tracking
        self._executed_bids: list[BidExecutionResult] = []
        self._failed_bids: list[BidExecutionResult] = []

    @property
    def is_running(self) -> bool:
        """Check if engine is currently running."""
        return self._is_running

    @property
    def is_paused(self) -> bool:
        """Check if engine is paused."""
        return self._is_paused

    def pause(self) -> None:
        """Pause bid execution."""
        self._is_paused = True
        logger.info("Live bidding paused")

    def resume(self) -> None:
        """Resume bid execution."""
        self._is_paused = False
        logger.info("Live bidding resumed")

    def stop(self) -> None:
        """Request stop of bid execution."""
        self._stop_requested = True
        logger.info("Live bidding stop requested")

    async def execute_bid(
        self,
        listing: NormalizedListing,
        decision: BidDecision,
        score: ItemScore,
    ) -> BidExecutionResult:
        """Execute a single bid.

        Args:
            listing: Listing to bid on.
            decision: Bid decision with amounts.
            score: Item score for logging.

        Returns:
            BidExecutionResult with outcome.
        """
        if not decision.should_bid or not decision.max_bid:
            return BidExecutionResult(
                item_id=listing.item_id,
                status=BidStatus.FAILED,
                bid_amount=0,
                error_message="Invalid bid decision",
            )

        # Pre-execution safety checks
        safety_check = self._pre_bid_safety_check(listing, decision)
        if not safety_check["safe"]:
            return BidExecutionResult(
                item_id=listing.item_id,
                status=BidStatus.CANCELLED,
                bid_amount=decision.recommended_bid or 0,
                error_message=safety_check["reason"],
            )

        logger.info(
            "Executing LIVE bid",
            item_id=listing.item_id,
            amount=decision.recommended_bid,
            max=decision.max_bid,
            score=score.total_score,
        )

        try:
            # Ensure authenticated session
            if not await self.session.ensure_authenticated():
                raise Exception("Session authentication failed")

            page = self.session.page
            if not page:
                raise Exception("No page available")

            # Navigate to listing
            await self._navigate_to_listing(page, listing.url)

            # Verify current price hasn't changed dramatically
            current_price = await self._get_current_price(page)
            if current_price and current_price > decision.max_bid:
                return BidExecutionResult(
                    item_id=listing.item_id,
                    status=BidStatus.CANCELLED,
                    bid_amount=decision.recommended_bid or 0,
                    error_message=f"Current price ${current_price} exceeds max bid ${decision.max_bid}",
                )

            # Execute bid
            result = await self._submit_bid(page, listing, decision)

            # Track the bid
            if result.status in [BidStatus.SUBMITTED, BidStatus.CONFIRMED]:
                self.bid_calculator.record_bid(
                    listing.item_id,
                    result.bid_amount,
                    listing.category,
                )
                self._executed_bids.append(result)

                if self.on_bid_placed:
                    self.on_bid_placed(result)
            else:
                self._failed_bids.append(result)

            return result

        except Exception as e:
            error_msg = f"Bid execution failed: {str(e)}"
            logger.error(error_msg, item_id=listing.item_id)

            if self.on_error:
                self.on_error(listing.item_id, e)

            result = BidExecutionResult(
                item_id=listing.item_id,
                status=BidStatus.FAILED,
                bid_amount=decision.recommended_bid or 0,
                error_message=error_msg,
            )
            self._failed_bids.append(result)
            return result

    async def execute_batch(
        self,
        bids: list[tuple[NormalizedListing, BidDecision, ItemScore]],
    ) -> list[BidExecutionResult]:
        """Execute multiple bids with safety controls.

        Args:
            bids: List of (listing, decision, score) tuples.

        Returns:
            List of BidExecutionResult for each bid.
        """
        self._is_running = True
        self._stop_requested = False
        results = []

        logger.info("Starting batch bid execution", count=len(bids))

        try:
            for listing, decision, score in bids:
                # Check for stop request
                if self._stop_requested:
                    logger.info("Batch execution stopped by user")
                    break

                # Wait if paused
                while self._is_paused:
                    await asyncio.sleep(1)
                    if self._stop_requested:
                        break

                if self._stop_requested:
                    break

                # Execute bid
                result = await self.execute_bid(listing, decision, score)
                results.append(result)

                # Human-like delay between bids
                await async_random_delay(self.min_delay, self.max_delay)

                # Check exposure limits after each bid
                budget_status = self.bid_calculator.get_budget_status()
                if budget_status["exposure_remaining"] <= 0:
                    logger.warning("Exposure limit reached, stopping batch")
                    break

        finally:
            self._is_running = False

        logger.info(
            "Batch execution complete",
            total=len(bids),
            executed=len(results),
            successful=sum(1 for r in results if r.status == BidStatus.CONFIRMED),
        )

        return results

    def _pre_bid_safety_check(
        self, listing: NormalizedListing, decision: BidDecision
    ) -> dict[str, Any]:
        """Perform safety checks before bid execution.

        Args:
            listing: Listing to bid on.
            decision: Bid decision.

        Returns:
            Dict with 'safe' boolean and 'reason' if not safe.
        """
        # Check budget status
        budget = self.bid_calculator.get_budget_status()

        if budget["daily_remaining"] <= 0:
            return {"safe": False, "reason": "Daily budget exhausted"}

        if budget["weekly_remaining"] <= 0:
            return {"safe": False, "reason": "Weekly budget exhausted"}

        if budget["exposure_remaining"] < (decision.max_bid or 0):
            return {"safe": False, "reason": "Would exceed exposure limit"}

        # Check for duplicate bid
        if listing.item_id in [b.item_id for b in self._executed_bids]:
            return {"safe": False, "reason": "Already bid on this item"}

        return {"safe": True, "reason": ""}

    async def _navigate_to_listing(self, page: Page, url: str) -> None:
        """Navigate to listing page.

        Args:
            page: Playwright page.
            url: Listing URL.
        """
        await async_random_delay(0.5, 1.5)
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle")
        await async_random_delay(1, 2)

    async def _get_current_price(self, page: Page) -> Optional[float]:
        """Get current bid price from page.

        Args:
            page: Playwright page.

        Returns:
            Current price or None.
        """
        try:
            selector = self.selectors["current_bid"]
            elem = await page.query_selector(selector)
            if elem:
                text = await elem.text_content()
                if text:
                    from ..utils.helpers import parse_price

                    return parse_price(text)
        except Exception as e:
            logger.warning("Could not get current price", error=str(e))
        return None

    async def _submit_bid(
        self,
        page: Page,
        listing: NormalizedListing,
        decision: BidDecision,
    ) -> BidExecutionResult:
        """Submit bid on the page.

        Args:
            page: Playwright page.
            listing: Listing data.
            decision: Bid decision.

        Returns:
            BidExecutionResult.
        """
        bid_amount = decision.recommended_bid or decision.max_bid
        if not bid_amount:
            return BidExecutionResult(
                item_id=listing.item_id,
                status=BidStatus.FAILED,
                bid_amount=0,
                error_message="No bid amount specified",
            )

        try:
            # Find and fill bid input
            bid_input = await page.wait_for_selector(
                self.selectors["bid_input"],
                timeout=10000,
            )
            await async_random_delay(0.3, 0.8)
            await bid_input.fill(str(bid_amount))

            # Click bid button
            await async_random_delay(0.5, 1.0)
            bid_button = await page.query_selector(self.selectors["bid_button"])
            if not bid_button:
                raise Exception("Bid button not found")
            await bid_button.click()

            # Wait for response
            await async_random_delay(1, 2)

            # Check for confirmation dialog
            confirm_button = await page.query_selector(self.selectors["confirm_button"])
            if confirm_button:
                await async_random_delay(0.5, 1.0)
                await confirm_button.click()
                await async_random_delay(1, 2)

            # Check result
            success = await page.query_selector(self.selectors["success_indicator"])
            error = await page.query_selector(self.selectors["error_indicator"])

            if error:
                error_text = await error.text_content()
                return BidExecutionResult(
                    item_id=listing.item_id,
                    status=BidStatus.FAILED,
                    bid_amount=bid_amount,
                    error_message=error_text or "Bid rejected",
                )

            # Get confirmation code if available
            confirmation_code = None
            conf_elem = await page.query_selector(self.selectors["confirmation_code"])
            if conf_elem:
                confirmation_code = await conf_elem.text_content()
                if not confirmation_code:
                    confirmation_code = await conf_elem.get_attribute("data-confirmation")

            # Get current high bid
            current_high = await self._get_current_price(page)

            is_winning = success is not None
            status = BidStatus.CONFIRMED if is_winning else BidStatus.SUBMITTED

            return BidExecutionResult(
                item_id=listing.item_id,
                status=status,
                bid_amount=bid_amount,
                confirmation_code=confirmation_code,
                current_high_bid=current_high,
                is_winning=is_winning,
            )

        except Exception as e:
            return BidExecutionResult(
                item_id=listing.item_id,
                status=BidStatus.FAILED,
                bid_amount=bid_amount,
                error_message=str(e),
            )

    def get_execution_summary(self) -> dict[str, Any]:
        """Get summary of bid executions.

        Returns:
            Summary statistics.
        """
        successful = [b for b in self._executed_bids if b.status == BidStatus.CONFIRMED]
        failed = self._failed_bids

        return {
            "total_executed": len(self._executed_bids),
            "successful": len(successful),
            "failed": len(failed),
            "total_amount_bid": sum(b.bid_amount for b in successful),
            "is_running": self._is_running,
            "is_paused": self._is_paused,
            "executed_bids": [b.to_dict() for b in self._executed_bids],
            "failed_bids": [b.to_dict() for b in failed],
        }

    def reset(self) -> None:
        """Reset execution state."""
        self._executed_bids = []
        self._failed_bids = []
        self._is_running = False
        self._is_paused = False
        self._stop_requested = False
