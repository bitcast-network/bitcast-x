"""Tests for tweet_scorer module."""

import pytest
from datetime import datetime, timezone
from bitcast.validator.tweet_scoring.tweet_scorer import filter_tweets_by_date


class TestFilterTweetsByDate:
    """Test filter_tweets_by_date function."""
    
    def test_filter_tweets_with_date_range(self):
        """Test filtering tweets within date range, including boundary behavior."""
        tweets = [
            {'tweet_id': '1', 'created_at': 'Mon Jan 01 00:00:00 +0000 2024'},
            {'tweet_id': '2', 'created_at': 'Wed Jan 03 12:00:00 +0000 2024'},
            {'tweet_id': '3', 'created_at': 'Fri Jan 05 12:00:00 +0000 2024'},
            {'tweet_id': '4', 'created_at': 'Sun Jan 07 23:59:59 +0000 2024'},
            {'tweet_id': '5', 'created_at': 'Mon Jan 08 00:00:01 +0000 2024'},
        ]
        
        cutoff_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cutoff_end = datetime(2024, 1, 7, 23, 59, 59, tzinfo=timezone.utc)
        
        result = filter_tweets_by_date(tweets, cutoff_start, cutoff_end)
        
        # Should include tweets 1-4 (within range, inclusive), exclude 5 (after cutoff)
        assert len(result) == 4
        result_ids = [t['tweet_id'] for t in result]
        assert '1' in result_ids
        assert '2' in result_ids
        assert '3' in result_ids
        assert '4' in result_ids
        assert '5' not in result_ids
    
    def test_filter_tweets_start_only(self):
        """Test filtering tweets with only start date (no end date)."""
        tweets = [
            {'tweet_id': '1', 'created_at': 'Mon Jan 01 12:00:00 +0000 2024'},
            {'tweet_id': '2', 'created_at': 'Wed Jan 03 12:00:00 +0000 2024'},
            {'tweet_id': '3', 'created_at': 'Fri Jan 05 12:00:00 +0000 2024'},
        ]
        
        cutoff_start = datetime(2024, 1, 3, tzinfo=timezone.utc)
        
        result = filter_tweets_by_date(tweets, cutoff_start, cutoff_end=None)
        
        # Should include tweets on or after Jan 3
        assert len(result) == 2
        assert result[0]['tweet_id'] == '2'
        assert result[1]['tweet_id'] == '3'
    
    def test_filter_tweets_edge_cases(self):
        """Test edge cases: missing created_at, unparseable dates, empty list."""
        # Missing and unparseable dates
        tweets = [
            {'tweet_id': '1', 'created_at': 'Mon Jan 01 12:00:00 +0000 2024'},
            {'tweet_id': '2'},  # Missing created_at - should be skipped
            {'tweet_id': '3', 'created_at': 'invalid-date'},  # Unparseable - included permissively
            {'tweet_id': '4', 'created_at': 'Fri Jan 05 12:00:00 +0000 2024'},
        ]
        
        cutoff_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cutoff_end = datetime(2024, 1, 7, tzinfo=timezone.utc)
        result = filter_tweets_by_date(tweets, cutoff_start, cutoff_end)
        
        # Should skip missing, include unparseable permissively
        assert len(result) == 3
        result_ids = [t['tweet_id'] for t in result]
        assert '1' in result_ids
        assert '2' not in result_ids
        assert '3' in result_ids  # Unparseable but included
        assert '4' in result_ids
        
        # Empty list
        assert filter_tweets_by_date([], cutoff_start, cutoff_end) == []
    
    def test_filter_tweets_timezone_handling(self):
        """Test that timezone is properly handled (Twitter dates have timezone info)."""
        tweets = [
            {'tweet_id': '1', 'created_at': 'Mon Jan 01 12:00:00 +0000 2024'},
            {'tweet_id': '2', 'created_at': 'Wed Jan 03 12:00:00 -0500 2024'},  # Different timezone
        ]
        
        # Cutoffs use timezone-aware UTC datetime
        cutoff_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cutoff_end = datetime(2024, 1, 7, tzinfo=timezone.utc)
        
        result = filter_tweets_by_date(tweets, cutoff_start, cutoff_end)
        
        # Both should be included (timezone is normalized to UTC)
        assert len(result) == 2

