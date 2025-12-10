"""Exposure tracking and limit enforcement.

Tracks pending bids, spending, and enforces hard limits to prevent
unexpected losses.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field
import structlog

logger = structlog.get_logger(__name__)


class ExposureStatus(str, Enum):
    """Status of exposure relative to limits."""

    SAFE = "safe"  # < 50% of limit
    WARNING = "warning"  # 50-80% of limit
    CRITICAL = "critical"  # 80-100% of limit
    EXCEEDED = "exceeded"  # > 100% of limit


class ExposureLimits(BaseModel):
    """Configurable exposure limits."""

    # Daily limits
    daily_max_spend: float = Field(100.0, ge=0, description="Max spend per day")
    daily_max_exposure: float = Field(200.0, ge=0, description="Max pending liability per day")
    daily_max_bids: int = Field(50, ge=0, description="Max bids per day")

    # Weekly limits
    weekly_max_spend: float = Field(500.0, ge=0, description="Max spend per week")
    weekly_max_bids: int = Field(200, ge=0, description="Max bids per week")

    # Per-session limits
    session_max_bids: int = Field(20, ge=0, description="Max bids per session")
    session_max_spend: float = Field(100.0, ge=0, description="Max spend per session")

    # Hard stop thresholds
    hard_stop_daily_loss: float = Field(150.0, ge=0, description="Hard stop if daily loss exceeds")
    hard_stop_consecutive_losses: int = Field(10, ge=0, description="Hard stop after N consecutive losses")

    # Warning thresholds (as percentage of limits)
    warning_threshold: float = Field(0.5, ge=0, le=1)  # 50%
    critical_threshold: float = Field(0.8, ge=0, le=1)  # 80%


@dataclass
class BidRecord:
    """Record of a single bid."""

    item_id: str
    amount: float
    timestamp: datetime
    status: str  # pending, won, lost, cancelled
    category: Optional[str] = None


@dataclass
class ExposureSnapshot:
    """Current exposure state snapshot."""

    timestamp: datetime
    daily_spent: float
    daily_pending: float
    daily_exposure: float  # spent + pending
    daily_bids: int
    weekly_spent: float
    weekly_bids: int
    session_spent: float
    session_bids: int
    consecutive_losses: int
    status: ExposureStatus
    limits: ExposureLimits
    can_bid: bool
    stop_reason: str = ""


class ExposureTracker:
    """Track and enforce exposure limits.

    Monitors:
    - Daily/weekly spending
    - Pending bid exposure
    - Bid counts
    - Win/loss streaks

    Enforces:
    - Soft limits (warnings)
    - Hard limits (stops)
    - Automatic pause on unusual activity
    """

    def __init__(self, limits: ExposureLimits):
        """Initialize exposure tracker.

        Args:
            limits: Exposure limit configuration.
        """
        self.limits = limits

        # Tracking state
        self._bids: list[BidRecord] = []
        self._session_start: datetime = datetime.now()
        self._last_daily_reset: datetime = datetime.now()
        self._last_weekly_reset: datetime = datetime.now()
        self._consecutive_losses: int = 0
        self._is_stopped: bool = False
        self._stop_reason: str = ""

    def record_bid(
        self,
        item_id: str,
        amount: float,
        category: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Record a new bid and check limits.

        Args:
            item_id: Item identifier.
            amount: Bid amount.
            category: Item category.

        Returns:
            Tuple of (allowed, reason if not allowed).
        """
        self._reset_periods_if_needed()

        # Check if we can bid
        can_bid, reason = self.can_place_bid(amount)
        if not can_bid:
            return False, reason

        # Record the bid
        record = BidRecord(
            item_id=item_id,
            amount=amount,
            timestamp=datetime.now(),
            status="pending",
            category=category,
        )
        self._bids.append(record)

        logger.info(
            "Bid recorded",
            item_id=item_id,
            amount=amount,
            pending_exposure=self._get_pending_exposure(),
        )

        return True, ""

    def record_win(self, item_id: str, final_price: float) -> None:
        """Record a won bid.

        Args:
            item_id: Item identifier.
            final_price: Final price paid.
        """
        for bid in self._bids:
            if bid.item_id == item_id and bid.status == "pending":
                bid.status = "won"
                bid.amount = final_price  # Update to actual price
                break

        self._consecutive_losses = 0

        logger.info("Bid won", item_id=item_id, price=final_price)

    def record_loss(self, item_id: str) -> None:
        """Record a lost bid.

        Args:
            item_id: Item identifier.
        """
        for bid in self._bids:
            if bid.item_id == item_id and bid.status == "pending":
                bid.status = "lost"
                break

        self._consecutive_losses += 1

        # Check consecutive loss limit
        if self._consecutive_losses >= self.limits.hard_stop_consecutive_losses:
            self._trigger_hard_stop(
                f"Consecutive loss limit ({self.limits.hard_stop_consecutive_losses}) reached"
            )

        logger.info(
            "Bid lost",
            item_id=item_id,
            consecutive_losses=self._consecutive_losses,
        )

    def record_cancel(self, item_id: str) -> None:
        """Record a cancelled bid.

        Args:
            item_id: Item identifier.
        """
        for bid in self._bids:
            if bid.item_id == item_id and bid.status == "pending":
                bid.status = "cancelled"
                break

    def can_place_bid(self, amount: float) -> tuple[bool, str]:
        """Check if a bid of given amount is allowed.

        Args:
            amount: Proposed bid amount.

        Returns:
            Tuple of (allowed, reason if not).
        """
        self._reset_periods_if_needed()

        if self._is_stopped:
            return False, f"Hard stop active: {self._stop_reason}"

        snapshot = self.get_snapshot()

        # Check daily exposure
        if snapshot.daily_exposure + amount > self.limits.daily_max_exposure:
            return False, f"Would exceed daily exposure limit (${self.limits.daily_max_exposure:.2f})"

        # Check daily spend
        if snapshot.daily_spent + amount > self.limits.daily_max_spend:
            return False, f"Would exceed daily spend limit (${self.limits.daily_max_spend:.2f})"

        # Check daily bid count
        if snapshot.daily_bids >= self.limits.daily_max_bids:
            return False, f"Daily bid limit ({self.limits.daily_max_bids}) reached"

        # Check weekly spend
        if snapshot.weekly_spent + amount > self.limits.weekly_max_spend:
            return False, f"Would exceed weekly spend limit (${self.limits.weekly_max_spend:.2f})"

        # Check weekly bid count
        if snapshot.weekly_bids >= self.limits.weekly_max_bids:
            return False, f"Weekly bid limit ({self.limits.weekly_max_bids}) reached"

        # Check session limits
        if snapshot.session_bids >= self.limits.session_max_bids:
            return False, f"Session bid limit ({self.limits.session_max_bids}) reached"

        if snapshot.session_spent + amount > self.limits.session_max_spend:
            return False, f"Would exceed session spend limit (${self.limits.session_max_spend:.2f})"

        return True, ""

    def get_snapshot(self) -> ExposureSnapshot:
        """Get current exposure snapshot.

        Returns:
            ExposureSnapshot with current state.
        """
        self._reset_periods_if_needed()

        now = datetime.now()
        today = now.date()
        week_start = now - timedelta(days=now.weekday())

        # Calculate metrics
        daily_spent = sum(
            b.amount for b in self._bids
            if b.timestamp.date() == today and b.status == "won"
        )
        daily_pending = sum(
            b.amount for b in self._bids
            if b.timestamp.date() == today and b.status == "pending"
        )
        daily_bids = sum(
            1 for b in self._bids
            if b.timestamp.date() == today
        )

        weekly_spent = sum(
            b.amount for b in self._bids
            if b.timestamp >= week_start and b.status == "won"
        )
        weekly_bids = sum(
            1 for b in self._bids
            if b.timestamp >= week_start
        )

        session_spent = sum(
            b.amount for b in self._bids
            if b.timestamp >= self._session_start and b.status == "won"
        )
        session_bids = sum(
            1 for b in self._bids
            if b.timestamp >= self._session_start
        )

        daily_exposure = daily_spent + daily_pending

        # Determine status
        status = self._calculate_status(daily_exposure, daily_spent)

        # Determine if can bid
        can_bid = not self._is_stopped and status != ExposureStatus.EXCEEDED

        return ExposureSnapshot(
            timestamp=now,
            daily_spent=daily_spent,
            daily_pending=daily_pending,
            daily_exposure=daily_exposure,
            daily_bids=daily_bids,
            weekly_spent=weekly_spent,
            weekly_bids=weekly_bids,
            session_spent=session_spent,
            session_bids=session_bids,
            consecutive_losses=self._consecutive_losses,
            status=status,
            limits=self.limits,
            can_bid=can_bid,
            stop_reason=self._stop_reason,
        )

    def _calculate_status(
        self, daily_exposure: float, daily_spent: float
    ) -> ExposureStatus:
        """Calculate current exposure status.

        Args:
            daily_exposure: Current daily exposure.
            daily_spent: Current daily spent.

        Returns:
            ExposureStatus.
        """
        if self._is_stopped:
            return ExposureStatus.EXCEEDED

        # Check against limits
        exposure_ratio = daily_exposure / self.limits.daily_max_exposure if self.limits.daily_max_exposure > 0 else 0
        spend_ratio = daily_spent / self.limits.daily_max_spend if self.limits.daily_max_spend > 0 else 0

        max_ratio = max(exposure_ratio, spend_ratio)

        if max_ratio >= 1.0:
            return ExposureStatus.EXCEEDED
        elif max_ratio >= self.limits.critical_threshold:
            return ExposureStatus.CRITICAL
        elif max_ratio >= self.limits.warning_threshold:
            return ExposureStatus.WARNING
        else:
            return ExposureStatus.SAFE

    def _trigger_hard_stop(self, reason: str) -> None:
        """Trigger hard stop.

        Args:
            reason: Reason for hard stop.
        """
        self._is_stopped = True
        self._stop_reason = reason
        logger.critical("HARD STOP TRIGGERED", reason=reason)

    def reset_hard_stop(self) -> None:
        """Reset hard stop (requires manual action)."""
        self._is_stopped = False
        self._stop_reason = ""
        logger.info("Hard stop reset")

    def start_new_session(self) -> None:
        """Start a new session, resetting session counters."""
        self._session_start = datetime.now()
        logger.info("New session started")

    def _reset_periods_if_needed(self) -> None:
        """Reset daily/weekly counters if period has passed."""
        now = datetime.now()

        # Daily reset
        if self._last_daily_reset.date() != now.date():
            self._last_daily_reset = now
            # Clear old daily data from bids list
            cutoff = now - timedelta(days=7)  # Keep last 7 days
            self._bids = [b for b in self._bids if b.timestamp >= cutoff]

        # Weekly reset
        if (now - self._last_weekly_reset).days >= 7:
            self._last_weekly_reset = now

    def get_summary(self) -> dict[str, Any]:
        """Get exposure summary for reporting.

        Returns:
            Summary dictionary.
        """
        snapshot = self.get_snapshot()
        return {
            "status": snapshot.status.value,
            "can_bid": snapshot.can_bid,
            "daily": {
                "spent": snapshot.daily_spent,
                "pending": snapshot.daily_pending,
                "exposure": snapshot.daily_exposure,
                "limit": self.limits.daily_max_exposure,
                "bids": snapshot.daily_bids,
                "bid_limit": self.limits.daily_max_bids,
            },
            "weekly": {
                "spent": snapshot.weekly_spent,
                "limit": self.limits.weekly_max_spend,
                "bids": snapshot.weekly_bids,
                "bid_limit": self.limits.weekly_max_bids,
            },
            "session": {
                "spent": snapshot.session_spent,
                "bids": snapshot.session_bids,
            },
            "consecutive_losses": snapshot.consecutive_losses,
            "hard_stop": self._is_stopped,
            "stop_reason": self._stop_reason,
        }
