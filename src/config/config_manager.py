"""Configuration management for the Marketplace Automation Agent.

Handles loading, validation, and saving of all agent configuration
from YAML/JSON files with environment variable overrides.
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings
import structlog

from ..scoring.quality_filter import FilterConfig
from ..scoring.scoring_engine import ScoringConfig
from ..bidding.bid_calculator import BidConfig

logger = structlog.get_logger(__name__)


class MarketplaceConfig(BaseModel):
    """Marketplace-specific configuration."""

    name: str = Field("generic", description="Marketplace name")
    base_url: str = Field(..., description="Base URL of marketplace")
    listings_url: str = Field(..., description="URL for listings page")
    login_url: Optional[str] = Field(None, description="Login page URL")

    # Selectors
    login_selectors: dict[str, str] = Field(default_factory=dict)
    listing_selectors: dict[str, str] = Field(default_factory=dict)
    bid_selectors: dict[str, str] = Field(default_factory=dict)

    # Pagination
    pagination_type: str = Field("url_param", description="pagination type: url_param, button, infinite_scroll")
    pagination_param: str = Field("page", description="URL parameter for pagination")
    max_pages: int = Field(10, ge=1, le=100)
    items_per_page: int = Field(50, ge=1, le=200)


class TimingConfig(BaseModel):
    """Timing and delay configuration."""

    min_action_delay: float = Field(1.0, ge=0, description="Min delay between actions (seconds)")
    max_action_delay: float = Field(3.0, ge=0, description="Max delay between actions (seconds)")
    page_load_timeout: int = Field(30000, ge=5000, description="Page load timeout (ms)")
    between_pages_delay_min: float = Field(2.0, ge=0)
    between_pages_delay_max: float = Field(4.0, ge=0)
    session_refresh_interval: int = Field(3600, ge=60, description="Session refresh interval (seconds)")


class NotificationConfig(BaseModel):
    """Notification settings."""

    enabled: bool = Field(False, description="Enable notifications")
    telegram_enabled: bool = Field(False)
    telegram_bot_token: Optional[str] = Field(None)
    telegram_chat_id: Optional[str] = Field(None)
    email_enabled: bool = Field(False)
    email_smtp_host: Optional[str] = Field(None)
    email_smtp_port: int = Field(587)
    email_from: Optional[str] = Field(None)
    email_to: Optional[str] = Field(None)
    notify_on_bid: bool = Field(True)
    notify_on_error: bool = Field(True)
    notify_on_win: bool = Field(True)


class StorageConfig(BaseModel):
    """Data storage configuration."""

    base_dir: Path = Field(Path("data"), description="Base data directory")
    raw_html_dir: str = Field("raw", description="Raw HTML subdirectory")
    processed_dir: str = Field("processed", description="Processed data subdirectory")
    reports_dir: str = Field("reports", description="Reports subdirectory")
    logs_dir: Path = Field(Path("logs"), description="Logs directory")
    save_raw_html: bool = Field(True)
    save_screenshots: bool = Field(True)
    log_retention_days: int = Field(30, ge=1)


class AgentConfig(BaseSettings):
    """Main agent configuration.

    Combines all configuration sections into a single validated model.
    Supports environment variable overrides with AGENT_ prefix.
    """

    # Mode
    mode: str = Field("DRY_RUN", description="Operating mode: DRY_RUN or LIVE")
    headless: bool = Field(True, description="Run browser in headless mode")

    # Sub-configurations
    marketplace: MarketplaceConfig
    timing: TimingConfig = Field(default_factory=TimingConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    bidding: BidConfig = Field(default_factory=BidConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # Target categories/search
    target_categories: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    refresh_interval: int = Field(300, ge=60, description="Refresh interval in seconds")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        """Validate operating mode."""
        valid_modes = ["DRY_RUN", "LIVE"]
        if v.upper() not in valid_modes:
            raise ValueError(f"Mode must be one of: {valid_modes}")
        return v.upper()

    class Config:
        """Pydantic settings config."""

        env_prefix = "AGENT_"
        env_nested_delimiter = "__"
        extra = "ignore"


class ConfigManager:
    """Manage agent configuration loading, validation, and persistence."""

    DEFAULT_CONFIG_PATHS = [
        Path("config/config.yaml"),
        Path("config/config.yml"),
        Path("config/config.json"),
        Path("config.yaml"),
        Path("config.yml"),
    ]

    def __init__(self, config_path: Optional[Path] = None):
        """Initialize configuration manager.

        Args:
            config_path: Optional explicit path to config file.
        """
        self.config_path = config_path
        self._config: Optional[AgentConfig] = None

    def load(self, config_path: Optional[Path] = None) -> AgentConfig:
        """Load configuration from file.

        Args:
            config_path: Optional path override.

        Returns:
            Validated AgentConfig.

        Raises:
            FileNotFoundError: If no config file found.
            ValueError: If config validation fails.
        """
        path = config_path or self.config_path

        if path is None:
            path = self._find_config_file()

        if path is None:
            logger.warning("No config file found, using defaults with required fields")
            # Return minimal config - marketplace config is required
            raise FileNotFoundError(
                "No configuration file found. Create config/config.yaml "
                "or specify path with --config"
            )

        logger.info("Loading configuration", path=str(path))

        config_data = self._load_file(path)
        self._config = AgentConfig(**config_data)
        self.config_path = path

        logger.info(
            "Configuration loaded",
            mode=self._config.mode,
            marketplace=self._config.marketplace.name,
        )

        return self._config

    def _find_config_file(self) -> Optional[Path]:
        """Find configuration file in default locations.

        Returns:
            Path to config file or None.
        """
        for path in self.DEFAULT_CONFIG_PATHS:
            if path.exists():
                return path
        return None

    def _load_file(self, path: Path) -> dict[str, Any]:
        """Load configuration from file.

        Args:
            path: Path to config file.

        Returns:
            Configuration dictionary.
        """
        suffix = path.suffix.lower()

        if suffix in [".yaml", ".yml"]:
            with open(path, "r") as f:
                return yaml.safe_load(f) or {}
        elif suffix == ".json":
            import json
            with open(path, "r") as f:
                return json.load(f)
        else:
            raise ValueError(f"Unsupported config file format: {suffix}")

    def save(self, config: Optional[AgentConfig] = None, path: Optional[Path] = None) -> None:
        """Save configuration to file.

        Args:
            config: Configuration to save (uses current if None).
            path: Path to save to (uses current if None).
        """
        config = config or self._config
        path = path or self.config_path

        if config is None:
            raise ValueError("No configuration to save")
        if path is None:
            path = Path("config/config.yaml")

        logger.info("Saving configuration", path=str(path))

        path.parent.mkdir(parents=True, exist_ok=True)

        config_dict = config.model_dump(mode="json")

        suffix = path.suffix.lower()
        if suffix in [".yaml", ".yml"]:
            with open(path, "w") as f:
                yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
        else:
            import json
            with open(path, "w") as f:
                json.dump(config_dict, f, indent=2)

    @property
    def config(self) -> Optional[AgentConfig]:
        """Get current configuration."""
        return self._config

    def update(self, updates: dict[str, Any]) -> AgentConfig:
        """Update configuration with new values.

        Args:
            updates: Dictionary of updates (supports nested keys with dots).

        Returns:
            Updated AgentConfig.
        """
        if self._config is None:
            raise ValueError("No configuration loaded")

        current = self._config.model_dump()
        merged = self._deep_merge(current, updates)
        self._config = AgentConfig(**merged)
        return self._config

    def _deep_merge(self, base: dict, updates: dict) -> dict:
        """Deep merge updates into base dict.

        Args:
            base: Base dictionary.
            updates: Updates to merge.

        Returns:
            Merged dictionary.
        """
        result = base.copy()
        for key, value in updates.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def validate_for_live(self) -> tuple[bool, list[str]]:
        """Validate configuration is safe for LIVE mode.

        Returns:
            Tuple of (is_valid, list of issues).
        """
        issues = []

        if self._config is None:
            return False, ["No configuration loaded"]

        # Check budget limits are set
        if self._config.bidding.daily_budget <= 0:
            issues.append("Daily budget must be positive for LIVE mode")

        if self._config.bidding.per_item_max <= 0:
            issues.append("Per-item max must be positive for LIVE mode")

        # Check exposure limits
        if self._config.bidding.max_daily_exposure <= 0:
            issues.append("Exposure limit must be positive for LIVE mode")

        # Check marketplace URLs
        if not self._config.marketplace.base_url:
            issues.append("Marketplace base URL is required")

        if not self._config.marketplace.listings_url:
            issues.append("Listings URL is required")

        # Check for target content
        if not self._config.target_categories and not self._config.search_queries:
            issues.append("At least one target category or search query required")

        return len(issues) == 0, issues

    def get_summary(self) -> str:
        """Get human-readable configuration summary.

        Returns:
            Summary string.
        """
        if self._config is None:
            return "No configuration loaded"

        return f"""
Configuration Summary
=====================
Mode: {self._config.mode}
Marketplace: {self._config.marketplace.name}
Base URL: {self._config.marketplace.base_url}

Budget Settings:
  Daily Budget: ${self._config.bidding.daily_budget:.2f}
  Weekly Budget: ${self._config.bidding.weekly_budget:.2f}
  Per-Item Max: ${self._config.bidding.per_item_max:.2f}
  Max Exposure: ${self._config.bidding.max_daily_exposure:.2f}

Scoring Settings:
  Min Eligible Score: {self._config.scoring.min_eligible_score}

Filter Settings:
  Min Seller Rating: {self._config.filter.min_seller_rating}%
  Require Images: {self._config.filter.require_images}

Targets:
  Categories: {', '.join(self._config.target_categories) or 'None'}
  Search Queries: {', '.join(self._config.search_queries) or 'None'}
  Refresh Interval: {self._config.refresh_interval}s
"""
