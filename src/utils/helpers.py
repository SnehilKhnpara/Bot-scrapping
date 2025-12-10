"""General utility functions for the Marketplace Automation Agent."""

import asyncio
import random
import re
import string
from datetime import datetime
from pathlib import Path
from typing import Any


def generate_random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> float:
    """Generate a random delay for human-like behavior.

    Uses a slightly weighted distribution to make delays feel more natural.

    Args:
        min_seconds: Minimum delay in seconds.
        max_seconds: Maximum delay in seconds.

    Returns:
        Random delay value in seconds.
    """
    # Use triangular distribution for more natural feeling delays
    # Mode is set to 40% of the range for slightly faster average
    mode = min_seconds + (max_seconds - min_seconds) * 0.4
    return random.triangular(min_seconds, max_seconds, mode)


async def async_random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    """Async version of random delay for use in async code.

    Args:
        min_seconds: Minimum delay in seconds.
        max_seconds: Maximum delay in seconds.
    """
    delay = generate_random_delay(min_seconds, max_seconds)
    await asyncio.sleep(delay)


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """Sanitize a string to be safe for use as a filename.

    Args:
        filename: Original filename string.
        max_length: Maximum allowed length.

    Returns:
        Sanitized filename safe for filesystem use.
    """
    # Remove or replace invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
    # Remove leading/trailing spaces and dots
    sanitized = sanitized.strip(' .')
    # Truncate if necessary
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
    # Ensure not empty
    if not sanitized:
        sanitized = "unnamed"
    return sanitized


def ensure_directory(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary.

    Args:
        path: Path to the directory.

    Returns:
        The same path for chaining.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def format_currency(amount: float, currency: str = "USD", decimals: int = 2) -> str:
    """Format a number as currency.

    Args:
        amount: Numeric amount to format.
        currency: Currency code (default: USD).
        decimals: Number of decimal places.

    Returns:
        Formatted currency string.
    """
    symbols = {
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "AUD": "A$",
        "CAD": "C$",
    }
    symbol = symbols.get(currency, currency + " ")
    return f"{symbol}{amount:,.{decimals}f}"


def truncate_string(text: str, max_length: int = 50, suffix: str = "...") -> str:
    """Truncate a string to a maximum length with suffix.

    Args:
        text: String to truncate.
        max_length: Maximum length including suffix.
        suffix: String to append when truncating.

    Returns:
        Truncated string or original if short enough.
    """
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def generate_session_id() -> str:
    """Generate a unique session identifier.

    Returns:
        Unique session ID string.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"session_{timestamp}_{random_suffix}"


def parse_price(price_str: str) -> float | None:
    """Parse a price string to a float value.

    Handles common formats like $1,234.56, €1.234,56, etc.

    Args:
        price_str: Price string to parse.

    Returns:
        Float value or None if parsing fails.
    """
    if not price_str:
        return None

    # Remove currency symbols and whitespace
    cleaned = re.sub(r'[^\d.,\-]', '', price_str.strip())

    if not cleaned:
        return None

    # Handle different decimal separators
    # If both . and , exist, determine which is decimal separator
    if '.' in cleaned and ',' in cleaned:
        # If comma comes after period, comma is decimal separator (European)
        if cleaned.rfind(',') > cleaned.rfind('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            # Period is decimal separator (US/UK)
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        # Check if comma is likely a decimal separator
        parts = cleaned.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            cleaned = cleaned.replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')

    try:
        return float(cleaned)
    except ValueError:
        return None


def calculate_percentage_diff(old_value: float, new_value: float) -> float:
    """Calculate percentage difference between two values.

    Args:
        old_value: Original value.
        new_value: New value.

    Returns:
        Percentage difference (positive = increase, negative = decrease).
    """
    if old_value == 0:
        return 100.0 if new_value > 0 else -100.0 if new_value < 0 else 0.0
    return ((new_value - old_value) / abs(old_value)) * 100


def chunks(lst: list[Any], n: int):
    """Yield successive n-sized chunks from a list.

    Args:
        lst: List to chunk.
        n: Chunk size.

    Yields:
        List chunks of size n.
    """
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries.

    Args:
        base: Base dictionary.
        override: Dictionary with values to override.

    Returns:
        Merged dictionary.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
