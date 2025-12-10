"""Bid decision engine for calculating maximum bid values.

Implements business rules, budget constraints, and risk parameters
to determine safe bid amounts for eligible items.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_UP
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field
import structlog

from ..scraper.normalizer import NormalizedListing
from ..scoring.scoring_engine import ItemScore

logger = structlog.get_logger(__name__)


class BidRejectionReason(str, Enum):
    """Reasons for rejecting a bid."""

    BELOW_SCORE_THRESHOLD = "below_score_threshold"
    EXCEEDS_ITEM_MAX = "exceeds_item_max"
    EXCEEDS_DAILY_BUDGET = "exceeds_daily_budget"
    EXCEEDS_WEEKLY_BUDGET = "exceeds_weekly_budget"
    EXCEEDS_EXPOSURE_LIMIT = "exceeds_exposure_limit"
    EXCEEDS_CATEGORY_LIMIT = "exceeds_category_limit"
    MANUAL_OVERRIDE = "manual_override"
    TIME_CONSTRAINT = "time_constraint"
    RISK_TOO_HIGH = "risk_too_high"


@dataclass
class BidDecision:
    """Result of bid calculation for an item."""

    item_id: str
    should_bid: bool
    max_bid: Optional[float] = None
    recommended_bid: Optional[float] = None
    rejection_reason: Optional[BidRejectionReason] = None
    rejection_detail: str = ""
    calculation_breakdown: dict[str, Any] = field(default_factory=dict)
    risk_assessment: str = ""
    decided_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "item_id": self.item_id,
            "should_bid": self.should_bid,
            "max_bid": self.max_bid,
            "recommended_bid": self.recommended_bid,
            "rejection_reason": self.rejection_reason.value if self.rejection_reason else None,
            "rejection_detail": self.rejection_detail,
            "risk_assessment": self.risk_assessment,
            "decided_at": self.decided_at.isoformat(),
        }


class BidConfig(BaseModel):
    """Configuration for bid calculation."""

    # Budget limits
    daily_budget: float = Field(100.0, ge=0, description="Maximum spend per day")
    weekly_budget: float = Field(500.0, ge=0, description="Maximum spend per week")
    per_item_max: float = Field(50.0, ge=0, description="Maximum bid per item")

    # Exposure limits
    max_pending_bids: int = Field(10, ge=0, description="Max concurrent pending bids")
    max_daily_exposure: float = Field(200.0, ge=0, description="Max potential liability per day")

    # Category-specific limits
    category_max_bids: dict[str, float] = Field(
        default_factory=dict,
        description="Per-category max bid overrides",
    )
    category_daily_limits: dict[str, float] = Field(
        default_factory=dict,
        description="Per-category daily spend limits",
    )

    # Bid calculation parameters
    min_bid_increment: float = Field(0.50, ge=0, description="Minimum bid increment")
    bid_rounding: float = Field(0.01, ge=0.01, description="Round bids to this precision")

    # Risk parameters
    risk_multiplier_low: float = Field(1.0, ge=0, le=2, description="Multiplier for low-risk items")
    risk_multiplier_medium: float = Field(0.85, ge=0, le=2, description="Multiplier for medium-risk")
    risk_multiplier_high: float = Field(0.7, ge=0, le=2, description="Multiplier for high-risk")
    score_threshold: float = Field(60.0, ge=0, le=100, description="Min score to consider bidding")

    # Value-based bidding
    max_percent_of_value: float = Field(80.0, ge=0, le=150, description="Max % of estimated value to bid")
    target_percent_of_value: float = Field(65.0, ge=0, le=100, description="Target % of estimated value")

    # Manual overrides (item_id -> max_bid)
    item_overrides: dict[str, float] = Field(
        default_factory=dict,
        description="Manual max bid overrides by item ID",
    )

    class Config:
        """Pydantic config."""

        extra = "forbid"


class BidCalculator:
    """Calculate maximum safe bid amounts for listings.

    Implements a multi-factor decision system considering:
    - Item scores and eligibility
    - Budget constraints (daily, weekly, per-item)
    - Exposure limits
    - Category-specific rules
    - Risk assessment
    - Value-based pricing
    - Manual overrides
    """

    def __init__(
        self,
        config: BidConfig,
        price_estimator: Optional[callable] = None,
    ):
        """Initialize bid calculator.

        Args:
            config: Bid calculation configuration.
            price_estimator: Optional function to estimate item value.
        """
        self.config = config
        self.price_estimator = price_estimator

        # Tracking state
        self._daily_spent: float = 0.0
        self._weekly_spent: float = 0.0
        self._pending_bids: dict[str, float] = {}  # item_id -> bid amount
        self._category_daily_spent: dict[str, float] = {}
        self._last_reset_date: Optional[datetime] = None
        self._last_week_reset: Optional[datetime] = None

    def calculate_bid(
        self,
        listing: NormalizedListing,
        score: ItemScore,
    ) -> BidDecision:
        """Calculate bid decision for a listing.

        Args:
            listing: Normalized listing data.
            score: Item score from scoring engine.

        Returns:
            BidDecision with bid recommendation or rejection.
        """
        decision = BidDecision(item_id=listing.item_id, should_bid=False)

        # Check score threshold first
        if not score.eligible or score.total_score < self.config.score_threshold:
            decision.rejection_reason = BidRejectionReason.BELOW_SCORE_THRESHOLD
            decision.rejection_detail = (
                f"Score {score.total_score:.1f} below threshold {self.config.score_threshold}"
            )
            return decision

        # Check for manual override
        if listing.item_id in self.config.item_overrides:
            override_max = self.config.item_overrides[listing.item_id]
            if override_max <= 0:
                decision.rejection_reason = BidRejectionReason.MANUAL_OVERRIDE
                decision.rejection_detail = "Item manually excluded from bidding"
                return decision
            # Use override as hard cap
            decision.calculation_breakdown["manual_override"] = override_max

        # Calculate base max bid from value
        value_based_max = self._calculate_value_based_max(listing, score)
        decision.calculation_breakdown["value_based_max"] = value_based_max

        # Apply category limits
        category_max = self._get_category_max(listing.category)
        decision.calculation_breakdown["category_max"] = category_max

        # Apply risk multiplier
        risk_level, risk_multiplier = self._assess_risk(listing, score)
        decision.risk_assessment = risk_level
        decision.calculation_breakdown["risk_multiplier"] = risk_multiplier

        # Calculate adjusted max
        adjusted_max = min(
            value_based_max * risk_multiplier,
            self.config.per_item_max,
            category_max,
        )

        # Apply manual override if exists
        if listing.item_id in self.config.item_overrides:
            adjusted_max = min(adjusted_max, self.config.item_overrides[listing.item_id])

        # Check budget constraints
        budget_result = self._check_budget_constraints(listing, adjusted_max)
        if not budget_result["allowed"]:
            decision.rejection_reason = budget_result["reason"]
            decision.rejection_detail = budget_result["detail"]
            return decision

        final_max = min(adjusted_max, budget_result["available"])

        # Check if bid is meaningful
        if final_max < listing.current_price + self.config.min_bid_increment:
            decision.rejection_reason = BidRejectionReason.EXCEEDS_ITEM_MAX
            decision.rejection_detail = (
                f"Max bid ${final_max:.2f} doesn't exceed current ${listing.current_price:.2f} "
                f"+ increment ${self.config.min_bid_increment:.2f}"
            )
            return decision

        # Round bid
        final_max = self._round_bid(final_max)

        # Calculate recommended (starting) bid
        recommended = self._calculate_recommended_bid(listing, final_max)

        decision.should_bid = True
        decision.max_bid = final_max
        decision.recommended_bid = recommended
        decision.calculation_breakdown["final_max"] = final_max
        decision.calculation_breakdown["recommended"] = recommended

        logger.info(
            "Bid calculated",
            item_id=listing.item_id,
            max_bid=final_max,
            recommended=recommended,
            score=score.total_score,
        )

        return decision

    def _calculate_value_based_max(
        self,
        listing: NormalizedListing,
        score: ItemScore,
    ) -> float:
        """Calculate max bid based on item value.

        Args:
            listing: Listing data.
            score: Item score.

        Returns:
            Value-based maximum bid.
        """
        # Try to get estimated value
        estimated_value = None
        if self.price_estimator:
            estimated_value = self.price_estimator(listing)

        # Fall back to buy now price
        if not estimated_value and listing.buy_now_price:
            estimated_value = listing.buy_now_price

        # Fall back to current price with markup
        if not estimated_value:
            # Assume current price is roughly fair, add small margin
            estimated_value = listing.current_price * 1.2

        # Calculate max as percentage of value
        max_percent = self.config.max_percent_of_value
        target_percent = self.config.target_percent_of_value

        # Adjust percentages based on score
        # Higher scores get closer to max percent
        score_factor = (score.total_score - self.config.score_threshold) / (100 - self.config.score_threshold)
        score_factor = max(0, min(1, score_factor))

        effective_percent = target_percent + (max_percent - target_percent) * score_factor

        return estimated_value * (effective_percent / 100)

    def _get_category_max(self, category: Optional[str]) -> float:
        """Get maximum bid for category.

        Args:
            category: Item category.

        Returns:
            Category max bid or default.
        """
        if not category:
            return self.config.per_item_max

        category_lower = category.lower()
        for cat_pattern, max_bid in self.config.category_max_bids.items():
            if cat_pattern.lower() in category_lower:
                return max_bid

        return self.config.per_item_max

    def _assess_risk(
        self,
        listing: NormalizedListing,
        score: ItemScore,
    ) -> tuple[str, float]:
        """Assess bid risk level.

        Args:
            listing: Listing data.
            score: Item score.

        Returns:
            Tuple of (risk level string, risk multiplier).
        """
        risk_factors = 0

        # Seller trust
        if listing.seller_trust_score < 80:
            risk_factors += 1
        if listing.seller_trust_score < 60:
            risk_factors += 1

        # Competition
        if listing.bid_count > 20:
            risk_factors += 1
        if listing.bid_count > 40:
            risk_factors += 1

        # Time remaining
        if listing.time_remaining_seconds and listing.time_remaining_seconds < 600:
            risk_factors += 1  # < 10 minutes is risky

        # Score proximity to threshold
        if score.total_score < self.config.score_threshold + 10:
            risk_factors += 1

        # Determine level
        if risk_factors <= 1:
            return "low", self.config.risk_multiplier_low
        elif risk_factors <= 3:
            return "medium", self.config.risk_multiplier_medium
        else:
            return "high", self.config.risk_multiplier_high

    def _check_budget_constraints(
        self,
        listing: NormalizedListing,
        proposed_max: float,
    ) -> dict[str, Any]:
        """Check if bid fits within budget constraints.

        Args:
            listing: Listing data.
            proposed_max: Proposed maximum bid.

        Returns:
            Dict with 'allowed', 'available', 'reason', 'detail'.
        """
        self._reset_if_needed()

        # Calculate current exposure
        pending_exposure = sum(self._pending_bids.values())

        # Check daily budget
        remaining_daily = self.config.daily_budget - self._daily_spent
        if proposed_max > remaining_daily:
            if remaining_daily <= 0:
                return {
                    "allowed": False,
                    "available": 0,
                    "reason": BidRejectionReason.EXCEEDS_DAILY_BUDGET,
                    "detail": f"Daily budget ${self.config.daily_budget:.2f} exhausted",
                }

        # Check weekly budget
        remaining_weekly = self.config.weekly_budget - self._weekly_spent
        if proposed_max > remaining_weekly:
            if remaining_weekly <= 0:
                return {
                    "allowed": False,
                    "available": 0,
                    "reason": BidRejectionReason.EXCEEDS_WEEKLY_BUDGET,
                    "detail": f"Weekly budget ${self.config.weekly_budget:.2f} exhausted",
                }

        # Check exposure limit
        potential_exposure = pending_exposure + proposed_max
        if potential_exposure > self.config.max_daily_exposure:
            available = self.config.max_daily_exposure - pending_exposure
            if available <= 0:
                return {
                    "allowed": False,
                    "available": 0,
                    "reason": BidRejectionReason.EXCEEDS_EXPOSURE_LIMIT,
                    "detail": f"Exposure limit ${self.config.max_daily_exposure:.2f} reached",
                }

        # Check pending bid count
        if len(self._pending_bids) >= self.config.max_pending_bids:
            return {
                "allowed": False,
                "available": 0,
                "reason": BidRejectionReason.EXCEEDS_EXPOSURE_LIMIT,
                "detail": f"Max pending bids ({self.config.max_pending_bids}) reached",
            }

        # Check category daily limit
        if listing.category:
            cat_limit_result = self._check_category_limit(listing.category, proposed_max)
            if not cat_limit_result["allowed"]:
                return cat_limit_result

        # Calculate actual available
        available = min(
            remaining_daily,
            remaining_weekly,
            self.config.max_daily_exposure - pending_exposure,
        )

        return {
            "allowed": True,
            "available": available,
            "reason": None,
            "detail": "",
        }

    def _check_category_limit(
        self, category: str, proposed_max: float
    ) -> dict[str, Any]:
        """Check category-specific daily limit.

        Args:
            category: Item category.
            proposed_max: Proposed maximum bid.

        Returns:
            Constraint check result.
        """
        category_lower = category.lower()

        for cat_pattern, daily_limit in self.config.category_daily_limits.items():
            if cat_pattern.lower() in category_lower:
                current_spent = self._category_daily_spent.get(cat_pattern, 0)
                remaining = daily_limit - current_spent

                if proposed_max > remaining:
                    if remaining <= 0:
                        return {
                            "allowed": False,
                            "available": 0,
                            "reason": BidRejectionReason.EXCEEDS_CATEGORY_LIMIT,
                            "detail": f"Category '{cat_pattern}' daily limit ${daily_limit:.2f} exhausted",
                        }
                    return {
                        "allowed": True,
                        "available": remaining,
                        "reason": None,
                        "detail": "",
                    }
                break

        return {
            "allowed": True,
            "available": proposed_max,
            "reason": None,
            "detail": "",
        }

    def _calculate_recommended_bid(
        self,
        listing: NormalizedListing,
        max_bid: float,
    ) -> float:
        """Calculate recommended starting bid.

        Args:
            listing: Listing data.
            max_bid: Maximum bid amount.

        Returns:
            Recommended bid amount.
        """
        # Start just above current price
        min_needed = listing.current_price + self.config.min_bid_increment

        # Don't exceed max
        if min_needed >= max_bid:
            return self._round_bid(max_bid)

        # Start conservative - about 10-20% above current
        conservative = listing.current_price * 1.15

        recommended = max(min_needed, min(conservative, max_bid))
        return self._round_bid(recommended)

    def _round_bid(self, amount: float) -> float:
        """Round bid to configured precision.

        Args:
            amount: Bid amount.

        Returns:
            Rounded bid amount.
        """
        precision = Decimal(str(self.config.bid_rounding))
        rounded = Decimal(str(amount)).quantize(precision, rounding=ROUND_UP)
        return float(rounded)

    def _reset_if_needed(self) -> None:
        """Reset daily/weekly counters if needed."""
        now = datetime.now()

        # Daily reset
        if self._last_reset_date is None or self._last_reset_date.date() != now.date():
            self._daily_spent = 0.0
            self._category_daily_spent = {}
            self._last_reset_date = now

        # Weekly reset (Monday)
        if self._last_week_reset is None or (now - self._last_week_reset).days >= 7:
            self._weekly_spent = 0.0
            self._last_week_reset = now

    def record_bid(self, item_id: str, amount: float, category: Optional[str] = None) -> None:
        """Record a bid for tracking.

        Args:
            item_id: Item identifier.
            amount: Bid amount.
            category: Item category.
        """
        self._pending_bids[item_id] = amount

        if category:
            cat_lower = category.lower()
            for cat_pattern in self.config.category_daily_limits:
                if cat_pattern.lower() in cat_lower:
                    self._category_daily_spent[cat_pattern] = (
                        self._category_daily_spent.get(cat_pattern, 0) + amount
                    )
                    break

    def record_win(self, item_id: str, final_price: float) -> None:
        """Record a won bid.

        Args:
            item_id: Item identifier.
            final_price: Final price paid.
        """
        if item_id in self._pending_bids:
            del self._pending_bids[item_id]

        self._daily_spent += final_price
        self._weekly_spent += final_price

    def record_loss(self, item_id: str) -> None:
        """Record a lost bid.

        Args:
            item_id: Item identifier.
        """
        if item_id in self._pending_bids:
            del self._pending_bids[item_id]

    def get_budget_status(self) -> dict[str, Any]:
        """Get current budget status.

        Returns:
            Dict with budget utilization info.
        """
        self._reset_if_needed()

        pending_exposure = sum(self._pending_bids.values())

        return {
            "daily_budget": self.config.daily_budget,
            "daily_spent": self._daily_spent,
            "daily_remaining": self.config.daily_budget - self._daily_spent,
            "weekly_budget": self.config.weekly_budget,
            "weekly_spent": self._weekly_spent,
            "weekly_remaining": self.config.weekly_budget - self._weekly_spent,
            "pending_bids": len(self._pending_bids),
            "pending_exposure": pending_exposure,
            "exposure_limit": self.config.max_daily_exposure,
            "exposure_remaining": self.config.max_daily_exposure - pending_exposure,
        }
