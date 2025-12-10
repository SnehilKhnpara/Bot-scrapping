"""Item scoring engine for bid eligibility ranking.

Calculates scores (0-100) for filtered items to determine
bidding priority and eligibility thresholds.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
import structlog

from ..scraper.normalizer import NormalizedListing, ItemCondition

logger = structlog.get_logger(__name__)


@dataclass
class ScoreComponent:
    """Individual scoring component result."""

    name: str
    score: float  # 0-100
    weight: float  # 0-1
    weighted_score: float  # score * weight
    explanation: str


@dataclass
class ItemScore:
    """Complete scoring result for an item."""

    item_id: str
    total_score: float  # 0-100
    eligible: bool
    components: list[ScoreComponent] = field(default_factory=list)
    explanation: str = ""
    scored_at: datetime = field(default_factory=datetime.now)

    def add_component(
        self,
        name: str,
        score: float,
        weight: float,
        explanation: str,
    ) -> None:
        """Add a scoring component."""
        weighted = score * weight
        self.components.append(
            ScoreComponent(
                name=name,
                score=score,
                weight=weight,
                weighted_score=weighted,
                explanation=explanation,
            )
        )


class ScoringConfig(BaseModel):
    """Configuration for the scoring engine."""

    # Minimum score threshold for eligibility
    min_eligible_score: float = Field(60.0, ge=0, le=100)

    # Component weights (should sum to 1.0)
    weight_seller_trust: float = Field(0.20, ge=0, le=1)
    weight_price_value: float = Field(0.25, ge=0, le=1)
    weight_condition: float = Field(0.15, ge=0, le=1)
    weight_competition: float = Field(0.15, ge=0, le=1)
    weight_time_urgency: float = Field(0.10, ge=0, le=1)
    weight_category_relevance: float = Field(0.15, ge=0, le=1)

    # Price scoring parameters
    target_discount_percent: float = Field(20.0, ge=0, le=100)
    max_discount_percent: float = Field(50.0, ge=0, le=100)

    # Time urgency parameters
    optimal_time_remaining_hours: float = Field(2.0, ge=0)
    min_time_remaining_minutes: int = Field(5, ge=0)

    # Competition scoring
    ideal_bid_count: int = Field(5, ge=0)
    max_acceptable_bids: int = Field(30, ge=0)

    # Category preferences (category -> priority 0-100)
    category_priorities: dict[str, float] = Field(default_factory=dict)
    default_category_score: float = Field(50.0, ge=0, le=100)

    # Condition scores (overrides defaults)
    condition_scores: dict[str, float] = Field(default_factory=dict)

    class Config:
        """Pydantic config."""

        extra = "forbid"


class ScoringEngine:
    """Calculate eligibility scores for marketplace listings.

    Produces a score from 0-100 based on multiple weighted factors:
    - Seller trustworthiness
    - Price/value ratio
    - Item condition
    - Competition level
    - Time urgency
    - Category relevance

    Scores above the threshold are eligible for bidding consideration.
    """

    # Default condition scores
    DEFAULT_CONDITION_SCORES = {
        ItemCondition.NEW.value: 100,
        ItemCondition.LIKE_NEW.value: 90,
        ItemCondition.VERY_GOOD.value: 75,
        ItemCondition.GOOD.value: 60,
        ItemCondition.ACCEPTABLE.value: 40,
        ItemCondition.FOR_PARTS.value: 10,
        ItemCondition.UNKNOWN.value: 30,
    }

    def __init__(
        self,
        config: ScoringConfig,
        price_estimator: Optional[Callable[[NormalizedListing], Optional[float]]] = None,
    ):
        """Initialize scoring engine.

        Args:
            config: Scoring configuration.
            price_estimator: Optional function to estimate fair market value.
                Takes a listing and returns estimated price or None.
        """
        self.config = config
        self.price_estimator = price_estimator
        self._validate_weights()

    def _validate_weights(self) -> None:
        """Validate that weights sum to approximately 1.0."""
        total = (
            self.config.weight_seller_trust
            + self.config.weight_price_value
            + self.config.weight_condition
            + self.config.weight_competition
            + self.config.weight_time_urgency
            + self.config.weight_category_relevance
        )
        if not 0.99 <= total <= 1.01:
            logger.warning(
                "Scoring weights do not sum to 1.0",
                total=total,
            )

    def score(self, listing: NormalizedListing) -> ItemScore:
        """Calculate score for a listing.

        Args:
            listing: Normalized listing to score.

        Returns:
            ItemScore with total and component scores.
        """
        result = ItemScore(
            item_id=listing.item_id,
            total_score=0,
            eligible=False,
        )

        # Calculate each component
        self._score_seller_trust(listing, result)
        self._score_price_value(listing, result)
        self._score_condition(listing, result)
        self._score_competition(listing, result)
        self._score_time_urgency(listing, result)
        self._score_category_relevance(listing, result)

        # Calculate total
        result.total_score = sum(c.weighted_score for c in result.components)
        result.total_score = round(min(100, max(0, result.total_score)), 2)

        # Determine eligibility
        result.eligible = result.total_score >= self.config.min_eligible_score

        # Generate explanation
        result.explanation = self._generate_explanation(result)

        logger.debug(
            "Item scored",
            item_id=listing.item_id,
            score=result.total_score,
            eligible=result.eligible,
        )

        return result

    def score_batch(
        self, listings: list[NormalizedListing]
    ) -> tuple[list[tuple[NormalizedListing, ItemScore]], list[ItemScore]]:
        """Score a batch of listings.

        Args:
            listings: List of listings to score.

        Returns:
            Tuple of (eligible listings with scores, all scores).
        """
        eligible = []
        all_scores = []

        for listing in listings:
            score = self.score(listing)
            all_scores.append(score)
            if score.eligible:
                eligible.append((listing, score))

        # Sort eligible by score (highest first)
        eligible.sort(key=lambda x: x[1].total_score, reverse=True)

        logger.info(
            "Batch scoring complete",
            total=len(listings),
            eligible=len(eligible),
        )

        return eligible, all_scores

    def _score_seller_trust(
        self, listing: NormalizedListing, result: ItemScore
    ) -> None:
        """Calculate seller trust score component."""
        score = listing.seller_trust_score

        # Boost for high feedback count
        if listing.seller_feedback_count:
            if listing.seller_feedback_count > 1000:
                score = min(100, score + 5)
            elif listing.seller_feedback_count > 500:
                score = min(100, score + 3)

        explanation = f"Seller trust: {listing.seller_trust_score:.1f}"
        if listing.seller_rating:
            explanation += f" (rating: {listing.seller_rating:.1f}%)"
        if listing.seller_feedback_count:
            explanation += f", {listing.seller_feedback_count} reviews"

        result.add_component(
            name="seller_trust",
            score=score,
            weight=self.config.weight_seller_trust,
            explanation=explanation,
        )

    def _score_price_value(
        self, listing: NormalizedListing, result: ItemScore
    ) -> None:
        """Calculate price/value score component."""
        current_price = listing.total_price

        # Try to get estimated value
        estimated_value = None
        if self.price_estimator:
            estimated_value = self.price_estimator(listing)

        # If no estimate, use buy now price as reference
        if not estimated_value and listing.buy_now_price:
            estimated_value = listing.buy_now_price

        if estimated_value and estimated_value > 0:
            # Calculate discount percentage
            discount_pct = ((estimated_value - current_price) / estimated_value) * 100

            if discount_pct >= self.config.max_discount_percent:
                score = 100
            elif discount_pct >= self.config.target_discount_percent:
                # Linear scale from target to max
                range_size = self.config.max_discount_percent - self.config.target_discount_percent
                position = (discount_pct - self.config.target_discount_percent) / range_size
                score = 70 + (position * 30)
            elif discount_pct > 0:
                # Below target but still a discount
                score = 50 + (discount_pct / self.config.target_discount_percent) * 20
            else:
                # No discount or premium
                score = max(0, 50 + discount_pct)  # Negative discount reduces score

            explanation = f"Price ${current_price:.2f} vs estimated ${estimated_value:.2f} ({discount_pct:.1f}% discount)"
        else:
            # No value estimate available - neutral score
            score = 50
            explanation = f"Price ${current_price:.2f} (no value estimate)"

        result.add_component(
            name="price_value",
            score=score,
            weight=self.config.weight_price_value,
            explanation=explanation,
        )

    def _score_condition(
        self, listing: NormalizedListing, result: ItemScore
    ) -> None:
        """Calculate condition score component."""
        condition_key = listing.condition.value

        # Check custom scores first
        if condition_key in self.config.condition_scores:
            score = self.config.condition_scores[condition_key]
        else:
            score = self.DEFAULT_CONDITION_SCORES.get(condition_key, 30)

        explanation = f"Condition: {listing.condition.value} ({score}/100)"

        result.add_component(
            name="condition",
            score=score,
            weight=self.config.weight_condition,
            explanation=explanation,
        )

    def _score_competition(
        self, listing: NormalizedListing, result: ItemScore
    ) -> None:
        """Calculate competition level score component."""
        bid_count = listing.bid_count

        if bid_count <= self.config.ideal_bid_count:
            # Low competition is good
            score = 100 - (bid_count * 5)  # Small penalty per bid
        elif bid_count <= self.config.max_acceptable_bids:
            # Moderate competition
            range_size = self.config.max_acceptable_bids - self.config.ideal_bid_count
            excess_bids = bid_count - self.config.ideal_bid_count
            score = 75 - (excess_bids / range_size) * 50
        else:
            # High competition
            excess = bid_count - self.config.max_acceptable_bids
            score = max(0, 25 - excess)

        explanation = f"{bid_count} bids (ideal: {self.config.ideal_bid_count})"

        result.add_component(
            name="competition",
            score=score,
            weight=self.config.weight_competition,
            explanation=explanation,
        )

    def _score_time_urgency(
        self, listing: NormalizedListing, result: ItemScore
    ) -> None:
        """Calculate time urgency score component."""
        if listing.time_remaining_seconds is None:
            # No time info - neutral score
            result.add_component(
                name="time_urgency",
                score=50,
                weight=self.config.weight_time_urgency,
                explanation="No time information available",
            )
            return

        hours_remaining = listing.time_remaining_seconds / 3600
        min_hours = self.config.min_time_remaining_minutes / 60

        if hours_remaining < min_hours:
            # Too soon - risky
            score = 20
            explanation = f"Only {listing.time_remaining_seconds}s remaining - too risky"
        elif hours_remaining <= self.config.optimal_time_remaining_hours:
            # Optimal window - high score
            score = 90 + (10 * (1 - hours_remaining / self.config.optimal_time_remaining_hours))
            explanation = f"{hours_remaining:.1f}h remaining - optimal timing"
        elif hours_remaining <= self.config.optimal_time_remaining_hours * 4:
            # Reasonable window
            score = 70
            explanation = f"{hours_remaining:.1f}h remaining - good timing"
        else:
            # Too far out
            score = 50 - min(30, (hours_remaining - self.config.optimal_time_remaining_hours * 4) / 10)
            explanation = f"{hours_remaining:.1f}h remaining - may need to wait"

        result.add_component(
            name="time_urgency",
            score=max(0, score),
            weight=self.config.weight_time_urgency,
            explanation=explanation,
        )

    def _score_category_relevance(
        self, listing: NormalizedListing, result: ItemScore
    ) -> None:
        """Calculate category relevance score component."""
        if not listing.category:
            result.add_component(
                name="category_relevance",
                score=self.config.default_category_score,
                weight=self.config.weight_category_relevance,
                explanation="No category information",
            )
            return

        # Check for matching category priorities
        category_lower = listing.category.lower()
        score = self.config.default_category_score

        for cat_pattern, priority in self.config.category_priorities.items():
            if cat_pattern.lower() in category_lower:
                score = priority
                break

        explanation = f"Category: {listing.category} (priority: {score})"

        result.add_component(
            name="category_relevance",
            score=score,
            weight=self.config.weight_category_relevance,
            explanation=explanation,
        )

    def _generate_explanation(self, result: ItemScore) -> str:
        """Generate human-readable explanation of score.

        Args:
            result: ItemScore with components.

        Returns:
            Explanation string.
        """
        lines = [
            f"Total Score: {result.total_score:.1f}/100",
            f"Eligible: {'Yes' if result.eligible else 'No'}",
            "",
            "Components:",
        ]

        for component in sorted(result.components, key=lambda c: c.weighted_score, reverse=True):
            lines.append(
                f"  - {component.name}: {component.score:.1f} x {component.weight:.2f} = {component.weighted_score:.2f}"
            )
            lines.append(f"    {component.explanation}")

        return "\n".join(lines)
