"""
Tests for Twitter username validation utilities.
"""
import pytest
from bitcast.validator.utils.twitter_validators import (
    is_valid_twitter_username,
    filter_valid_usernames
)


class TestIsValidTwitterUsername:
    """Test is_valid_twitter_username function."""
    
    def test_valid_usernames(self):
        """Test that valid usernames are accepted."""
        valid_usernames = [
            "elonmusk",
            "jack",
            "a",  # Minimum length
            "user_123",
            "test_user_999",
            "ABC123xyz",
            "x" * 15,  # Maximum length
        ]
        for username in valid_usernames:
            assert is_valid_twitter_username(username), f"Should accept {username}"
    
    def test_valid_usernames_with_at_prefix(self):
        """Test that usernames with @ prefix are handled correctly."""
        assert is_valid_twitter_username("@elonmusk")
        assert is_valid_twitter_username("@user_123")
    
    def test_numeric_ids_rejected(self):
        """Test that numeric user IDs are rejected."""
        numeric_ids = [
            "911245230426525697",
            "1098881129057112064",
            "123",
            "999999999999999999",
        ]
        for user_id in numeric_ids:
            assert not is_valid_twitter_username(user_id), f"Should reject numeric ID {user_id}"
    
    def test_numeric_ids_with_at_prefix_rejected(self):
        """Test that numeric user IDs with @ prefix are rejected."""
        assert not is_valid_twitter_username("@911245230426525697")
        assert not is_valid_twitter_username("@1098881129057112064")
    
    def test_too_long_rejected(self):
        """Test that usernames longer than 15 characters are rejected."""
        assert not is_valid_twitter_username("a" * 16)
        assert not is_valid_twitter_username("verylongusername123")
    
    def test_empty_rejected(self):
        """Test that empty strings are rejected."""
        assert not is_valid_twitter_username("")
        assert not is_valid_twitter_username("@")
    
    def test_invalid_characters_rejected(self):
        """Test that usernames with invalid characters are rejected."""
        invalid_usernames = [
            "user name",  # Space
            "user-name",  # Hyphen
            "user.name",  # Period
            "user@name",  # @ in middle
            "user!",      # Exclamation
            "user#tag",   # Hash
        ]
        for username in invalid_usernames:
            assert not is_valid_twitter_username(username), f"Should reject {username}"


class TestFilterValidUsernames:
    """Test filter_valid_usernames function."""
    
    def test_filter_mixed_list(self):
        """Test filtering a list with valid and invalid usernames."""
        usernames = [
            "elonmusk",              # Valid
            "911245230426525697",    # Invalid - numeric ID
            "user_123",              # Valid
            "1098881129057112064",   # Invalid - numeric ID
            "jack",                  # Valid
            "999999999",             # Invalid - numeric ID
            "test_user",             # Valid
        ]
        expected = ["elonmusk", "user_123", "jack", "test_user"]
        assert filter_valid_usernames(usernames) == expected
    
    def test_filter_all_valid(self):
        """Test filtering a list with all valid usernames."""
        usernames = ["elonmusk", "jack", "user_123"]
        assert filter_valid_usernames(usernames) == usernames
    
    def test_filter_all_invalid(self):
        """Test filtering a list with all invalid usernames."""
        usernames = ["911245230426525697", "1098881129057112064", "123456"]
        assert filter_valid_usernames(usernames) == []
    
    def test_filter_empty_list(self):
        """Test filtering an empty list."""
        assert filter_valid_usernames([]) == []
