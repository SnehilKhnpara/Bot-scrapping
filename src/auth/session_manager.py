"""Session management for marketplace authentication.

Handles login, session persistence, token refresh, and retry logic
using Playwright for browser automation.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
import structlog

from .challenge_handler import ChallengeHandler, ChallengeType
from .credentials import CredentialsLoader, MarketplaceCredentials
from ..utils.helpers import async_random_delay, generate_session_id

logger = structlog.get_logger(__name__)


class SessionState(Enum):
    """Current state of the session."""

    NOT_INITIALIZED = auto()
    INITIALIZING = auto()
    AUTHENTICATED = auto()
    EXPIRED = auto()
    REFRESHING = auto()
    ERROR = auto()
    CHALLENGE_REQUIRED = auto()


@dataclass
class SessionInfo:
    """Information about the current session."""

    session_id: str
    state: SessionState
    created_at: datetime
    last_activity: datetime
    expires_at: Optional[datetime] = None
    login_attempts: int = 0
    refresh_count: int = 0
    cookies: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    """Manage authenticated sessions with the marketplace.

    Features:
    - Automatic login with retry logic
    - Session persistence via cookies
    - Automatic session refresh before expiry
    - Challenge detection and handling
    - Configurable timeouts and delays
    """

    DEFAULT_TIMEOUT = 30000  # 30 seconds
    MAX_LOGIN_ATTEMPTS = 3
    SESSION_REFRESH_THRESHOLD = timedelta(minutes=5)

    def __init__(
        self,
        marketplace_url: str,
        credentials_loader: CredentialsLoader,
        storage_dir: Path,
        headless: bool = True,
        session_duration: timedelta = timedelta(hours=1),
        login_selectors: Optional[dict[str, str]] = None,
    ):
        """Initialize session manager.

        Args:
            marketplace_url: Base URL of the marketplace.
            credentials_loader: Loader for credentials.
            storage_dir: Directory for session storage.
            headless: Run browser in headless mode.
            session_duration: Expected session duration before refresh needed.
            login_selectors: CSS selectors for login form elements.
        """
        self.marketplace_url = marketplace_url.rstrip("/")
        self.credentials_loader = credentials_loader
        self.storage_dir = storage_dir
        self.headless = headless
        self.session_duration = session_duration

        # Default selectors (should be customized per marketplace)
        self.login_selectors = login_selectors or {
            "username_input": 'input[name="username"], input[name="email"], input[type="email"]',
            "password_input": 'input[name="password"], input[type="password"]',
            "submit_button": 'button[type="submit"], input[type="submit"]',
            "login_success_indicator": ".user-menu, .account-menu, .logged-in",
            "login_error_indicator": ".error, .alert-danger, .login-error",
        }

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._session_info: Optional[SessionInfo] = None
        self._challenge_handler = ChallengeHandler()

        self.storage_dir.mkdir(parents=True, exist_ok=True)

    @property
    def session_info(self) -> Optional[SessionInfo]:
        """Get current session information."""
        return self._session_info

    @property
    def is_authenticated(self) -> bool:
        """Check if session is currently authenticated."""
        return (
            self._session_info is not None
            and self._session_info.state == SessionState.AUTHENTICATED
        )

    @property
    def page(self) -> Optional[Page]:
        """Get the current page object for use in scraping/bidding."""
        return self._page

    @property
    def context(self) -> Optional[BrowserContext]:
        """Get the browser context."""
        return self._context

    async def initialize(self) -> bool:
        """Initialize browser and attempt to restore or create session.

        Returns:
            True if initialization successful and authenticated.
        """
        session_id = generate_session_id()
        self._session_info = SessionInfo(
            session_id=session_id,
            state=SessionState.INITIALIZING,
            created_at=datetime.now(),
            last_activity=datetime.now(),
        )

        logger.info("Initializing session", session_id=session_id)

        try:
            # Start Playwright
            self._playwright = await async_playwright().start()

            # Launch browser with anti-detection settings
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )

            # Try to restore session from cookies
            restored = await self._restore_session()
            if restored:
                logger.info("Session restored from cookies")
                return True

            # Otherwise, perform fresh login
            return await self.login()

        except Exception as e:
            logger.error("Session initialization failed", error=str(e))
            self._session_info.state = SessionState.ERROR
            return False

    async def _restore_session(self) -> bool:
        """Attempt to restore session from saved cookies.

        Returns:
            True if session restored and valid.
        """
        cookies_file = self.storage_dir / "cookies.json"

        if not cookies_file.exists():
            logger.debug("No saved cookies found")
            return False

        try:
            cookies = json.loads(cookies_file.read_text())

            # Create context with saved cookies
            self._context = await self._browser.new_context(
                user_agent=self._get_user_agent(),
            )
            await self._context.add_cookies(cookies)

            self._page = await self._context.new_page()
            await self._page.set_default_timeout(self.DEFAULT_TIMEOUT)

            # Navigate to marketplace and check if logged in
            await self._page.goto(self.marketplace_url)
            await async_random_delay(1, 2)

            # Check for challenges
            challenge = await self._challenge_handler.detect_challenge(self._page)
            if challenge.detected:
                result = await self._challenge_handler.handle_challenge(
                    self._page, challenge.challenge_type
                )
                if not result.handled:
                    return False

            # Verify login status
            if await self._verify_login():
                self._session_info.state = SessionState.AUTHENTICATED
                self._session_info.cookies = cookies
                self._session_info.expires_at = datetime.now() + self.session_duration
                return True

            logger.info("Saved session expired or invalid")
            return False

        except Exception as e:
            logger.warning("Failed to restore session", error=str(e))
            return False

    async def login(self) -> bool:
        """Perform fresh login to the marketplace.

        Returns:
            True if login successful.
        """
        if self._session_info.login_attempts >= self.MAX_LOGIN_ATTEMPTS:
            logger.error("Max login attempts exceeded")
            self._session_info.state = SessionState.ERROR
            return False

        self._session_info.login_attempts += 1
        logger.info("Attempting login", attempt=self._session_info.login_attempts)

        try:
            # Load credentials
            credentials = self.credentials_loader.load()

            # Create fresh context if needed
            if not self._context:
                self._context = await self._browser.new_context(
                    user_agent=self._get_user_agent(),
                )

            if not self._page:
                self._page = await self._context.new_page()
                await self._page.set_default_timeout(self.DEFAULT_TIMEOUT)

            # Navigate to login page
            login_url = f"{self.marketplace_url}/login"
            await self._page.goto(login_url)
            await async_random_delay(1, 3)

            # Handle any initial challenges
            challenge = await self._challenge_handler.detect_challenge(self._page)
            if challenge.detected:
                result = await self._challenge_handler.handle_challenge(
                    self._page, challenge.challenge_type
                )
                if not result.handled and challenge.challenge_type == ChallengeType.CAPTCHA:
                    self._session_info.state = SessionState.CHALLENGE_REQUIRED
                    return False

            # Perform login
            success = await self._perform_login(credentials)

            if success:
                self._session_info.state = SessionState.AUTHENTICATED
                self._session_info.expires_at = datetime.now() + self.session_duration
                await self._save_cookies()
                logger.info("Login successful")
                return True

            # Retry with delay
            await async_random_delay(3, 5)
            return await self.login()

        except Exception as e:
            logger.error("Login failed", error=str(e))
            await async_random_delay(2, 4)
            return await self.login()

    async def _perform_login(self, credentials: MarketplaceCredentials) -> bool:
        """Execute the actual login form submission.

        Args:
            credentials: Login credentials.

        Returns:
            True if login successful.
        """
        try:
            # Wait for and fill username
            username_selector = self.login_selectors["username_input"]
            await self._page.wait_for_selector(username_selector, timeout=10000)
            await async_random_delay(0.5, 1)
            await self._page.fill(username_selector, credentials.username)

            # Fill password
            await async_random_delay(0.3, 0.8)
            password_selector = self.login_selectors["password_input"]
            await self._page.fill(
                password_selector,
                credentials.password.get_secret_value(),
            )

            # Click submit
            await async_random_delay(0.5, 1.5)
            submit_selector = self.login_selectors["submit_button"]
            await self._page.click(submit_selector)

            # Wait for navigation
            await self._page.wait_for_load_state("networkidle", timeout=15000)
            await async_random_delay(1, 2)

            # Check for login errors
            error_selector = self.login_selectors["login_error_indicator"]
            error_element = await self._page.query_selector(error_selector)
            if error_element:
                error_text = await error_element.text_content()
                logger.warning("Login error detected", error=error_text)
                return False

            # Verify login success
            return await self._verify_login()

        except Exception as e:
            logger.error("Login form submission failed", error=str(e))
            return False

    async def _verify_login(self) -> bool:
        """Verify that user is currently logged in.

        Returns:
            True if logged in.
        """
        try:
            success_selector = self.login_selectors["login_success_indicator"]
            success_element = await self._page.query_selector(success_selector)
            return success_element is not None
        except Exception:
            return False

    async def _save_cookies(self) -> None:
        """Save current session cookies to disk."""
        try:
            cookies = await self._context.cookies()
            cookies_file = self.storage_dir / "cookies.json"
            cookies_file.write_text(json.dumps(cookies, indent=2))
            self._session_info.cookies = cookies
            logger.debug("Cookies saved", count=len(cookies))
        except Exception as e:
            logger.warning("Failed to save cookies", error=str(e))

    async def refresh_session(self) -> bool:
        """Refresh the session before it expires.

        Returns:
            True if refresh successful.
        """
        if not self.is_authenticated:
            return await self.login()

        self._session_info.state = SessionState.REFRESHING
        self._session_info.refresh_count += 1

        logger.info("Refreshing session", refresh_count=self._session_info.refresh_count)

        try:
            # Navigate to a page that requires authentication
            await self._page.goto(self.marketplace_url)
            await async_random_delay(1, 2)

            # Check if still logged in
            if await self._verify_login():
                self._session_info.state = SessionState.AUTHENTICATED
                self._session_info.expires_at = datetime.now() + self.session_duration
                self._session_info.last_activity = datetime.now()
                await self._save_cookies()
                return True

            # Session expired, need to re-login
            logger.info("Session expired during refresh, re-authenticating")
            self._session_info.state = SessionState.EXPIRED
            self._session_info.login_attempts = 0  # Reset for new login
            return await self.login()

        except Exception as e:
            logger.error("Session refresh failed", error=str(e))
            return await self.login()

    async def ensure_authenticated(self) -> bool:
        """Ensure session is authenticated, refreshing if needed.

        Returns:
            True if authenticated (possibly after refresh).
        """
        if not self._session_info:
            return await self.initialize()

        # Check if refresh is needed
        if self._session_info.expires_at:
            time_until_expiry = self._session_info.expires_at - datetime.now()
            if time_until_expiry < self.SESSION_REFRESH_THRESHOLD:
                return await self.refresh_session()

        if self.is_authenticated:
            return True

        if self._session_info.state == SessionState.EXPIRED:
            return await self.login()

        return await self.initialize()

    def update_activity(self) -> None:
        """Update last activity timestamp."""
        if self._session_info:
            self._session_info.last_activity = datetime.now()

    async def close(self) -> None:
        """Close browser and clean up resources."""
        logger.info("Closing session")

        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("Error during session cleanup", error=str(e))

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _get_user_agent(self) -> str:
        """Get a realistic user agent string."""
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    async def __aenter__(self):
        """Async context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
