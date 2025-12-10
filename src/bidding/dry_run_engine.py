"""DRY_RUN mode simulation engine.

Simulates the full bidding workflow without executing real bids.
Generates comprehensive reports for testing and validation.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog

from ..scraper.normalizer import NormalizedListing
from ..scraper.listing_scraper import ScrapeResult
from ..scoring.quality_filter import QualityFilter, FilterResult
from ..scoring.scoring_engine import ScoringEngine, ItemScore
from .bid_calculator import BidCalculator, BidDecision

logger = structlog.get_logger(__name__)


@dataclass
class SimulatedBid:
    """A simulated bid decision."""

    listing: NormalizedListing
    filter_result: FilterResult
    score: Optional[ItemScore]
    decision: Optional[BidDecision]
    would_bid: bool
    reason: str


@dataclass
class DryRunResult:
    """Complete result of a dry run simulation."""

    # Run metadata
    run_id: str
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    mode: str = "DRY_RUN"

    # Scraping summary
    scrape_result: Optional[ScrapeResult] = None
    total_listings_found: int = 0

    # Processing summary
    total_processed: int = 0
    passed_filter: int = 0
    passed_scoring: int = 0
    would_bid_count: int = 0

    # Detailed results
    simulated_bids: list[SimulatedBid] = field(default_factory=list)
    filtered_items: list[tuple[str, str]] = field(default_factory=list)  # (item_id, reason)
    low_score_items: list[tuple[str, float]] = field(default_factory=list)  # (item_id, score)
    rejected_bids: list[tuple[str, str]] = field(default_factory=list)  # (item_id, reason)

    # Budget simulation
    simulated_exposure: float = 0.0
    simulated_spend: float = 0.0
    budget_status: dict[str, Any] = field(default_factory=dict)

    # Errors and warnings
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "summary": {
                "total_listings_found": self.total_listings_found,
                "total_processed": self.total_processed,
                "passed_filter": self.passed_filter,
                "passed_scoring": self.passed_scoring,
                "would_bid_count": self.would_bid_count,
            },
            "budget_simulation": {
                "simulated_exposure": self.simulated_exposure,
                "simulated_spend": self.simulated_spend,
                "budget_status": self.budget_status,
            },
            "detailed_bids": [
                {
                    "item_id": sb.listing.item_id,
                    "title": sb.listing.title,
                    "current_price": sb.listing.current_price,
                    "would_bid": sb.would_bid,
                    "max_bid": sb.decision.max_bid if sb.decision else None,
                    "recommended_bid": sb.decision.recommended_bid if sb.decision else None,
                    "score": sb.score.total_score if sb.score else None,
                    "reason": sb.reason,
                }
                for sb in self.simulated_bids
                if sb.would_bid
            ],
            "filtered_items": self.filtered_items,
            "low_score_items": self.low_score_items,
            "rejected_bids": self.rejected_bids,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class DryRunEngine:
    """Execute full bidding simulation without real transactions.

    Shares 90%+ code logic with LIVE engine but:
    - Does not submit actual bids
    - Generates comprehensive simulation reports
    - Allows comparison with LIVE decisions
    - Safe for testing and validation
    """

    def __init__(
        self,
        quality_filter: QualityFilter,
        scoring_engine: ScoringEngine,
        bid_calculator: BidCalculator,
        output_dir: Path,
    ):
        """Initialize dry run engine.

        Args:
            quality_filter: Quality filter instance.
            scoring_engine: Scoring engine instance.
            bid_calculator: Bid calculator instance.
            output_dir: Directory for output reports.
        """
        self.quality_filter = quality_filter
        self.scoring_engine = scoring_engine
        self.bid_calculator = bid_calculator
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        listings: list[NormalizedListing],
        scrape_result: Optional[ScrapeResult] = None,
    ) -> DryRunResult:
        """Execute dry run simulation.

        Args:
            listings: List of normalized listings to process.
            scrape_result: Optional scrape result for metadata.

        Returns:
            DryRunResult with complete simulation data.
        """
        run_id = f"dry_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        started_at = datetime.now()

        logger.info("Starting DRY_RUN simulation", run_id=run_id, listings=len(listings))

        result = DryRunResult(
            run_id=run_id,
            started_at=started_at,
            completed_at=started_at,  # Will update
            duration_seconds=0,
            scrape_result=scrape_result,
            total_listings_found=len(listings),
        )

        try:
            # Process each listing through the full pipeline
            for listing in listings:
                self._process_listing(listing, result)

            # Calculate simulated budget impact
            result.budget_status = self.bid_calculator.get_budget_status()

        except Exception as e:
            error_msg = f"DRY_RUN error: {str(e)}"
            result.errors.append(error_msg)
            logger.error(error_msg)

        completed_at = datetime.now()
        result.completed_at = completed_at
        result.duration_seconds = (completed_at - started_at).total_seconds()

        # Generate report
        self._save_report(result)

        logger.info(
            "DRY_RUN complete",
            run_id=run_id,
            processed=result.total_processed,
            would_bid=result.would_bid_count,
            duration=f"{result.duration_seconds:.2f}s",
        )

        return result

    def _process_listing(
        self, listing: NormalizedListing, result: DryRunResult
    ) -> None:
        """Process a single listing through the simulation pipeline.

        Args:
            listing: Listing to process.
            result: DryRunResult to update.
        """
        result.total_processed += 1

        # Step 1: Quality filter
        filter_result = self.quality_filter.filter(listing)

        if not filter_result.passed:
            reasons = [r.value for r in filter_result.reasons]
            result.filtered_items.append((listing.item_id, ", ".join(reasons)))
            result.simulated_bids.append(
                SimulatedBid(
                    listing=listing,
                    filter_result=filter_result,
                    score=None,
                    decision=None,
                    would_bid=False,
                    reason=f"Filtered: {', '.join(reasons)}",
                )
            )
            return

        result.passed_filter += 1

        # Step 2: Scoring
        score = self.scoring_engine.score(listing)

        if not score.eligible:
            result.low_score_items.append((listing.item_id, score.total_score))
            result.simulated_bids.append(
                SimulatedBid(
                    listing=listing,
                    filter_result=filter_result,
                    score=score,
                    decision=None,
                    would_bid=False,
                    reason=f"Low score: {score.total_score:.1f}",
                )
            )
            return

        result.passed_scoring += 1

        # Step 3: Bid calculation
        decision = self.bid_calculator.calculate_bid(listing, score)

        if not decision.should_bid:
            reason = decision.rejection_reason.value if decision.rejection_reason else "unknown"
            result.rejected_bids.append((listing.item_id, f"{reason}: {decision.rejection_detail}"))
            result.simulated_bids.append(
                SimulatedBid(
                    listing=listing,
                    filter_result=filter_result,
                    score=score,
                    decision=decision,
                    would_bid=False,
                    reason=f"Rejected: {reason}",
                )
            )
            return

        # Would bid!
        result.would_bid_count += 1
        result.simulated_exposure += decision.max_bid or 0
        result.simulated_spend += decision.recommended_bid or 0

        result.simulated_bids.append(
            SimulatedBid(
                listing=listing,
                filter_result=filter_result,
                score=score,
                decision=decision,
                would_bid=True,
                reason=f"Would bid ${decision.recommended_bid:.2f} (max: ${decision.max_bid:.2f})",
            )
        )

        # Simulate recording the bid for budget tracking
        self.bid_calculator.record_bid(
            listing.item_id,
            decision.max_bid or 0,
            listing.category,
        )

        logger.debug(
            "Simulated bid",
            item_id=listing.item_id,
            max_bid=decision.max_bid,
            recommended=decision.recommended_bid,
            score=score.total_score,
        )

    def _save_report(self, result: DryRunResult) -> Path:
        """Save dry run report to file.

        Args:
            result: DryRunResult to save.

        Returns:
            Path to saved report.
        """
        filename = f"{result.run_id}_report.json"
        filepath = self.output_dir / filename

        try:
            report_data = result.to_dict()
            filepath.write_text(json.dumps(report_data, indent=2, default=str))
            logger.info("DRY_RUN report saved", path=str(filepath))
        except Exception as e:
            logger.error("Failed to save report", error=str(e))

        # Also save summary
        summary_path = self.output_dir / f"{result.run_id}_summary.txt"
        self._save_summary(result, summary_path)

        return filepath

    def _save_summary(self, result: DryRunResult, filepath: Path) -> None:
        """Save human-readable summary.

        Args:
            result: DryRunResult.
            filepath: Path to save summary.
        """
        lines = [
            "=" * 60,
            "DRY RUN SIMULATION SUMMARY",
            "=" * 60,
            "",
            f"Run ID: {result.run_id}",
            f"Started: {result.started_at}",
            f"Duration: {result.duration_seconds:.2f} seconds",
            "",
            "PROCESSING SUMMARY",
            "-" * 40,
            f"Total listings found: {result.total_listings_found}",
            f"Total processed: {result.total_processed}",
            f"Passed quality filter: {result.passed_filter}",
            f"Passed scoring: {result.passed_scoring}",
            f"Would bid on: {result.would_bid_count}",
            "",
            "BUDGET SIMULATION",
            "-" * 40,
            f"Simulated exposure: ${result.simulated_exposure:.2f}",
            f"Simulated spend: ${result.simulated_spend:.2f}",
            "",
        ]

        if result.would_bid_count > 0:
            lines.extend([
                "WOULD BID ON",
                "-" * 40,
            ])
            for sb in result.simulated_bids:
                if sb.would_bid:
                    lines.append(
                        f"  - {sb.listing.title[:50]}..."
                        f"\n    Current: ${sb.listing.current_price:.2f}"
                        f" | Max bid: ${sb.decision.max_bid:.2f}"
                        f" | Score: {sb.score.total_score:.1f}"
                    )
            lines.append("")

        if result.errors:
            lines.extend([
                "ERRORS",
                "-" * 40,
            ])
            lines.extend(f"  - {e}" for e in result.errors)
            lines.append("")

        try:
            filepath.write_text("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to save summary", error=str(e))

    def compare_with_live(
        self, dry_result: DryRunResult, live_decisions: list[BidDecision]
    ) -> dict[str, Any]:
        """Compare dry run results with live decisions.

        Args:
            dry_result: Dry run result.
            live_decisions: List of live bid decisions.

        Returns:
            Comparison report.
        """
        dry_bids = {
            sb.listing.item_id: sb
            for sb in dry_result.simulated_bids
            if sb.would_bid
        }

        live_bids = {d.item_id: d for d in live_decisions if d.should_bid}

        # Find discrepancies
        dry_only = set(dry_bids.keys()) - set(live_bids.keys())
        live_only = set(live_bids.keys()) - set(dry_bids.keys())
        both = set(dry_bids.keys()) & set(live_bids.keys())

        amount_diffs = []
        for item_id in both:
            dry_max = dry_bids[item_id].decision.max_bid
            live_max = live_bids[item_id].max_bid
            if dry_max and live_max and abs(dry_max - live_max) > 0.01:
                amount_diffs.append({
                    "item_id": item_id,
                    "dry_max": dry_max,
                    "live_max": live_max,
                    "difference": live_max - dry_max,
                })

        return {
            "dry_run_count": len(dry_bids),
            "live_count": len(live_bids),
            "matching_items": len(both),
            "dry_only": list(dry_only),
            "live_only": list(live_only),
            "amount_differences": amount_diffs,
            "consistency_score": len(both) / max(len(dry_bids), len(live_bids), 1) * 100,
        }
