"""Quality filtering for marketplace listings.

Applies configurable rules to determine item eligibility for bidding.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
import structlog

from ..scraper.normalizer import NormalizedListing, ItemCondition

logger = structlog.get_logger(__name__)


class FilterReason(str, Enum):
    """Reasons for filtering out an item."""

    BLACKLISTED_CATEGORY = "blacklisted_category"
    BLACKLISTED_SELLER = "blacklisted_seller"
    LOW_SELLER_RATING = "low_seller_rating"
    PRICE_TOO_HIGH = "price_too_high"
    PRICE_TOO_LOW = "price_too_low"
    BAD_CONDITION = "bad_condition"
    NO_IMAGES = "no_images"
    ENDING_TOO_SOON = "ending_too_soon"
    TOO_MANY_BIDS = "too_many_bids"
    SUSPICIOUS_LISTING = "suspicious_listing"
    LOCATION_EXCLUDED = "location_excluded"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    CUSTOM_RULE = "custom_rule"


@dataclass
class FilterResult:
    """Result of filtering a single item."""

    item_id: str
    passed: bool
    reasons: list[FilterReason] = field(default_factory=list)
    reason_details: dict[str, str] = field(default_factory=dict)
    filtered_at: datetime = field(default_factory=datetime.now)

    def add_reason(self, reason: FilterReason, detail: str = "") -> None:
        """Add a filter reason."""
        self.reasons.append(reason)
        if detail:
            self.reason_details[reason.value] = detail


class FilterConfig(BaseModel):
    """Configuration for quality filtering."""

    # Category filters
    blacklisted_categories: list[str] = Field(
        default_factory=lambda: ["adult", "weapons", "tobacco"]
    )
    allowed_categories: Optional[list[str]] = None  # If set, only these allowed

    # Seller filters
    blacklisted_sellers: list[str] = Field(default_factory=list)
    min_seller_rating: float = Field(95.0, ge=0, le=100)
    min_seller_feedback: int = Field(10, ge=0)

    # Price filters
    min_price: float = Field(0.01, ge=0)
    max_price: float = Field(10000.0, ge=0)
    max_price_per_category: dict[str, float] = Field(default_factory=dict)

    # Condition filters
    acceptable_conditions: list[ItemCondition] = Field(
        default_factory=lambda: [
            ItemCondition.NEW,
            ItemCondition.LIKE_NEW,
            ItemCondition.VERY_GOOD,
            ItemCondition.GOOD,
        ]
    )

    # Image requirements
    require_images: bool = True

    # Time filters
    min_time_remaining_seconds: int = Field(300, ge=0)  # 5 minutes min
    max_time_remaining_seconds: Optional[int] = None

    # Competition filters
    max_bid_count: int = Field(50, ge=0)

    # Location filters
    excluded_locations: list[str] = Field(default_factory=list)
    allowed_locations: Optional[list[str]] = None

    # Suspicious listing detection
    suspicious_keywords: list[str] = Field(
        default_factory=lambda: [
            "test listing",
            "do not bid",
            "reserved",
            "for friend",
            "sample",
        ]
    )

    class Config:
        """Pydantic config."""

        extra = "forbid"


class QualityFilter:
    """Filter listings based on quality rules.

    Applies a series of configurable filters to determine
    which items are eligible for bidding consideration.
    """

    def __init__(
        self,
        config: FilterConfig,
        custom_filters: Optional[list[Callable[[NormalizedListing], Optional[str]]]] = None,
    ):
        """Initialize quality filter.

        Args:
            config: Filter configuration.
            custom_filters: Optional list of custom filter functions.
                Each function takes a listing and returns None if passed,
                or a string description if filtered.
        """
        self.config = config
        self.custom_filters = custom_filters or []
        self._filter_stats: dict[str, int] = {}

    def filter(self, listing: NormalizedListing) -> FilterResult:
        """Filter a single listing.

        Args:
            listing: Normalized listing to filter.

        Returns:
            FilterResult indicating pass/fail and reasons.
        """
        result = FilterResult(item_id=listing.item_id, passed=True)

        # Run all filters
        self._check_category(listing, result)
        self._check_seller(listing, result)
        self._check_price(listing, result)
        self._check_condition(listing, result)
        self._check_images(listing, result)
        self._check_time(listing, result)
        self._check_competition(listing, result)
        self._check_location(listing, result)
        self._check_suspicious(listing, result)
        self._check_custom_rules(listing, result)

        # Update stats
        for reason in result.reasons:
            self._filter_stats[reason.value] = self._filter_stats.get(reason.value, 0) + 1

        if result.reasons:
            result.passed = False
            logger.debug(
                "Item filtered",
                item_id=listing.item_id,
                reasons=[r.value for r in result.reasons],
            )

        return result

    def filter_batch(
        self, listings: list[NormalizedListing]
    ) -> tuple[list[NormalizedListing], list[FilterResult]]:
        """Filter a batch of listings.

        Args:
            listings: List of listings to filter.

        Returns:
            Tuple of (passed listings, all filter results).
        """
        passed = []
        results = []

        for listing in listings:
            result = self.filter(listing)
            results.append(result)
            if result.passed:
                passed.append(listing)

        logger.info(
            "Batch filtering complete",
            total=len(listings),
            passed=len(passed),
            filtered=len(listings) - len(passed),
        )

        return passed, results

    def get_filter_stats(self) -> dict[str, int]:
        """Get statistics on filter reasons.

        Returns:
            Dict mapping filter reason to count.
        """
        return self._filter_stats.copy()

    def reset_stats(self) -> None:
        """Reset filter statistics."""
        self._filter_stats = {}

    def _check_category(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check category filters."""
        if not listing.category:
            return

        category_lower = listing.category.lower()

        # Check blacklist
        for blacklisted in self.config.blacklisted_categories:
            if blacklisted.lower() in category_lower:
                result.add_reason(
                    FilterReason.BLACKLISTED_CATEGORY,
                    f"Category '{listing.category}' contains blacklisted term '{blacklisted}'",
                )
                return

        # Check allowed list (if set)
        if self.config.allowed_categories:
            allowed = False
            for allowed_cat in self.config.allowed_categories:
                if allowed_cat.lower() in category_lower:
                    allowed = True
                    break
            if not allowed:
                result.add_reason(
                    FilterReason.BLACKLISTED_CATEGORY,
                    f"Category '{listing.category}' not in allowed list",
                )

    def _check_seller(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check seller filters."""
        # Check blacklisted sellers
        if listing.seller_name:
            seller_lower = listing.seller_name.lower()
            for blacklisted in self.config.blacklisted_sellers:
                if blacklisted.lower() == seller_lower:
                    result.add_reason(
                        FilterReason.BLACKLISTED_SELLER,
                        f"Seller '{listing.seller_name}' is blacklisted",
                    )
                    return

        # Check rating
        if listing.seller_rating is not None:
            if listing.seller_rating < self.config.min_seller_rating:
                result.add_reason(
                    FilterReason.LOW_SELLER_RATING,
                    f"Seller rating {listing.seller_rating}% below minimum {self.config.min_seller_rating}%",
                )

        # Check feedback count
        if listing.seller_feedback_count is not None:
            if listing.seller_feedback_count < self.config.min_seller_feedback:
                result.add_reason(
                    FilterReason.LOW_SELLER_RATING,
                    f"Seller feedback count {listing.seller_feedback_count} below minimum {self.config.min_seller_feedback}",
                )

    def _check_price(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check price filters."""
        price = listing.total_price

        if price < self.config.min_price:
            result.add_reason(
                FilterReason.PRICE_TOO_LOW,
                f"Price ${price:.2f} below minimum ${self.config.min_price:.2f}",
            )

        if price > self.config.max_price:
            result.add_reason(
                FilterReason.PRICE_TOO_HIGH,
                f"Price ${price:.2f} above maximum ${self.config.max_price:.2f}",
            )

        # Check category-specific max price
        if listing.category and self.config.max_price_per_category:
            for cat_pattern, max_price in self.config.max_price_per_category.items():
                if cat_pattern.lower() in listing.category.lower():
                    if price > max_price:
                        result.add_reason(
                            FilterReason.PRICE_TOO_HIGH,
                            f"Price ${price:.2f} above category max ${max_price:.2f}",
                        )
                    break

    def _check_condition(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check item condition filters."""
        if listing.condition not in self.config.acceptable_conditions:
            result.add_reason(
                FilterReason.BAD_CONDITION,
                f"Condition '{listing.condition.value}' not acceptable",
            )

    def _check_images(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check image requirements."""
        if self.config.require_images and not listing.has_images:
            result.add_reason(
                FilterReason.NO_IMAGES,
                "Listing has no images",
            )

    def _check_time(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check time remaining filters."""
        if listing.time_remaining_seconds is None:
            return

        if listing.time_remaining_seconds < self.config.min_time_remaining_seconds:
            result.add_reason(
                FilterReason.ENDING_TOO_SOON,
                f"Only {listing.time_remaining_seconds}s remaining, minimum is {self.config.min_time_remaining_seconds}s",
            )

        if self.config.max_time_remaining_seconds:
            if listing.time_remaining_seconds > self.config.max_time_remaining_seconds:
                result.add_reason(
                    FilterReason.ENDING_TOO_SOON,
                    f"{listing.time_remaining_seconds}s remaining exceeds maximum {self.config.max_time_remaining_seconds}s",
                )

    def _check_competition(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check competition level filters."""
        if listing.bid_count > self.config.max_bid_count:
            result.add_reason(
                FilterReason.TOO_MANY_BIDS,
                f"{listing.bid_count} bids exceeds maximum {self.config.max_bid_count}",
            )

    def _check_location(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check location filters."""
        if not listing.location:
            return

        location_lower = listing.location.lower()

        # Check excluded locations
        for excluded in self.config.excluded_locations:
            if excluded.lower() in location_lower:
                result.add_reason(
                    FilterReason.LOCATION_EXCLUDED,
                    f"Location '{listing.location}' is excluded",
                )
                return

        # Check allowed locations (if set)
        if self.config.allowed_locations:
            allowed = False
            for allowed_loc in self.config.allowed_locations:
                if allowed_loc.lower() in location_lower:
                    allowed = True
                    break
            if not allowed:
                result.add_reason(
                    FilterReason.LOCATION_EXCLUDED,
                    f"Location '{listing.location}' not in allowed list",
                )

    def _check_suspicious(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Check for suspicious listing patterns."""
        title_lower = listing.title.lower()
        desc_lower = (listing.description_snippet or "").lower()
        combined = f"{title_lower} {desc_lower}"

        for keyword in self.config.suspicious_keywords:
            if keyword.lower() in combined:
                result.add_reason(
                    FilterReason.SUSPICIOUS_LISTING,
                    f"Contains suspicious keyword: '{keyword}'",
                )
                return

    def _check_custom_rules(
        self, listing: NormalizedListing, result: FilterResult
    ) -> None:
        """Apply custom filter rules."""
        for custom_filter in self.custom_filters:
            try:
                filter_result = custom_filter(listing)
                if filter_result:
                    result.add_reason(
                        FilterReason.CUSTOM_RULE,
                        filter_result,
                    )
            except Exception as e:
                logger.warning(
                    "Custom filter error",
                    item_id=listing.item_id,
                    error=str(e),
                )
