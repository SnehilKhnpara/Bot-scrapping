"""Pytest configuration and fixtures."""

import pytest
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "mode": "DRY_RUN",
        "headless": True,
        "marketplace": {
            "name": "test_marketplace",
            "base_url": "https://example.com",
            "listings_url": "https://example.com/listings",
        },
    }


@pytest.fixture
def temp_data_dir(tmp_path):
    """Create temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "raw").mkdir()
    (data_dir / "processed").mkdir()
    (data_dir / "reports").mkdir()
    return data_dir


@pytest.fixture
def temp_logs_dir(tmp_path):
    """Create temporary logs directory."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return logs_dir
