"""Comprehensive audit logging for all agent actions.

Provides detailed, timestamped logging of every decision and action
for compliance, debugging, and analysis.
"""

import json
import gzip
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
import threading
from queue import Queue

import structlog

logger = structlog.get_logger(__name__)


class AuditLevel(str, Enum):
    """Audit event severity levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AuditCategory(str, Enum):
    """Categories of audit events."""

    AUTH = "auth"
    SCRAPE = "scrape"
    FILTER = "filter"
    SCORE = "score"
    BID_DECISION = "bid_decision"
    BID_EXECUTION = "bid_execution"
    CONFIG = "config"
    EXPOSURE = "exposure"
    SAFETY = "safety"
    SYSTEM = "system"
    USER = "user"


@dataclass
class AuditEvent:
    """Single audit event record."""

    timestamp: datetime
    level: AuditLevel
    category: AuditCategory
    action: str
    details: dict[str, Any]
    session_id: Optional[str] = None
    item_id: Optional[str] = None
    user_id: Optional[str] = None
    success: bool = True
    error_message: str = ""
    duration_ms: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["level"] = self.level.value
        d["category"] = self.category.value
        return d

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


class AuditLogger:
    """Comprehensive audit logging system.

    Features:
    - Timestamped event logging
    - Multiple output targets (file, structured)
    - Log rotation and compression
    - Async write queue
    - Query/search capabilities
    """

    def __init__(
        self,
        log_dir: Path,
        session_id: str,
        max_file_size_mb: int = 10,
        max_files: int = 30,
        compress_old: bool = True,
    ):
        """Initialize audit logger.

        Args:
            log_dir: Directory for audit logs.
            session_id: Current session identifier.
            max_file_size_mb: Max size before rotation.
            max_files: Max number of log files to keep.
            compress_old: Compress rotated files.
        """
        self.log_dir = log_dir
        self.session_id = session_id
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.max_files = max_files
        self.compress_old = compress_old

        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._current_file: Optional[Path] = None
        self._file_handle = None
        self._write_queue: Queue = Queue()
        self._events: list[AuditEvent] = []  # In-memory buffer for recent events
        self._max_memory_events = 1000

        self._lock = threading.Lock()
        self._start_writer_thread()
        self._rotate_if_needed()

    def _start_writer_thread(self) -> None:
        """Start background writer thread."""
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        """Background writer loop."""
        while True:
            try:
                event = self._write_queue.get()
                if event is None:  # Shutdown signal
                    break
                self._write_event(event)
            except Exception as e:
                logger.error("Audit write error", error=str(e))

    def log(
        self,
        level: AuditLevel,
        category: AuditCategory,
        action: str,
        details: Optional[dict[str, Any]] = None,
        item_id: Optional[str] = None,
        success: bool = True,
        error_message: str = "",
        duration_ms: Optional[float] = None,
    ) -> AuditEvent:
        """Log an audit event.

        Args:
            level: Event severity level.
            category: Event category.
            action: Action description.
            details: Additional details.
            item_id: Related item ID.
            success: Whether action succeeded.
            error_message: Error message if failed.
            duration_ms: Operation duration.

        Returns:
            The logged AuditEvent.
        """
        event = AuditEvent(
            timestamp=datetime.now(),
            level=level,
            category=category,
            action=action,
            details=details or {},
            session_id=self.session_id,
            item_id=item_id,
            success=success,
            error_message=error_message,
            duration_ms=duration_ms,
        )

        # Add to memory buffer
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_memory_events:
                self._events = self._events[-self._max_memory_events:]

        # Queue for file write
        self._write_queue.put(event)

        # Also log to structlog
        log_method = getattr(logger, level.value.lower(), logger.info)
        log_method(
            action,
            category=category.value,
            item_id=item_id,
            success=success,
            **details,
        )

        return event

    def log_auth(self, action: str, success: bool, details: Optional[dict] = None) -> AuditEvent:
        """Log authentication event."""
        return self.log(
            AuditLevel.INFO if success else AuditLevel.WARNING,
            AuditCategory.AUTH,
            action,
            details,
            success=success,
        )

    def log_scrape(
        self,
        action: str,
        items_found: int = 0,
        pages: int = 0,
        duration_ms: Optional[float] = None,
    ) -> AuditEvent:
        """Log scraping event."""
        return self.log(
            AuditLevel.INFO,
            AuditCategory.SCRAPE,
            action,
            {"items_found": items_found, "pages": pages},
            duration_ms=duration_ms,
        )

    def log_filter(
        self,
        item_id: str,
        passed: bool,
        reasons: Optional[list[str]] = None,
    ) -> AuditEvent:
        """Log filtering decision."""
        return self.log(
            AuditLevel.DEBUG,
            AuditCategory.FILTER,
            "filter_decision",
            {"passed": passed, "reasons": reasons or []},
            item_id=item_id,
        )

    def log_score(
        self,
        item_id: str,
        score: float,
        eligible: bool,
        components: Optional[dict] = None,
    ) -> AuditEvent:
        """Log scoring decision."""
        return self.log(
            AuditLevel.DEBUG,
            AuditCategory.SCORE,
            "score_calculation",
            {"score": score, "eligible": eligible, "components": components or {}},
            item_id=item_id,
        )

    def log_bid_decision(
        self,
        item_id: str,
        should_bid: bool,
        max_bid: Optional[float] = None,
        rejection_reason: Optional[str] = None,
    ) -> AuditEvent:
        """Log bid decision."""
        return self.log(
            AuditLevel.INFO,
            AuditCategory.BID_DECISION,
            "bid_decision",
            {
                "should_bid": should_bid,
                "max_bid": max_bid,
                "rejection_reason": rejection_reason,
            },
            item_id=item_id,
        )

    def log_bid_execution(
        self,
        item_id: str,
        amount: float,
        status: str,
        confirmation_code: Optional[str] = None,
        error: Optional[str] = None,
    ) -> AuditEvent:
        """Log bid execution."""
        success = status in ["submitted", "confirmed", "won"]
        return self.log(
            AuditLevel.INFO if success else AuditLevel.WARNING,
            AuditCategory.BID_EXECUTION,
            "bid_execution",
            {
                "amount": amount,
                "status": status,
                "confirmation_code": confirmation_code,
            },
            item_id=item_id,
            success=success,
            error_message=error or "",
        )

    def log_exposure(
        self,
        action: str,
        status: str,
        daily_exposure: float,
        can_bid: bool,
    ) -> AuditEvent:
        """Log exposure tracking event."""
        level = AuditLevel.INFO
        if status == "critical":
            level = AuditLevel.WARNING
        elif status == "exceeded":
            level = AuditLevel.CRITICAL

        return self.log(
            level,
            AuditCategory.EXPOSURE,
            action,
            {"status": status, "daily_exposure": daily_exposure, "can_bid": can_bid},
        )

    def log_safety(
        self,
        action: str,
        level: AuditLevel = AuditLevel.WARNING,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Log safety-related event."""
        return self.log(level, AuditCategory.SAFETY, action, details)

    def log_error(
        self,
        action: str,
        error: Exception,
        category: AuditCategory = AuditCategory.SYSTEM,
        item_id: Optional[str] = None,
    ) -> AuditEvent:
        """Log error event."""
        return self.log(
            AuditLevel.ERROR,
            category,
            action,
            {"error_type": type(error).__name__},
            item_id=item_id,
            success=False,
            error_message=str(error),
        )

    def _write_event(self, event: AuditEvent) -> None:
        """Write event to file.

        Args:
            event: Event to write.
        """
        self._rotate_if_needed()

        if self._file_handle is None:
            self._open_current_file()

        try:
            self._file_handle.write(event.to_json() + "\n")
            self._file_handle.flush()
        except Exception as e:
            logger.error("Failed to write audit event", error=str(e))

    def _open_current_file(self) -> None:
        """Open current log file for writing."""
        today = datetime.now().strftime("%Y%m%d")
        self._current_file = self.log_dir / f"audit_{today}.jsonl"
        self._file_handle = open(self._current_file, "a", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        """Rotate log file if needed."""
        if self._current_file is None:
            return

        if not self._current_file.exists():
            self._file_handle = None
            return

        if self._current_file.stat().st_size >= self.max_file_size:
            self._rotate_current_file()

        # Clean up old files
        self._cleanup_old_files()

    def _rotate_current_file(self) -> None:
        """Rotate current file."""
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None

        if self._current_file and self._current_file.exists():
            timestamp = datetime.now().strftime("%H%M%S")
            rotated = self._current_file.with_suffix(f".{timestamp}.jsonl")
            self._current_file.rename(rotated)

            if self.compress_old:
                self._compress_file(rotated)

    def _compress_file(self, path: Path) -> None:
        """Compress a log file.

        Args:
            path: Path to file to compress.
        """
        try:
            with open(path, "rb") as f_in:
                with gzip.open(str(path) + ".gz", "wb") as f_out:
                    f_out.writelines(f_in)
            path.unlink()
        except Exception as e:
            logger.warning("Failed to compress log file", path=str(path), error=str(e))

    def _cleanup_old_files(self) -> None:
        """Remove old log files beyond retention limit."""
        log_files = sorted(self.log_dir.glob("audit_*.jsonl*"))
        while len(log_files) > self.max_files:
            oldest = log_files.pop(0)
            try:
                oldest.unlink()
            except Exception as e:
                logger.warning("Failed to delete old log", path=str(oldest), error=str(e))

    def get_recent_events(
        self,
        count: int = 100,
        category: Optional[AuditCategory] = None,
        level: Optional[AuditLevel] = None,
    ) -> list[AuditEvent]:
        """Get recent events from memory buffer.

        Args:
            count: Number of events to return.
            category: Filter by category.
            level: Filter by level.

        Returns:
            List of recent events.
        """
        with self._lock:
            events = self._events.copy()

        if category:
            events = [e for e in events if e.category == category]
        if level:
            events = [e for e in events if e.level == level]

        return events[-count:]

    def get_session_summary(self) -> dict[str, Any]:
        """Get summary of session audit events.

        Returns:
            Summary statistics.
        """
        with self._lock:
            events = [e for e in self._events if e.session_id == self.session_id]

        return {
            "session_id": self.session_id,
            "total_events": len(events),
            "by_category": {
                cat.value: sum(1 for e in events if e.category == cat)
                for cat in AuditCategory
            },
            "by_level": {
                lvl.value: sum(1 for e in events if e.level == lvl)
                for lvl in AuditLevel
            },
            "errors": sum(1 for e in events if not e.success),
            "bids_logged": sum(1 for e in events if e.category == AuditCategory.BID_EXECUTION),
        }

    def close(self) -> None:
        """Close the audit logger."""
        self._write_queue.put(None)  # Signal shutdown
        if self._file_handle:
            self._file_handle.close()
