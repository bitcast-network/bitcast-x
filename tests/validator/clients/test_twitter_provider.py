"""
Tests for TwitterProvider base class.

Note: Username validation has been moved to bitcast.validator.utils.twitter_validators.
See test_twitter_validators.py for comprehensive validation tests.
"""

import pytest
from bitcast.validator.clients.twitter_provider import TwitterProvider
from bitcast.validator.utils.twitter_validators import is_valid_twitter_username


class TestTwitterProviderValidation:
    """Tests for username validation (delegated to twitter_validators module)."""
    
    def test_is_valid_username_valid_usernames(self):
        """Test that valid usernames are accepted."""
        # Regular usernames
        assert is_valid_twitter_username("elonmusk") is True
        assert is_valid_twitter_username("Twitter") is True
        assert is_valid_twitter_username("jack") is True
        
        # Alphanumeric usernames (common pattern)
        assert is_valid_twitter_username("user123") is True
        assert is_valid_twitter_username("test_user") is True
        assert is_valid_twitter_username("user2023") is True
        assert is_valid_twitter_username("123user") is True  # Starts with number but has letters
    
    def test_is_valid_username_numeric_ids(self):
        """Test that numeric user IDs are rejected."""
        # Pure numeric IDs (Twitter user IDs)
        assert is_valid_twitter_username("911245230426525697") is False
        assert is_valid_twitter_username("1098881129057112064") is False
        assert is_valid_twitter_username("123456789") is False
        assert is_valid_twitter_username("0") is False
    
    def test_is_valid_username_empty_and_none(self):
        """Test that empty strings and None are rejected."""
        assert is_valid_twitter_username("") is False
        assert is_valid_twitter_username(None) is False
    
    def test_is_valid_username_edge_cases(self):
        """Test edge cases."""
        # Single character (valid)
        assert is_valid_twitter_username("a") is True
        assert is_valid_twitter_username("Z") is True
        
        # Single digit (invalid - purely numeric)
        assert is_valid_twitter_username("1") is False
        assert is_valid_twitter_username("9") is False
        
        # Whitespace (invalid - empty after strip would make it falsy)
        assert is_valid_twitter_username("   ") is False
