"""Tests for safety controls."""

import pytest
from datetime import datetime, timedelta

from src.safety.exposure_tracker import ExposureTracker, ExposureLimits, ExposureStatus


class TestExposureTracker:
    """Tests for ExposureTracker."""

    def test_initial_state(self):
        """Initial state should allow bidding."""
        limits = ExposureLimits(
            daily_max_spend=100.0,
            daily_max_exposure=200.0,
        )
        tracker = ExposureTracker(limits)

        snapshot = tracker.get_snapshot()

        assert snapshot.status == ExposureStatus.SAFE
        assert snapshot.can_bid is True
        assert snapshot.daily_spent == 0
        assert snapshot.daily_pending == 0

    def test_record_bid(self):
        """Recording bids should update exposure."""
        limits = ExposureLimits(daily_max_exposure=200.0)
        tracker = ExposureTracker(limits)

        allowed, reason = tracker.record_bid("item1", 50.0)

        assert allowed is True
        snapshot = tracker.get_snapshot()
        assert snapshot.daily_pending == 50.0

    def test_exposure_limit_enforcement(self):
        """Should reject bids that exceed exposure limit."""
        limits = ExposureLimits(
            daily_max_exposure=100.0,
            daily_max_spend=1000.0,
        )
        tracker = ExposureTracker(limits)

        # Record some bids
        tracker.record_bid("item1", 50.0)
        tracker.record_bid("item2", 40.0)

        # This should be rejected
        allowed, reason = tracker.record_bid("item3", 20.0)

        assert allowed is False
        assert "exposure" in reason.lower()

    def test_daily_spend_limit(self):
        """Should reject when daily spend exceeded."""
        limits = ExposureLimits(
            daily_max_spend=50.0,
            daily_max_exposure=200.0,
        )
        tracker = ExposureTracker(limits)

        # Record some wins
        tracker.record_bid("item1", 30.0)
        tracker.record_win("item1", 30.0)

        tracker.record_bid("item2", 15.0)
        tracker.record_win("item2", 15.0)

        # Should be rejected - would exceed daily spend
        allowed, reason = tracker.record_bid("item3", 10.0)

        assert allowed is False
        assert "daily" in reason.lower()

    def test_session_limits(self):
        """Session limits should be enforced."""
        limits = ExposureLimits(
            session_max_bids=3,
            session_max_spend=100.0,
        )
        tracker = ExposureTracker(limits)

        tracker.record_bid("item1", 10.0)
        tracker.record_bid("item2", 10.0)
        tracker.record_bid("item3", 10.0)

        # Fourth bid should be rejected
        allowed, reason = tracker.record_bid("item4", 10.0)

        assert allowed is False
        assert "session" in reason.lower()

    def test_consecutive_losses_hard_stop(self):
        """Should trigger hard stop on consecutive losses."""
        limits = ExposureLimits(hard_stop_consecutive_losses=3)
        tracker = ExposureTracker(limits)

        # Record losses
        tracker.record_bid("item1", 10.0)
        tracker.record_loss("item1")
        tracker.record_bid("item2", 10.0)
        tracker.record_loss("item2")
        tracker.record_bid("item3", 10.0)
        tracker.record_loss("item3")

        snapshot = tracker.get_snapshot()
        assert snapshot.can_bid is False

    def test_win_resets_consecutive_losses(self):
        """Winning should reset consecutive loss counter."""
        limits = ExposureLimits(hard_stop_consecutive_losses=5)
        tracker = ExposureTracker(limits)

        # Record some losses
        tracker.record_bid("item1", 10.0)
        tracker.record_loss("item1")
        tracker.record_bid("item2", 10.0)
        tracker.record_loss("item2")

        # Record a win
        tracker.record_bid("item3", 10.0)
        tracker.record_win("item3", 10.0)

        snapshot = tracker.get_snapshot()
        assert snapshot.consecutive_losses == 0

    def test_exposure_status_levels(self):
        """Exposure status should reflect usage levels."""
        limits = ExposureLimits(
            daily_max_exposure=100.0,
            warning_threshold=0.5,
            critical_threshold=0.8,
        )
        tracker = ExposureTracker(limits)

        # Safe level
        tracker.record_bid("item1", 30.0)
        assert tracker.get_snapshot().status == ExposureStatus.SAFE

        # Warning level
        tracker.record_bid("item2", 30.0)  # 60% exposure
        assert tracker.get_snapshot().status == ExposureStatus.WARNING

        # Critical level
        tracker.record_bid("item3", 25.0)  # 85% exposure
        assert tracker.get_snapshot().status == ExposureStatus.CRITICAL

    def test_new_session_resets_session_counters(self):
        """Starting new session should reset session counters."""
        limits = ExposureLimits(session_max_bids=5)
        tracker = ExposureTracker(limits)

        tracker.record_bid("item1", 10.0)
        tracker.record_bid("item2", 10.0)

        tracker.start_new_session()

        snapshot = tracker.get_snapshot()
        assert snapshot.session_bids == 0

    def test_hard_stop_reset(self):
        """Hard stop should be manually resettable."""
        limits = ExposureLimits(hard_stop_consecutive_losses=1)
        tracker = ExposureTracker(limits)

        tracker.record_bid("item1", 10.0)
        tracker.record_loss("item1")

        assert tracker.get_snapshot().can_bid is False

        tracker.reset_hard_stop()

        assert tracker.get_snapshot().can_bid is True

    def test_summary_report(self):
        """Summary should contain relevant data."""
        limits = ExposureLimits(
            daily_max_exposure=100.0,
            daily_max_bids=10,
        )
        tracker = ExposureTracker(limits)

        tracker.record_bid("item1", 20.0)
        tracker.record_win("item1", 20.0)

        summary = tracker.get_summary()

        assert "status" in summary
        assert "daily" in summary
        assert summary["daily"]["spent"] == 20.0
        assert summary["daily"]["limit"] == 100.0


class TestCanPlaceBid:
    """Tests for can_place_bid checking."""

    def test_can_bid_when_within_limits(self):
        """Should allow bidding within all limits."""
        limits = ExposureLimits(
            daily_max_spend=100.0,
            daily_max_exposure=200.0,
            daily_max_bids=10,
        )
        tracker = ExposureTracker(limits)

        can_bid, reason = tracker.can_place_bid(50.0)

        assert can_bid is True
        assert reason == ""

    def test_cannot_bid_after_hard_stop(self):
        """Cannot bid after hard stop triggered."""
        limits = ExposureLimits()
        tracker = ExposureTracker(limits)
        tracker._trigger_hard_stop("Test stop")

        can_bid, reason = tracker.can_place_bid(10.0)

        assert can_bid is False
        assert "hard stop" in reason.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
