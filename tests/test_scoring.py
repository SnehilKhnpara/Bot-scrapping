"""Tests for scoring engine and quality filter."""

import pytest
from datetime import datetime, timedelta

from src.scraper.normalizer import NormalizedListing, ItemCondition, ListingStatus
from src.scoring.quality_filter import QualityFilter, FilterConfig, FilterReason
from src.scoring.scoring_engine import ScoringEngine, ScoringConfig


def create_test_listing(**kwargs) -> NormalizedListing:
    """Create a test listing with defaults."""
    defaults = {
        "item_id": "test123",
        "title": "Test Item",
        "url": "https://example.com/item/123",
        "current_price": 50.0,
        "total_price": 55.0,
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


class TestQualityFilter:
    """Tests for QualityFilter."""

    def test_filter_passes_good_listing(self):
        """Good listings should pass the filter."""
        config = FilterConfig()
        filter = QualityFilter(config)
        listing = create_test_listing()

        result = filter.filter(listing)

        assert result.passed is True
        assert len(result.reasons) == 0

    def test_filter_rejects_low_seller_rating(self):
        """Listings with low seller rating should be rejected."""
        config = FilterConfig(min_seller_rating=95.0)
        filter = QualityFilter(config)
        listing = create_test_listing(seller_rating=85.0)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.LOW_SELLER_RATING in result.reasons

    def test_filter_rejects_blacklisted_category(self):
        """Listings in blacklisted categories should be rejected."""
        config = FilterConfig(blacklisted_categories=["weapons", "adult"])
        filter = QualityFilter(config)
        listing = create_test_listing(category="Weapons & Ammunition")

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.BLACKLISTED_CATEGORY in result.reasons

    def test_filter_rejects_high_price(self):
        """Listings above max price should be rejected."""
        config = FilterConfig(max_price=100.0)
        filter = QualityFilter(config)
        listing = create_test_listing(total_price=150.0)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.PRICE_TOO_HIGH in result.reasons

    def test_filter_rejects_low_price(self):
        """Listings below min price should be rejected."""
        config = FilterConfig(min_price=10.0)
        filter = QualityFilter(config)
        listing = create_test_listing(total_price=5.0)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.PRICE_TOO_LOW in result.reasons

    def test_filter_rejects_no_images(self):
        """Listings without images should be rejected if required."""
        config = FilterConfig(require_images=True)
        filter = QualityFilter(config)
        listing = create_test_listing(has_images=False)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.NO_IMAGES in result.reasons

    def test_filter_rejects_too_many_bids(self):
        """Listings with too many bids should be rejected."""
        config = FilterConfig(max_bid_count=20)
        filter = QualityFilter(config)
        listing = create_test_listing(bid_count=50)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.TOO_MANY_BIDS in result.reasons

    def test_filter_rejects_ending_too_soon(self):
        """Listings ending too soon should be rejected."""
        config = FilterConfig(min_time_remaining_seconds=600)
        filter = QualityFilter(config)
        listing = create_test_listing(time_remaining_seconds=60)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.ENDING_TOO_SOON in result.reasons

    def test_filter_rejects_bad_condition(self):
        """Listings with unacceptable condition should be rejected."""
        config = FilterConfig(
            acceptable_conditions=[ItemCondition.NEW, ItemCondition.LIKE_NEW]
        )
        filter = QualityFilter(config)
        listing = create_test_listing(condition=ItemCondition.FOR_PARTS)

        result = filter.filter(listing)

        assert result.passed is False
        assert FilterReason.BAD_CONDITION in result.reasons

    def test_filter_batch(self):
        """Batch filtering should work correctly."""
        config = FilterConfig(min_seller_rating=90.0)
        filter = QualityFilter(config)

        listings = [
            create_test_listing(item_id="1", seller_rating=95.0),
            create_test_listing(item_id="2", seller_rating=85.0),
            create_test_listing(item_id="3", seller_rating=92.0),
        ]

        passed, results = filter.filter_batch(listings)

        assert len(passed) == 2
        assert len(results) == 3
        assert passed[0].item_id == "1"
        assert passed[1].item_id == "3"


class TestScoringEngine:
    """Tests for ScoringEngine."""

    def test_score_good_listing(self):
        """Good listings should get high scores."""
        config = ScoringConfig(min_eligible_score=60.0)
        engine = ScoringEngine(config)
        listing = create_test_listing(
            seller_trust_score=98.0,
            condition=ItemCondition.NEW,
            bid_count=2,
        )

        score = engine.score(listing)

        assert score.total_score > 60.0
        assert score.eligible is True
        assert len(score.components) == 6

    def test_score_below_threshold(self):
        """Low quality listings should not be eligible."""
        config = ScoringConfig(min_eligible_score=70.0)
        engine = ScoringEngine(config)
        listing = create_test_listing(
            seller_trust_score=50.0,
            condition=ItemCondition.FOR_PARTS,
            bid_count=40,
        )

        score = engine.score(listing)

        assert score.eligible is False

    def test_score_components_sum_correctly(self):
        """Component weighted scores should sum to total."""
        config = ScoringConfig()
        engine = ScoringEngine(config)
        listing = create_test_listing()

        score = engine.score(listing)

        component_sum = sum(c.weighted_score for c in score.components)
        assert abs(score.total_score - component_sum) < 0.1

    def test_seller_trust_scoring(self):
        """Seller trust should affect score appropriately."""
        config = ScoringConfig()
        engine = ScoringEngine(config)

        high_trust = create_test_listing(seller_trust_score=98.0)
        low_trust = create_test_listing(seller_trust_score=60.0)

        high_score = engine.score(high_trust)
        low_score = engine.score(low_trust)

        # Find seller trust components
        high_trust_comp = next(c for c in high_score.components if c.name == "seller_trust")
        low_trust_comp = next(c for c in low_score.components if c.name == "seller_trust")

        assert high_trust_comp.score > low_trust_comp.score

    def test_competition_scoring(self):
        """More competition should lower score."""
        config = ScoringConfig()
        engine = ScoringEngine(config)

        low_bids = create_test_listing(bid_count=2)
        high_bids = create_test_listing(bid_count=40)

        low_score = engine.score(low_bids)
        high_score = engine.score(high_bids)

        low_comp = next(c for c in low_score.components if c.name == "competition")
        high_comp = next(c for c in high_score.components if c.name == "competition")

        assert low_comp.score > high_comp.score

    def test_batch_scoring(self):
        """Batch scoring should work and sort by score."""
        config = ScoringConfig(min_eligible_score=50.0)
        engine = ScoringEngine(config)

        listings = [
            create_test_listing(item_id="1", seller_trust_score=70.0),
            create_test_listing(item_id="2", seller_trust_score=90.0),
            create_test_listing(item_id="3", seller_trust_score=60.0),
        ]

        eligible, all_scores = engine.score_batch(listings)

        assert len(all_scores) == 3
        # Should be sorted by score descending
        if len(eligible) >= 2:
            assert eligible[0][1].total_score >= eligible[1][1].total_score


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
