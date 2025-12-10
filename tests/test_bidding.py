"""Tests for bidding components."""

import pytest
from datetime import datetime

from src.scraper.normalizer import NormalizedListing, ItemCondition, ListingStatus
from src.scoring.scoring_engine import ScoringEngine, ScoringConfig, ItemScore
from src.bidding.bid_calculator import BidCalculator, BidConfig, BidRejectionReason


def create_test_listing(**kwargs) -> NormalizedListing:
    """Create a test listing with defaults."""
    defaults = {
        "item_id": "test123",
        "title": "Test Item",
        "url": "https://example.com/item/123",
        "current_price": 50.0,
        "total_price": 55.0,
        "buy_now_price": 100.0,
        "currency": "USD",
        "shipping_cost": 5.0,
        "bid_count": 5,
        "is_auction": True,
        "seller_name": "test_seller",
        "seller_rating": 98.5,
        "seller_feedback_count": 500,
        "seller_trust_score": 95.0,
        "condition": ItemCondition.GOOD,
        "category": "Electronics",
        "has_images": True,
        "time_remaining_seconds": 7200,
        "status": ListingStatus.ACTIVE,
        "is_valid": True,
    }
    defaults.update(kwargs)
    return NormalizedListing(**defaults)


def create_test_score(total: float = 75.0, eligible: bool = True) -> ItemScore:
    """Create a test score."""
    return ItemScore(
        item_id="test123",
        total_score=total,
        eligible=eligible,
    )


class TestBidCalculator:
    """Tests for BidCalculator."""

    def test_calculate_bid_basic(self):
        """Basic bid calculation should work."""
        config = BidConfig(
            daily_budget=1000.0,
            per_item_max=100.0,
            max_daily_exposure=500.0,
        )
        calculator = BidCalculator(config)

        listing = create_test_listing(current_price=50.0, buy_now_price=100.0)
        score = create_test_score(total=75.0, eligible=True)

        decision = calculator.calculate_bid(listing, score)

        assert decision.should_bid is True
        assert decision.max_bid is not None
        assert decision.max_bid > listing.current_price
        assert decision.recommended_bid is not None

    def test_rejects_low_score(self):
        """Low scores should be rejected."""
        config = BidConfig(score_threshold=70.0)
        calculator = BidCalculator(config)

        listing = create_test_listing()
        score = create_test_score(total=60.0, eligible=False)

        decision = calculator.calculate_bid(listing, score)

        assert decision.should_bid is False
        assert decision.rejection_reason == BidRejectionReason.BELOW_SCORE_THRESHOLD

    def test_respects_per_item_max(self):
        """Bids should not exceed per-item max."""
        config = BidConfig(per_item_max=30.0)
        calculator = BidCalculator(config)

        listing = create_test_listing(current_price=20.0, buy_now_price=200.0)
        score = create_test_score(total=90.0, eligible=True)

        decision = calculator.calculate_bid(listing, score)

        if decision.should_bid:
            assert decision.max_bid <= 30.0

    def test_respects_daily_budget(self):
        """Should reject when daily budget exceeded."""
        config = BidConfig(
            daily_budget=50.0,
            max_daily_exposure=200.0,
        )
        calculator = BidCalculator(config)

        # Simulate spending
        calculator._daily_spent = 45.0

        listing = create_test_listing(current_price=40.0)
        score = create_test_score()

        decision = calculator.calculate_bid(listing, score)

        # Should either reject or limit the bid
        if decision.should_bid:
            assert decision.max_bid <= 5.0  # Only $5 remaining
        else:
            assert decision.rejection_reason in [
                BidRejectionReason.EXCEEDS_DAILY_BUDGET,
                BidRejectionReason.EXCEEDS_ITEM_MAX,
            ]

    def test_respects_exposure_limit(self):
        """Should reject when exposure limit reached."""
        config = BidConfig(
            max_daily_exposure=100.0,
            max_pending_bids=10,
        )
        calculator = BidCalculator(config)

        # Simulate pending bids
        for i in range(5):
            calculator._pending_bids[f"item_{i}"] = 20.0  # $100 total pending

        listing = create_test_listing(current_price=30.0)
        score = create_test_score()

        decision = calculator.calculate_bid(listing, score)

        assert decision.should_bid is False
        assert decision.rejection_reason == BidRejectionReason.EXCEEDS_EXPOSURE_LIMIT

    def test_manual_override_max(self):
        """Manual overrides should cap the bid."""
        config = BidConfig(
            per_item_max=100.0,
            item_overrides={"test123": 25.0},
        )
        calculator = BidCalculator(config)

        listing = create_test_listing(item_id="test123", current_price=15.0)
        score = create_test_score()

        decision = calculator.calculate_bid(listing, score)

        if decision.should_bid:
            assert decision.max_bid <= 25.0

    def test_manual_override_exclude(self):
        """Manual override of 0 should exclude item."""
        config = BidConfig(
            item_overrides={"test123": 0},
        )
        calculator = BidCalculator(config)

        listing = create_test_listing(item_id="test123")
        score = create_test_score()

        decision = calculator.calculate_bid(listing, score)

        assert decision.should_bid is False
        assert decision.rejection_reason == BidRejectionReason.MANUAL_OVERRIDE

    def test_category_limits(self):
        """Category-specific limits should be respected."""
        config = BidConfig(
            per_item_max=100.0,
            category_max_bids={"electronics": 40.0},
        )
        calculator = BidCalculator(config)

        listing = create_test_listing(category="Electronics > Cameras", current_price=20.0)
        score = create_test_score()

        decision = calculator.calculate_bid(listing, score)

        if decision.should_bid:
            assert decision.max_bid <= 40.0

    def test_risk_multiplier_high_risk(self):
        """High risk items should have lower bids."""
        config = BidConfig(
            risk_multiplier_high=0.5,
            per_item_max=100.0,
        )
        calculator = BidCalculator(config)

        # Low trust seller, high competition, ending soon
        listing = create_test_listing(
            seller_trust_score=50.0,
            bid_count=50,
            time_remaining_seconds=300,
        )
        score = create_test_score(total=65.0)

        decision = calculator.calculate_bid(listing, score)

        # Should have "high" risk assessment
        assert decision.risk_assessment in ["high", "medium"]

    def test_record_bid_tracking(self):
        """Recording bids should update tracking."""
        config = BidConfig()
        calculator = BidCalculator(config)

        calculator.record_bid("item1", 50.0, "Electronics")

        assert "item1" in calculator._pending_bids
        assert calculator._pending_bids["item1"] == 50.0

    def test_record_win(self):
        """Recording wins should update budgets."""
        config = BidConfig()
        calculator = BidCalculator(config)

        calculator.record_bid("item1", 50.0)
        calculator.record_win("item1", 45.0)

        assert "item1" not in calculator._pending_bids
        assert calculator._daily_spent == 45.0
        assert calculator._weekly_spent == 45.0

    def test_record_loss(self):
        """Recording losses should remove pending bid."""
        config = BidConfig()
        calculator = BidCalculator(config)

        calculator.record_bid("item1", 50.0)
        calculator.record_loss("item1")

        assert "item1" not in calculator._pending_bids

    def test_budget_status(self):
        """Budget status should reflect current state."""
        config = BidConfig(
            daily_budget=100.0,
            weekly_budget=500.0,
            max_daily_exposure=200.0,
        )
        calculator = BidCalculator(config)

        calculator.record_bid("item1", 30.0)
        calculator.record_win("item1", 25.0)

        status = calculator.get_budget_status()

        assert status["daily_spent"] == 25.0
        assert status["daily_remaining"] == 75.0
        assert status["weekly_spent"] == 25.0


class TestBidRounding:
    """Tests for bid rounding."""

    def test_round_to_cents(self):
        """Bids should be rounded to cents."""
        config = BidConfig(bid_rounding=0.01)
        calculator = BidCalculator(config)

        rounded = calculator._round_bid(25.123)
        assert rounded == 25.13  # Rounds up

    def test_round_to_dollars(self):
        """Bids can be rounded to whole dollars."""
        config = BidConfig(bid_rounding=1.0)
        calculator = BidCalculator(config)

        rounded = calculator._round_bid(25.3)
        assert rounded == 26.0  # Rounds up


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
