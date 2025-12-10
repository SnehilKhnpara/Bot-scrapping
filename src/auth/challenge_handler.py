"""Handle anti-bot challenges and CAPTCHAs.

This module provides detection and handling strategies for common
bot-detection mechanisms including Cloudflare, rate limiting, and CAPTCHAs.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Callable, Optional

from playwright.async_api import Page, Response
import structlog

logger = structlog.get_logger(__name__)


class ChallengeType(Enum):
    """Types of challenges that may be encountered."""

    CLOUDFLARE = auto()
    CAPTCHA = auto()
    RATE_LIMIT = auto()
    IP_BLOCK = auto()
    SESSION_EXPIRED = auto()
    MAINTENANCE = auto()
    UNKNOWN = auto()


@dataclass
class ChallengeResult:
    """Result of challenge detection or handling."""

    detected: bool
    challenge_type: Optional[ChallengeType] = None
    handled: bool = False
    message: str = ""
    retry_after: Optional[int] = None  # Seconds to wait before retry
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class ChallengeHandler:
    """Detect and handle various anti-bot challenges.

    Provides strategies for:
    - Cloudflare challenges (waiting for JS evaluation)
    - Rate limiting (exponential backoff)
    - CAPTCHA detection (notification only - no automated solving)
    - Session expiry detection
    """

    # Common indicators for each challenge type
    CLOUDFLARE_INDICATORS = [
        "cf-browser-verification",
        "cf_clearance",
        "Checking your browser",
        "Please wait while we check your browser",
        "Just a moment...",
        "DDoS protection by Cloudflare",
    ]

    CAPTCHA_INDICATORS = [
        "recaptcha",
        "hcaptcha",
        "captcha",
        "g-recaptcha",
        "h-captcha",
        "verify you are human",
    ]

    RATE_LIMIT_INDICATORS = [
        "rate limit",
        "too many requests",
        "429",
        "slow down",
        "try again later",
    ]

    MAINTENANCE_INDICATORS = [
        "maintenance",
        "temporarily unavailable",
        "503",
        "be back soon",
    ]

    def __init__(
        self,
        max_cloudflare_wait: int = 30,
        rate_limit_base_delay: int = 60,
        on_captcha_detected: Optional[Callable] = None,
    ):
        """Initialize challenge handler.

        Args:
            max_cloudflare_wait: Max seconds to wait for Cloudflare challenge.
            rate_limit_base_delay: Base delay for rate limit backoff.
            on_captcha_detected: Callback when CAPTCHA is detected.
        """
        self.max_cloudflare_wait = max_cloudflare_wait
        self.rate_limit_base_delay = rate_limit_base_delay
        self.on_captcha_detected = on_captcha_detected
        self._rate_limit_attempts = 0

    async def detect_challenge(self, page: Page) -> ChallengeResult:
        """Detect if current page shows a challenge.

        Args:
            page: Playwright page object.

        Returns:
            ChallengeResult with detection info.
        """
        try:
            content = await page.content()
            title = await page.title()
            url = page.url

            combined_text = f"{content} {title}".lower()

            # Check for Cloudflare
            for indicator in self.CLOUDFLARE_INDICATORS:
                if indicator.lower() in combined_text:
                    logger.info("Cloudflare challenge detected", url=url)
                    return ChallengeResult(
                        detected=True,
                        challenge_type=ChallengeType.CLOUDFLARE,
                        message="Cloudflare browser verification detected",
                    )

            # Check for CAPTCHA
            for indicator in self.CAPTCHA_INDICATORS:
                if indicator.lower() in combined_text:
                    logger.warning("CAPTCHA detected", url=url)
                    return ChallengeResult(
                        detected=True,
                        challenge_type=ChallengeType.CAPTCHA,
                        message="CAPTCHA verification required - manual intervention needed",
                    )

            # Check for rate limiting
            for indicator in self.RATE_LIMIT_INDICATORS:
                if indicator.lower() in combined_text:
                    logger.warning("Rate limit detected", url=url)
                    return ChallengeResult(
                        detected=True,
                        challenge_type=ChallengeType.RATE_LIMIT,
                        message="Rate limit hit",
                        retry_after=self._calculate_rate_limit_delay(),
                    )

            # Check for maintenance
            for indicator in self.MAINTENANCE_INDICATORS:
                if indicator.lower() in combined_text:
                    logger.info("Maintenance page detected", url=url)
                    return ChallengeResult(
                        detected=True,
                        challenge_type=ChallengeType.MAINTENANCE,
                        message="Site is under maintenance",
                        retry_after=300,  # 5 minutes
                    )

            return ChallengeResult(detected=False)

        except Exception as e:
            logger.error("Error detecting challenge", error=str(e))
            return ChallengeResult(
                detected=True,
                challenge_type=ChallengeType.UNKNOWN,
                message=f"Error during detection: {str(e)}",
            )

    async def handle_challenge(self, page: Page, challenge_type: ChallengeType) -> ChallengeResult:
        """Attempt to handle a detected challenge.

        Args:
            page: Playwright page object.
            challenge_type: Type of challenge to handle.

        Returns:
            ChallengeResult indicating if handling was successful.
        """
        handlers = {
            ChallengeType.CLOUDFLARE: self._handle_cloudflare,
            ChallengeType.RATE_LIMIT: self._handle_rate_limit,
            ChallengeType.CAPTCHA: self._handle_captcha,
            ChallengeType.MAINTENANCE: self._handle_maintenance,
        }

        handler = handlers.get(challenge_type)
        if handler:
            return await handler(page)

        return ChallengeResult(
            detected=True,
            challenge_type=challenge_type,
            handled=False,
            message=f"No handler for challenge type: {challenge_type}",
        )

    async def _handle_cloudflare(self, page: Page) -> ChallengeResult:
        """Handle Cloudflare challenge by waiting for JS evaluation.

        Cloudflare's browser check typically completes within 5-10 seconds.
        """
        logger.info("Waiting for Cloudflare challenge to complete...")

        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < self.max_cloudflare_wait:
            await asyncio.sleep(2)

            # Check if challenge is still present
            result = await self.detect_challenge(page)
            if not result.detected or result.challenge_type != ChallengeType.CLOUDFLARE:
                logger.info("Cloudflare challenge completed")
                return ChallengeResult(
                    detected=True,
                    challenge_type=ChallengeType.CLOUDFLARE,
                    handled=True,
                    message="Cloudflare challenge passed",
                )

        logger.warning("Cloudflare challenge timeout")
        return ChallengeResult(
            detected=True,
            challenge_type=ChallengeType.CLOUDFLARE,
            handled=False,
            message="Cloudflare challenge did not complete in time",
        )

    async def _handle_rate_limit(self, page: Page) -> ChallengeResult:
        """Handle rate limiting with exponential backoff."""
        self._rate_limit_attempts += 1
        delay = self._calculate_rate_limit_delay()

        logger.info(
            "Rate limit backoff",
            attempt=self._rate_limit_attempts,
            delay_seconds=delay,
        )

        return ChallengeResult(
            detected=True,
            challenge_type=ChallengeType.RATE_LIMIT,
            handled=False,  # Caller should wait and retry
            message=f"Rate limited - wait {delay} seconds before retry",
            retry_after=delay,
        )

    async def _handle_captcha(self, page: Page) -> ChallengeResult:
        """Handle CAPTCHA detection - requires manual intervention.

        CAPTCHAs cannot be automatically solved ethically.
        This notifies the user and pauses execution.
        """
        logger.warning("CAPTCHA detected - manual intervention required")

        if self.on_captcha_detected:
            await self.on_captcha_detected(page.url)

        return ChallengeResult(
            detected=True,
            challenge_type=ChallengeType.CAPTCHA,
            handled=False,
            message="CAPTCHA detected - manual solve required. Bot will pause.",
        )

    async def _handle_maintenance(self, page: Page) -> ChallengeResult:
        """Handle maintenance page."""
        return ChallengeResult(
            detected=True,
            challenge_type=ChallengeType.MAINTENANCE,
            handled=False,
            message="Site under maintenance - retry later",
            retry_after=300,
        )

    def _calculate_rate_limit_delay(self) -> int:
        """Calculate delay for rate limit using exponential backoff.

        Returns:
            Delay in seconds.
        """
        # Exponential backoff: base * 2^attempts, capped at 30 minutes
        delay = min(
            self.rate_limit_base_delay * (2 ** self._rate_limit_attempts),
            1800,  # 30 minutes max
        )
        return delay

    def reset_rate_limit_counter(self) -> None:
        """Reset rate limit attempt counter after successful request."""
        self._rate_limit_attempts = 0

    async def check_response(self, response: Response) -> ChallengeResult:
        """Check HTTP response for challenge indicators.

        Args:
            response: Playwright response object.

        Returns:
            ChallengeResult based on response status and headers.
        """
        status = response.status

        if status == 403:
            return ChallengeResult(
                detected=True,
                challenge_type=ChallengeType.IP_BLOCK,
                message="Access forbidden - possible IP block",
            )

        if status == 429:
            retry_after = response.headers.get("retry-after")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else self.rate_limit_base_delay

            return ChallengeResult(
                detected=True,
                challenge_type=ChallengeType.RATE_LIMIT,
                message="Rate limit response",
                retry_after=delay,
            )

        if status == 503:
            return ChallengeResult(
                detected=True,
                challenge_type=ChallengeType.MAINTENANCE,
                message="Service unavailable",
                retry_after=60,
            )

        if status == 401:
            return ChallengeResult(
                detected=True,
                challenge_type=ChallengeType.SESSION_EXPIRED,
                message="Session expired - re-authentication required",
            )

        return ChallengeResult(detected=False)
