"""Tests for date parsing utilities."""

import pytest
from datetime import datetime, timezone

from bitcast.validator.utils.date_utils import parse_brief_date


class TestParseBriefDate:
    """Tests for parse_brief_date function."""
    
    def test_simple_date_start_of_day(self):
        """Simple date format defaults to start of day (00:00:00)."""
        result = parse_brief_date('2025-11-25')
        
        expected = datetime(2025, 11, 25, 0, 0, 0, tzinfo=timezone.utc)
        assert result == expected
    
    def test_simple_date_end_of_day(self):
        """Simple date format with end_of_day=True sets to 23:59:59."""
        result = parse_brief_date('2025-11-25', end_of_day=True)
        
        expected = datetime(2025, 11, 25, 23, 59, 59, tzinfo=timezone.utc)
        assert result == expected
    
    def test_iso_timestamp_with_z(self):
        """ISO timestamp with Z suffix is parsed correctly."""
        result = parse_brief_date('2025-11-25T14:30:00Z')
        
        expected = datetime(2025, 11, 25, 14, 30, 0, tzinfo=timezone.utc)
        assert result == expected
    
    def test_iso_timestamp_with_timezone(self):
        """ISO timestamp with timezone is converted to UTC."""
        result = parse_brief_date('2025-11-25T14:30:00+05:00')
        
        # Should be converted to UTC (14:30 + 5:00 = 09:30 UTC)
        expected = datetime(2025, 11, 25, 9, 30, 0, tzinfo=timezone.utc)
        assert result == expected
    
    def test_iso_timestamp_ignores_end_of_day(self):
        """ISO timestamp ignores end_of_day parameter (preserves time)."""
        result = parse_brief_date('2025-11-25T14:30:00Z', end_of_day=True)
        
        # Should preserve the time from ISO, not change to end of day
        expected = datetime(2025, 11, 25, 14, 30, 0, tzinfo=timezone.utc)
        assert result == expected
    
    def test_none_returns_none(self):
        """None input returns None."""
        assert parse_brief_date(None) is None
    
    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert parse_brief_date('') is None
    
    def test_invalid_date_returns_none(self):
        """Invalid date string returns None and logs warning."""
        result = parse_brief_date('not-a-date')
        assert result is None
    
    def test_result_is_timezone_aware(self):
        """All results are timezone-aware."""
        result = parse_brief_date('2025-11-25')
        
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

