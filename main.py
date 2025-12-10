#!/usr/bin/env python3
"""Marketplace Automation Agent - Main Entry Point.

A production-grade automation system for secure marketplace bidding.
Supports DRY_RUN and LIVE modes with comprehensive safety controls.
"""

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
import structlog

from src.auth.credentials import CredentialsLoader
from src.auth.session_manager import SessionManager
from src.scraper.listing_scraper import ListingScraper, ScraperConfig
from src.scraper.normalizer import ListingNormalizer
from src.scoring.quality_filter import QualityFilter
from src.scoring.scoring_engine import ScoringEngine
from src.bidding.bid_calculator import BidCalculator
from src.bidding.dry_run_engine import DryRunEngine
from src.bidding.live_engine import LiveBiddingEngine
from src.config.config_manager import ConfigManager
from src.config.cli_interface import CLIInterface
from src.safety.exposure_tracker import ExposureTracker, ExposureLimits
from src.safety.audit_logger import AuditLogger, AuditCategory, AuditLevel
from src.safety.alerts import AlertManager
from src.utils.helpers import generate_session_id

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)
console = Console()


class MarketplaceAgent:
    """Main marketplace automation agent.

    Orchestrates all components for scraping, filtering, scoring,
    and bidding with integrated safety controls.
    """

    def __init__(self, config_path: Optional[Path] = None):
        """Initialize the agent.

        Args:
            config_path: Path to configuration file.
        """
        self.config_manager = ConfigManager(config_path)
        self.config = None
        self.session_id = generate_session_id()

        # Components (initialized in setup)
        self.session_manager: Optional[SessionManager] = None
        self.scraper: Optional[ListingScraper] = None
        self.quality_filter: Optional[QualityFilter] = None
        self.scoring_engine: Optional[ScoringEngine] = None
        self.bid_calculator: Optional[BidCalculator] = None
        self.dry_run_engine: Optional[DryRunEngine] = None
        self.live_engine: Optional[LiveBiddingEngine] = None
        self.exposure_tracker: Optional[ExposureTracker] = None
        self.audit_logger: Optional[AuditLogger] = None
        self.alert_manager: Optional[AlertManager] = None

        self._shutdown_requested = False

    async def setup(self) -> bool:
        """Initialize all components.

        Returns:
            True if setup successful.
        """
        try:
            # Load configuration
            self.config = self.config_manager.load()
            logger.info(
                "Configuration loaded",
                mode=self.config.mode,
                marketplace=self.config.marketplace.name,
            )

            # Setup audit logging
            self.audit_logger = AuditLogger(
                log_dir=self.config.storage.logs_dir,
                session_id=self.session_id,
            )
            self.audit_logger.log(
                AuditLevel.INFO,
                AuditCategory.SYSTEM,
                "agent_initialized",
                {"mode": self.config.mode, "session_id": self.session_id},
            )

            # Setup credentials
            credentials_loader = CredentialsLoader()

            # Setup session manager
            self.session_manager = SessionManager(
                marketplace_url=self.config.marketplace.base_url,
                credentials_loader=credentials_loader,
                storage_dir=self.config.storage.base_dir / "sessions",
                headless=self.config.headless,
                login_selectors=self.config.marketplace.login_selectors or None,
            )

            # Setup scraper
            scraper_config = ScraperConfig(
                listings_url=self.config.marketplace.listings_url,
                base_url=self.config.marketplace.base_url,
                max_pages=self.config.marketplace.max_pages,
                items_per_page=self.config.marketplace.items_per_page,
                pagination_param=self.config.marketplace.pagination_param,
                storage_dir=self.config.storage.base_dir,
                save_raw_html=self.config.storage.save_raw_html,
                save_screenshots=self.config.storage.save_screenshots,
            )
            self.scraper = ListingScraper(
                session_manager=self.session_manager,
                config=scraper_config,
            )

            # Setup filtering and scoring
            self.quality_filter = QualityFilter(config=self.config.filter)
            self.scoring_engine = ScoringEngine(config=self.config.scoring)

            # Setup bid calculator
            self.bid_calculator = BidCalculator(config=self.config.bidding)

            # Setup exposure tracking
            exposure_limits = ExposureLimits(
                daily_max_spend=self.config.bidding.daily_budget,
                daily_max_exposure=self.config.bidding.max_daily_exposure,
                weekly_max_spend=self.config.bidding.weekly_budget,
            )
            self.exposure_tracker = ExposureTracker(limits=exposure_limits)

            # Setup alerts
            self.alert_manager = AlertManager(config=self.config.notifications)

            # Setup engines
            reports_dir = self.config.storage.base_dir / self.config.storage.reports_dir
            self.dry_run_engine = DryRunEngine(
                quality_filter=self.quality_filter,
                scoring_engine=self.scoring_engine,
                bid_calculator=self.bid_calculator,
                output_dir=reports_dir,
            )

            self.live_engine = LiveBiddingEngine(
                session_manager=self.session_manager,
                bid_calculator=self.bid_calculator,
                selectors=self.config.marketplace.bid_selectors or None,
            )

            logger.info("Agent setup complete", session_id=self.session_id)
            return True

        except Exception as e:
            logger.error("Agent setup failed", error=str(e))
            if self.audit_logger:
                self.audit_logger.log_error("setup_failed", e)
            return False

    async def run(self) -> None:
        """Run the main agent loop."""
        if not self.config:
            logger.error("Agent not configured")
            return

        logger.info(
            "Starting agent",
            mode=self.config.mode,
            targets=self.config.target_categories + self.config.search_queries,
        )

        try:
            while not self._shutdown_requested:
                await self._run_cycle()

                if self._shutdown_requested:
                    break

                # Wait for next cycle
                logger.info(
                    "Waiting for next cycle",
                    seconds=self.config.refresh_interval,
                )
                await asyncio.sleep(self.config.refresh_interval)

        except asyncio.CancelledError:
            logger.info("Agent cancelled")
        except Exception as e:
            logger.error("Agent error", error=str(e))
            if self.audit_logger:
                self.audit_logger.log_error("agent_error", e)
        finally:
            await self.shutdown()

    async def _run_cycle(self) -> None:
        """Run a single scrape-filter-score-bid cycle."""
        cycle_start = datetime.now()
        logger.info("Starting cycle")

        try:
            # Scrape listings
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Scraping listings...", total=None)

                scrape_result = await self.scraper.scrape_listings(
                    search_query=self.config.search_queries[0] if self.config.search_queries else None,
                    category=self.config.target_categories[0] if self.config.target_categories else None,
                )

                progress.update(task, description=f"Found {scrape_result.total_found} listings")

            if self.audit_logger:
                self.audit_logger.log_scrape(
                    "scrape_complete",
                    items_found=scrape_result.total_found,
                    pages=scrape_result.pages_scraped,
                    duration_ms=(datetime.now() - cycle_start).total_seconds() * 1000,
                )

            if scrape_result.total_found == 0:
                logger.warning("No listings found")
                return

            # Process based on mode
            if self.config.mode == "DRY_RUN":
                await self._run_dry_mode(scrape_result.listings)
            else:
                await self._run_live_mode(scrape_result.listings)

            cycle_duration = (datetime.now() - cycle_start).total_seconds()
            logger.info("Cycle complete", duration_seconds=cycle_duration)

        except Exception as e:
            logger.error("Cycle error", error=str(e))
            if self.audit_logger:
                self.audit_logger.log_error("cycle_error", e)

    async def _run_dry_mode(self, listings) -> None:
        """Run DRY_RUN simulation mode."""
        logger.info("Running DRY_RUN simulation", listings=len(listings))

        result = self.dry_run_engine.run(listings)

        console.print(Panel(
            f"[bold green]DRY_RUN Complete[/bold green]\n\n"
            f"Processed: {result.total_processed}\n"
            f"Passed Filter: {result.passed_filter}\n"
            f"Eligible for Bidding: {result.passed_scoring}\n"
            f"Would Bid On: {result.would_bid_count}\n"
            f"Simulated Exposure: ${result.simulated_exposure:.2f}",
            title="Simulation Results",
        ))

    async def _run_live_mode(self, listings) -> None:
        """Run LIVE bidding mode."""
        logger.info("Running LIVE mode", listings=len(listings))

        # Filter listings
        passed_filter, filter_results = self.quality_filter.filter_batch(listings)
        logger.info("Filtering complete", passed=len(passed_filter))

        # Score listings
        eligible, scores = self.scoring_engine.score_batch(passed_filter)
        logger.info("Scoring complete", eligible=len(eligible))

        # Calculate bids
        bids_to_execute = []
        for listing, score in eligible:
            # Check exposure before deciding
            can_bid, reason = self.exposure_tracker.can_place_bid(
                self.config.bidding.per_item_max
            )
            if not can_bid:
                logger.warning("Exposure limit prevents bid", reason=reason)
                break

            decision = self.bid_calculator.calculate_bid(listing, score)

            if self.audit_logger:
                self.audit_logger.log_bid_decision(
                    listing.item_id,
                    decision.should_bid,
                    decision.max_bid,
                    decision.rejection_reason.value if decision.rejection_reason else None,
                )

            if decision.should_bid:
                bids_to_execute.append((listing, decision, score))

        if not bids_to_execute:
            logger.info("No bids to execute this cycle")
            return

        # Execute bids
        logger.info("Executing bids", count=len(bids_to_execute))
        results = await self.live_engine.execute_batch(bids_to_execute)

        # Log results
        for result in results:
            if self.audit_logger:
                self.audit_logger.log_bid_execution(
                    result.item_id,
                    result.bid_amount,
                    result.status.value,
                    result.confirmation_code,
                    result.error_message if not result.is_winning else None,
                )

            # Track in exposure
            if result.status.value in ["submitted", "confirmed"]:
                self.exposure_tracker.record_bid(
                    result.item_id,
                    result.bid_amount,
                )

        # Alert on results
        if self.alert_manager and self.config.notifications.enabled:
            for result in results:
                if result.is_winning:
                    await self.alert_manager.alert_bid_placed(
                        result.item_id,
                        result.bid_amount,
                        "Item",  # Would need title from listing
                    )

    def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        logger.info("Shutdown requested")
        self._shutdown_requested = True
        if self.live_engine:
            self.live_engine.stop()

    async def shutdown(self) -> None:
        """Clean up resources."""
        logger.info("Shutting down agent")

        if self.session_manager:
            await self.session_manager.close()

        if self.audit_logger:
            self.audit_logger.log(
                AuditLevel.INFO,
                AuditCategory.SYSTEM,
                "agent_shutdown",
                {"session_id": self.session_id},
            )
            self.audit_logger.close()

        logger.info("Agent shutdown complete")


# CLI Commands
@click.group()
@click.version_option(version="1.0.0")
def cli():
    """Marketplace Automation Agent - Secure Bidding System."""
    pass


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True), help="Config file path")
@click.option("--mode", "-m", type=click.Choice(["DRY_RUN", "LIVE"]), help="Override mode")
@click.option("--headless/--no-headless", default=True, help="Run browser headless")
def run(config: Optional[str], mode: Optional[str], headless: bool):
    """Run the marketplace automation agent."""
    console.print(Panel.fit(
        "[bold blue]Marketplace Automation Agent[/bold blue]\n"
        "[dim]Starting up...[/dim]",
        border_style="blue",
    ))

    agent = MarketplaceAgent(Path(config) if config else None)

    # Setup signal handlers
    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutdown signal received...[/yellow]")
        agent.request_shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    async def main():
        if not await agent.setup():
            console.print("[red]Setup failed. Check logs for details.[/red]")
            sys.exit(1)

        # Override mode if specified
        if mode:
            agent.config.mode = mode

        if not headless:
            agent.config.headless = False

        console.print(f"[green]Running in {agent.config.mode} mode[/green]")

        await agent.run()

    asyncio.run(main())


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True), help="Config file path")
def configure(config: Optional[str]):
    """Launch interactive configuration interface."""
    config_manager = ConfigManager(Path(config) if config else None)

    try:
        config_manager.load()
    except FileNotFoundError:
        console.print("[yellow]No configuration found. Please create config/config.yaml[/yellow]")
        return

    interface = CLIInterface(config_manager)
    interface.show_main_menu()


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True), help="Config file path")
def validate(config: Optional[str]):
    """Validate configuration for LIVE mode."""
    config_manager = ConfigManager(Path(config) if config else None)

    try:
        config_manager.load()
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    is_valid, issues = config_manager.validate_for_live()

    if is_valid:
        console.print("[green]Configuration is valid for LIVE mode[/green]")
        console.print(config_manager.get_summary())
    else:
        console.print("[red]Configuration has issues for LIVE mode:[/red]")
        for issue in issues:
            console.print(f"  [red]x[/red] {issue}")
        sys.exit(1)


@cli.command()
def init():
    """Initialize a new configuration file."""
    config_path = Path("config/config.yaml")

    if config_path.exists():
        if not click.confirm("config/config.yaml already exists. Overwrite?"):
            return

    config_path.parent.mkdir(parents=True, exist_ok=True)

    default_config = """# Marketplace Automation Agent Configuration

# Operating mode: DRY_RUN or LIVE
mode: DRY_RUN
headless: true

# Marketplace settings
marketplace:
  name: example_marketplace
  base_url: https://example-marketplace.com
  listings_url: https://example-marketplace.com/listings
  login_url: https://example-marketplace.com/login
  pagination_param: page
  max_pages: 5
  items_per_page: 50

# Timing configuration
timing:
  min_action_delay: 1.0
  max_action_delay: 3.0
  page_load_timeout: 30000
  session_refresh_interval: 3600

# Quality filter settings
filter:
  min_seller_rating: 95.0
  min_seller_feedback: 10
  min_price: 1.0
  max_price: 500.0
  require_images: true
  max_bid_count: 30
  blacklisted_categories:
    - adult
    - weapons

# Scoring settings
scoring:
  min_eligible_score: 60.0
  weight_seller_trust: 0.20
  weight_price_value: 0.25
  weight_condition: 0.15
  weight_competition: 0.15
  weight_time_urgency: 0.10
  weight_category_relevance: 0.15

# Bidding settings
bidding:
  daily_budget: 100.0
  weekly_budget: 500.0
  per_item_max: 50.0
  max_daily_exposure: 200.0
  max_pending_bids: 10
  min_bid_increment: 0.50

# Notification settings
notifications:
  enabled: false
  telegram_enabled: false
  email_enabled: false
  alert_on_bid: true
  alert_on_win: true
  alert_on_error: true

# Storage settings
storage:
  base_dir: data
  logs_dir: logs
  save_raw_html: true
  save_screenshots: true

# Target configuration
target_categories: []
search_queries: []
refresh_interval: 300
"""

    config_path.write_text(default_config)
    console.print(f"[green]Created {config_path}[/green]")
    console.print("Edit this file to configure the agent for your marketplace.")


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
