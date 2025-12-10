"""Listing data normalisation and validation.

Converts raw extracted data into a consistent schema with validation
and quality filtering.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator
import structlog

from .parser import ListingData

logger = structlog.get_logger(__name__)


class ItemCondition(str, Enum):
    """Standardized item condition values."""

    NEW = "new"
    LIKE_NEW = "like_new"
    VERY_GOOD = "very_good"
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    FOR_PARTS = "for_parts"
    UNKNOWN = "unknown"


class ListingStatus(str, Enum):
    """Listing availability status."""

    ACTIVE = "active"
    ENDED = "ended"
    SOLD = "sold"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class NormalizedListing(BaseModel):
    """Normalized listing data with validation.

    This is the standard schema used throughout the application
    for item evaluation, scoring, and bidding.
    """

    # Core identification
    item_id: str = Field(..., min_length=1, description="Unique item identifier")
    title: str = Field(..., min_length=1, max_length=500)
    url: str = Field(..., min_length=1)

    # Pricing (all in base currency)
    current_price: float = Field(..., ge=0)
    starting_price: Optional[float] = Field(None, ge=0)
    buy_now_price: Optional[float] = Field(None, ge=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    shipping_cost: float = Field(0.0, ge=0)
    total_price: float = Field(..., ge=0)  # current_price + shipping

    # Bidding info
    bid_count: int = Field(0, ge=0)
    is_auction: bool = True
    has_buy_now: bool = False

    # Seller info
    seller_name: Optional[str] = None
    seller_rating: Optional[float] = Field(None, ge=0, le=100)
    seller_feedback_count: Optional[int] = Field(None, ge=0)
    seller_trust_score: float = Field(50.0, ge=0, le=100)

    # Item details
    condition: ItemCondition = ItemCondition.UNKNOWN
    category: Optional[str] = None
    subcategory: Optional[str] = None
    location: Optional[str] = None
    description_snippet: Optional[str] = Field(None, max_length=500)

    # Time info
    end_time: Optional[datetime] = None
    time_remaining_seconds: Optional[int] = Field(None, ge=0)
    is_ending_soon: bool = False

    # Media
    image_url: Optional[str] = None
    has_images: bool = False

    # Item attributes (normalized)
    attributes: dict[str, Any] = Field(default_factory=dict)

    # Quality/filtering metadata
    status: ListingStatus = ListingStatus.ACTIVE
    is_valid: bool = True
    validation_warnings: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)

    # Processing metadata
    normalized_at: datetime = Field(default_factory=datetime.now)
    raw_data_hash: Optional[str] = None

    @field_validator("seller_rating")
    @classmethod
    def validate_rating(cls, v):
        """Ensure rating is in valid range."""
        if v is not None and (v < 0 or v > 100):
            return min(max(v, 0), 100)
        return v

    class Config:
        """Pydantic config."""

        extra = "ignore"
        json_encoders = {datetime: lambda v: v.isoformat()}


@dataclass
class NormalizationResult:
    """Result of normalization process."""

    listing: Optional[NormalizedListing]
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ListingNormalizer:
    """Normalize raw listing data into consistent schema.

    Handles:
    - Field mapping and type conversion
    - Condition standardization
    - Price calculation (including shipping)
    - Seller trust scoring
    - Quality filtering
    - Data validation
    """

    # Condition mapping from various formats
    CONDITION_MAP = {
        # New conditions
        "new": ItemCondition.NEW,
        "brand new": ItemCondition.NEW,
        "new with tags": ItemCondition.NEW,
        "new with box": ItemCondition.NEW,
        "bnib": ItemCondition.NEW,
        "sealed": ItemCondition.NEW,
        # Like new
        "like new": ItemCondition.LIKE_NEW,
        "mint": ItemCondition.LIKE_NEW,
        "open box": ItemCondition.LIKE_NEW,
        "new other": ItemCondition.LIKE_NEW,
        "refurbished": ItemCondition.LIKE_NEW,
        "certified refurbished": ItemCondition.LIKE_NEW,
        # Very good
        "very good": ItemCondition.VERY_GOOD,
        "excellent": ItemCondition.VERY_GOOD,
        # Good
        "good": ItemCondition.GOOD,
        # Acceptable
        "acceptable": ItemCondition.ACCEPTABLE,
        "fair": ItemCondition.ACCEPTABLE,
        "used": ItemCondition.ACCEPTABLE,
        # For parts
        "for parts": ItemCondition.FOR_PARTS,
        "not working": ItemCondition.FOR_PARTS,
        "parts only": ItemCondition.FOR_PARTS,
    }

    # Categories to blacklist
    DEFAULT_BLACKLIST = [
        "adult",
        "weapons",
        "firearms",
        "tobacco",
        "drugs",
        "counterfeit",
    ]

    def __init__(
        self,
        blacklisted_categories: Optional[list[str]] = None,
        min_seller_rating: float = 0.0,
        require_images: bool = False,
        ending_soon_threshold: int = 3600,  # 1 hour in seconds
    ):
        """Initialize normalizer with filtering options.

        Args:
            blacklisted_categories: Categories to filter out.
            min_seller_rating: Minimum seller rating (0-100).
            require_images: Whether items must have images.
            ending_soon_threshold: Seconds remaining to mark as "ending soon".
        """
        self.blacklisted_categories = blacklisted_categories or self.DEFAULT_BLACKLIST
        self.min_seller_rating = min_seller_rating
        self.require_images = require_images
        self.ending_soon_threshold = ending_soon_threshold

    def normalize(self, raw: ListingData) -> NormalizationResult:
        """Normalize a raw listing.

        Args:
            raw: Raw ListingData from parser.

        Returns:
            NormalizationResult with normalized listing or errors.
        """
        errors = []
        warnings = []

        try:
            # Basic validation
            if not raw.item_id:
                errors.append("Missing item ID")
                return NormalizationResult(None, False, errors)

            if not raw.title:
                errors.append("Missing title")
                return NormalizationResult(None, False, errors)

            # Calculate prices
            current_price = raw.current_price or raw.starting_price or 0.0
            if current_price <= 0:
                warnings.append("No price available, using 0")

            shipping_cost = raw.shipping_cost or 0.0
            if raw.free_shipping:
                shipping_cost = 0.0

            total_price = current_price + shipping_cost

            # Normalize condition
            condition = self._normalize_condition(raw.condition)

            # Calculate seller trust score
            seller_trust = self._calculate_seller_trust(
                raw.seller_rating, raw.seller_feedback_count
            )

            # Check if ending soon
            time_remaining = None
            is_ending_soon = False
            if raw.end_time:
                delta = raw.end_time - datetime.now()
                time_remaining = max(0, int(delta.total_seconds()))
                is_ending_soon = time_remaining <= self.ending_soon_threshold

            # Create normalized listing
            listing = NormalizedListing(
                item_id=raw.item_id,
                title=raw.title,
                url=raw.url,
                current_price=current_price,
                starting_price=raw.starting_price,
                buy_now_price=raw.buy_now_price,
                currency=raw.currency,
                shipping_cost=shipping_cost,
                total_price=total_price,
                bid_count=raw.bid_count,
                is_auction=raw.buy_now_price is None or raw.current_price != raw.buy_now_price,
                has_buy_now=raw.buy_now_price is not None,
                seller_name=raw.seller_name,
                seller_rating=raw.seller_rating,
                seller_feedback_count=raw.seller_feedback_count,
                seller_trust_score=seller_trust,
                condition=condition,
                category=raw.category,
                subcategory=raw.subcategory,
                location=raw.location,
                description_snippet=self._truncate_description(raw.description),
                end_time=raw.end_time,
                time_remaining_seconds=time_remaining,
                is_ending_soon=is_ending_soon,
                image_url=raw.image_url,
                has_images=raw.image_url is not None,
                attributes=raw.item_specifics,
                validation_warnings=warnings,
                raw_data_hash=self._hash_raw_data(raw),
            )

            # Apply quality filters
            quality_flags, filter_warnings = self._apply_quality_filters(listing)
            listing.quality_flags = quality_flags
            listing.validation_warnings.extend(filter_warnings)
            listing.is_valid = len(quality_flags) == 0

            return NormalizationResult(listing, True, errors, warnings)

        except Exception as e:
            logger.error("Normalization failed", item_id=raw.item_id, error=str(e))
            errors.append(f"Normalization error: {str(e)}")
            return NormalizationResult(None, False, errors)

    def normalize_batch(
        self, listings: list[ListingData]
    ) -> tuple[list[NormalizedListing], list[NormalizationResult]]:
        """Normalize a batch of listings.

        Args:
            listings: List of raw listings.

        Returns:
            Tuple of (successful listings, all results including failures).
        """
        successful = []
        all_results = []

        for raw in listings:
            result = self.normalize(raw)
            all_results.append(result)
            if result.success and result.listing:
                successful.append(result.listing)

        logger.info(
            "Batch normalization complete",
            total=len(listings),
            successful=len(successful),
            failed=len(listings) - len(successful),
        )

        return successful, all_results

    def _normalize_condition(self, condition_str: Optional[str]) -> ItemCondition:
        """Normalize condition string to enum.

        Args:
            condition_str: Raw condition string.

        Returns:
            Standardized ItemCondition.
        """
        if not condition_str:
            return ItemCondition.UNKNOWN

        normalized = condition_str.lower().strip()

        for key, value in self.CONDITION_MAP.items():
            if key in normalized:
                return value

        return ItemCondition.UNKNOWN

    def _calculate_seller_trust(
        self,
        rating: Optional[float],
        feedback_count: Optional[int],
    ) -> float:
        """Calculate seller trust score.

        Combines rating and feedback count into a single trust metric.

        Args:
            rating: Seller rating (0-100 scale).
            feedback_count: Number of feedback/reviews.

        Returns:
            Trust score 0-100.
        """
        if rating is None:
            return 50.0  # Default neutral score

        # Base score from rating
        base_score = rating

        # Adjust based on feedback count (more feedback = more reliable)
        if feedback_count is not None:
            if feedback_count < 10:
                # New seller, less trustworthy even with high rating
                base_score *= 0.8
            elif feedback_count < 100:
                base_score *= 0.9
            elif feedback_count > 1000:
                # Established seller, slight boost
                base_score = min(100, base_score * 1.05)

        return round(base_score, 2)

    def _apply_quality_filters(
        self, listing: NormalizedListing
    ) -> tuple[list[str], list[str]]:
        """Apply quality filters and return flags.

        Args:
            listing: Normalized listing to check.

        Returns:
            Tuple of (quality flags that failed, warnings).
        """
        flags = []
        warnings = []

        # Check blacklisted categories
        if listing.category:
            cat_lower = listing.category.lower()
            for blacklisted in self.blacklisted_categories:
                if blacklisted.lower() in cat_lower:
                    flags.append(f"blacklisted_category:{blacklisted}")

        # Check seller rating
        if listing.seller_rating is not None:
            if listing.seller_rating < self.min_seller_rating:
                flags.append(f"low_seller_rating:{listing.seller_rating}")

        # Check images requirement
        if self.require_images and not listing.has_images:
            flags.append("no_images")

        # Check for suspicious patterns
        title_lower = listing.title.lower()

        suspicious_terms = [
            "test listing",
            "do not bid",
            "placeholder",
            "template",
        ]
        for term in suspicious_terms:
            if term in title_lower:
                flags.append(f"suspicious_title:{term}")

        # Warn about potential issues
        if listing.current_price == 0:
            warnings.append("Zero price listing")

        if listing.bid_count > 50:
            warnings.append("High bid count - competitive item")

        return flags, warnings

    def _truncate_description(
        self, description: Optional[str], max_length: int = 500
    ) -> Optional[str]:
        """Truncate description to max length.

        Args:
            description: Full description.
            max_length: Maximum length.

        Returns:
            Truncated description or None.
        """
        if not description:
            return None

        if len(description) <= max_length:
            return description

        return description[: max_length - 3] + "..."

    def _hash_raw_data(self, raw: ListingData) -> str:
        """Create hash of raw data for change detection.

        Args:
            raw: Raw listing data.

        Returns:
            Hash string.
        """
        from hashlib import md5

        data_str = f"{raw.item_id}:{raw.current_price}:{raw.bid_count}:{raw.title}"
        return md5(data_str.encode()).hexdigest()[:16]
