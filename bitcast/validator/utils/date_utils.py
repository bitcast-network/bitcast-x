"""Date parsing utilities for brief date handling."""

from datetime import datetime, timezone
from typing import Optional
import bittensor as bt


def parse_brief_date(date_str: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    """
    Parse date string from brief to timezone-aware UTC datetime.
    
    Handles both full ISO timestamps and simple date formats.
    For simple date formats (YYYY-MM-DD), can set to either start or end of day.
    
    Args:
        date_str: Date string in format 'YYYY-MM-DD' or ISO format with time
        end_of_day: If True and date_str is simple date format, set time to 23:59:59
                   If False, set to 00:00:00
                   (Useful for end_date to include entire day)
        
    Returns:
        Timezone-aware datetime in UTC, or None if date_str is None/empty
        
    Examples:
        >>> parse_brief_date('2025-11-25')
        datetime(2025, 11, 25, 0, 0, 0, tzinfo=timezone.utc)
        
        >>> parse_brief_date('2025-11-25', end_of_day=True)
        datetime(2025, 11, 25, 23, 59, 59, tzinfo=timezone.utc)
        
        >>> parse_brief_date('2025-11-25T14:30:00Z')
        datetime(2025, 11, 25, 14, 30, 0, tzinfo=timezone.utc)
    """
    if not date_str:
        return None
    
    try:
        # Check if date string has time component (ISO format or timestamp)
        if 'T' in date_str or ':' in date_str:
            # Full timestamp - parse as-is
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return dt.astimezone(timezone.utc)
        
        # Simple date format 'YYYY-MM-DD'
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        
        if end_of_day:
            # Set to end of day (23:59:59) to include all tweets on that day
            dt = dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        else:
            # Set to start of day (00:00:00)
            dt = dt.replace(tzinfo=timezone.utc)
        
        return dt
        
    except (ValueError, AttributeError) as e:
        bt.logging.warning(f"Failed to parse date '{date_str}': {e}")
        return None

