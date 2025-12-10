"""Alert and notification system.

Sends notifications for important events via multiple channels
(Telegram, email, etc.).
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
import structlog

logger = structlog.get_logger(__name__)


class AlertType(str, Enum):
    """Types of alerts."""

    BID_PLACED = "bid_placed"
    BID_WON = "bid_won"
    BID_LOST = "bid_lost"
    BID_FAILED = "bid_failed"
    EXPOSURE_WARNING = "exposure_warning"
    EXPOSURE_CRITICAL = "exposure_critical"
    HARD_STOP = "hard_stop"
    SESSION_ERROR = "session_error"
    AUTH_FAILURE = "auth_failure"
    RATE_LIMIT = "rate_limit"
    SYSTEM_ERROR = "system_error"
    DAILY_SUMMARY = "daily_summary"


class AlertPriority(str, Enum):
    """Alert priority levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Single alert instance."""

    alert_type: AlertType
    priority: AlertPriority
    title: str
    message: str
    timestamp: datetime
    data: dict[str, Any]
    sent: bool = False
    sent_at: Optional[datetime] = None
    error: str = ""


class AlertConfig(BaseModel):
    """Alert configuration."""

    enabled: bool = Field(True, description="Enable alerting")

    # Channel settings
    telegram_enabled: bool = Field(False)
    telegram_bot_token: Optional[str] = Field(None)
    telegram_chat_id: Optional[str] = Field(None)

    email_enabled: bool = Field(False)
    email_smtp_host: Optional[str] = Field(None)
    email_smtp_port: int = Field(587)
    email_smtp_user: Optional[str] = Field(None)
    email_smtp_password: Optional[str] = Field(None)
    email_from: Optional[str] = Field(None)
    email_to: list[str] = Field(default_factory=list)

    # Alert type settings
    alert_on_bid: bool = Field(True)
    alert_on_win: bool = Field(True)
    alert_on_error: bool = Field(True)
    alert_on_exposure_warning: bool = Field(True)
    alert_on_hard_stop: bool = Field(True)

    # Rate limiting
    min_alert_interval_seconds: int = Field(60, ge=0)
    max_alerts_per_hour: int = Field(30, ge=1)

    # Quiet hours (no alerts except critical)
    quiet_hours_enabled: bool = Field(False)
    quiet_hours_start: int = Field(22, ge=0, le=23)  # 10 PM
    quiet_hours_end: int = Field(7, ge=0, le=23)  # 7 AM


class AlertManager:
    """Manage and dispatch alerts.

    Features:
    - Multiple notification channels (Telegram, email)
    - Alert prioritization
    - Rate limiting
    - Quiet hours support
    - Alert history
    """

    # Priority mapping for alert types
    TYPE_PRIORITIES = {
        AlertType.BID_PLACED: AlertPriority.LOW,
        AlertType.BID_WON: AlertPriority.MEDIUM,
        AlertType.BID_LOST: AlertPriority.LOW,
        AlertType.BID_FAILED: AlertPriority.MEDIUM,
        AlertType.EXPOSURE_WARNING: AlertPriority.MEDIUM,
        AlertType.EXPOSURE_CRITICAL: AlertPriority.HIGH,
        AlertType.HARD_STOP: AlertPriority.CRITICAL,
        AlertType.SESSION_ERROR: AlertPriority.HIGH,
        AlertType.AUTH_FAILURE: AlertPriority.HIGH,
        AlertType.RATE_LIMIT: AlertPriority.MEDIUM,
        AlertType.SYSTEM_ERROR: AlertPriority.HIGH,
        AlertType.DAILY_SUMMARY: AlertPriority.LOW,
    }

    def __init__(self, config: AlertConfig):
        """Initialize alert manager.

        Args:
            config: Alert configuration.
        """
        self.config = config
        self._alerts: list[Alert] = []
        self._last_alert_time: dict[AlertType, datetime] = {}
        self._hourly_count: int = 0
        self._hour_start: datetime = datetime.now()

    async def send_alert(
        self,
        alert_type: AlertType,
        title: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> bool:
        """Send an alert.

        Args:
            alert_type: Type of alert.
            title: Alert title.
            message: Alert message.
            data: Additional data.
            force: Force send, bypassing rate limits.

        Returns:
            True if alert was sent.
        """
        if not self.config.enabled:
            return False

        priority = self.TYPE_PRIORITIES.get(alert_type, AlertPriority.MEDIUM)

        alert = Alert(
            alert_type=alert_type,
            priority=priority,
            title=title,
            message=message,
            timestamp=datetime.now(),
            data=data or {},
        )

        self._alerts.append(alert)

        # Check if we should send
        if not force and not self._should_send(alert):
            logger.debug("Alert suppressed", alert_type=alert_type.value)
            return False

        # Send via enabled channels
        sent = False

        if self.config.telegram_enabled:
            try:
                await self._send_telegram(alert)
                sent = True
            except Exception as e:
                alert.error = f"Telegram: {str(e)}"
                logger.warning("Telegram alert failed", error=str(e))

        if self.config.email_enabled:
            try:
                await self._send_email(alert)
                sent = True
            except Exception as e:
                alert.error += f" Email: {str(e)}"
                logger.warning("Email alert failed", error=str(e))

        if sent:
            alert.sent = True
            alert.sent_at = datetime.now()
            self._last_alert_time[alert_type] = datetime.now()
            self._hourly_count += 1

        return sent

    def _should_send(self, alert: Alert) -> bool:
        """Check if alert should be sent.

        Args:
            alert: Alert to check.

        Returns:
            True if should send.
        """
        # Always send critical alerts
        if alert.priority == AlertPriority.CRITICAL:
            return True

        # Check quiet hours
        if self._in_quiet_hours() and alert.priority != AlertPriority.CRITICAL:
            return False

        # Check rate limits
        if not self._check_rate_limit(alert):
            return False

        # Check alert type settings
        type_checks = {
            AlertType.BID_PLACED: self.config.alert_on_bid,
            AlertType.BID_WON: self.config.alert_on_win,
            AlertType.BID_LOST: False,  # Usually too noisy
            AlertType.BID_FAILED: self.config.alert_on_error,
            AlertType.EXPOSURE_WARNING: self.config.alert_on_exposure_warning,
            AlertType.EXPOSURE_CRITICAL: self.config.alert_on_exposure_warning,
            AlertType.HARD_STOP: self.config.alert_on_hard_stop,
            AlertType.SESSION_ERROR: self.config.alert_on_error,
            AlertType.AUTH_FAILURE: self.config.alert_on_error,
            AlertType.SYSTEM_ERROR: self.config.alert_on_error,
        }

        return type_checks.get(alert.alert_type, True)

    def _check_rate_limit(self, alert: Alert) -> bool:
        """Check rate limiting.

        Args:
            alert: Alert to check.

        Returns:
            True if within rate limits.
        """
        now = datetime.now()

        # Reset hourly counter
        if (now - self._hour_start).total_seconds() >= 3600:
            self._hourly_count = 0
            self._hour_start = now

        # Check hourly limit
        if self._hourly_count >= self.config.max_alerts_per_hour:
            return False

        # Check per-type interval
        last_time = self._last_alert_time.get(alert.alert_type)
        if last_time:
            elapsed = (now - last_time).total_seconds()
            if elapsed < self.config.min_alert_interval_seconds:
                return False

        return True

    def _in_quiet_hours(self) -> bool:
        """Check if currently in quiet hours.

        Returns:
            True if in quiet hours.
        """
        if not self.config.quiet_hours_enabled:
            return False

        hour = datetime.now().hour
        start = self.config.quiet_hours_start
        end = self.config.quiet_hours_end

        if start <= end:
            return start <= hour < end
        else:
            # Wraps midnight
            return hour >= start or hour < end

    async def _send_telegram(self, alert: Alert) -> None:
        """Send alert via Telegram.

        Args:
            alert: Alert to send.
        """
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            raise ValueError("Telegram not configured")

        try:
            import httpx

            # Format message
            emoji = self._get_priority_emoji(alert.priority)
            text = f"{emoji} *{alert.title}*\n\n{alert.message}"

            if alert.data:
                text += "\n\n*Details:*"
                for key, value in alert.data.items():
                    text += f"\n• {key}: `{value}`"

            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=10)
                response.raise_for_status()

            logger.debug("Telegram alert sent", alert_type=alert.alert_type.value)

        except ImportError:
            logger.warning("httpx not installed, cannot send Telegram alerts")
            raise

    async def _send_email(self, alert: Alert) -> None:
        """Send alert via email.

        Args:
            alert: Alert to send.
        """
        if not self.config.email_to:
            raise ValueError("Email not configured")

        try:
            import aiosmtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            # Create message
            msg = MIMEMultipart()
            msg["From"] = self.config.email_from
            msg["To"] = ", ".join(self.config.email_to)
            msg["Subject"] = f"[{alert.priority.value.upper()}] {alert.title}"

            body = f"""
{alert.message}

Alert Type: {alert.alert_type.value}
Priority: {alert.priority.value}
Time: {alert.timestamp.isoformat()}

Additional Data:
{json.dumps(alert.data, indent=2) if alert.data else "None"}
"""
            msg.attach(MIMEText(body, "plain"))

            # Send
            await aiosmtplib.send(
                msg,
                hostname=self.config.email_smtp_host,
                port=self.config.email_smtp_port,
                username=self.config.email_smtp_user,
                password=self.config.email_smtp_password,
                start_tls=True,
            )

            logger.debug("Email alert sent", alert_type=alert.alert_type.value)

        except ImportError:
            logger.warning("aiosmtplib not installed, cannot send email alerts")
            raise

    def _get_priority_emoji(self, priority: AlertPriority) -> str:
        """Get emoji for priority level.

        Args:
            priority: Alert priority.

        Returns:
            Emoji string.
        """
        emojis = {
            AlertPriority.LOW: "ℹ️",
            AlertPriority.MEDIUM: "⚠️",
            AlertPriority.HIGH: "🔴",
            AlertPriority.CRITICAL: "🚨",
        }
        return emojis.get(priority, "📢")

    # Convenience methods for common alerts

    async def alert_bid_placed(
        self,
        item_id: str,
        amount: float,
        title: str,
    ) -> bool:
        """Send bid placed alert."""
        return await self.send_alert(
            AlertType.BID_PLACED,
            f"Bid Placed: {title[:30]}...",
            f"Placed bid of ${amount:.2f} on item {item_id}",
            {"item_id": item_id, "amount": amount},
        )

    async def alert_bid_won(
        self,
        item_id: str,
        amount: float,
        title: str,
    ) -> bool:
        """Send bid won alert."""
        return await self.send_alert(
            AlertType.BID_WON,
            f"🎉 Auction Won: {title[:30]}...",
            f"Won item {item_id} for ${amount:.2f}",
            {"item_id": item_id, "amount": amount},
        )

    async def alert_exposure_warning(
        self,
        current: float,
        limit: float,
        status: str,
    ) -> bool:
        """Send exposure warning alert."""
        pct = (current / limit * 100) if limit > 0 else 0
        return await self.send_alert(
            AlertType.EXPOSURE_WARNING if status != "critical" else AlertType.EXPOSURE_CRITICAL,
            f"Exposure {status.upper()}: {pct:.0f}%",
            f"Current exposure: ${current:.2f} / ${limit:.2f} ({pct:.1f}%)",
            {"current": current, "limit": limit, "percent": pct},
        )

    async def alert_hard_stop(self, reason: str) -> bool:
        """Send hard stop alert."""
        return await self.send_alert(
            AlertType.HARD_STOP,
            "🛑 HARD STOP TRIGGERED",
            f"Bidding has been stopped: {reason}",
            {"reason": reason},
            force=True,  # Always send critical alerts
        )

    async def alert_error(
        self,
        error_type: str,
        message: str,
        details: Optional[dict] = None,
    ) -> bool:
        """Send error alert."""
        return await self.send_alert(
            AlertType.SYSTEM_ERROR,
            f"Error: {error_type}",
            message,
            details,
        )

    def get_alert_history(
        self,
        count: int = 50,
        alert_type: Optional[AlertType] = None,
    ) -> list[Alert]:
        """Get recent alert history.

        Args:
            count: Number of alerts to return.
            alert_type: Filter by type.

        Returns:
            List of recent alerts.
        """
        alerts = self._alerts
        if alert_type:
            alerts = [a for a in alerts if a.alert_type == alert_type]
        return alerts[-count:]


# Import json for email body formatting
import json
