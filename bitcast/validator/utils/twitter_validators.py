"""
Twitter username validation utilities.

Provides functions to validate Twitter usernames and filter out invalid ones
like numeric user IDs that appear when accounts are suspended/deleted.
"""
import re


def is_valid_twitter_username(username: str) -> bool:
    """
    Check if a string is a valid Twitter username (screen name).
    
    Twitter usernames must:
    - Be 1-15 characters long
    - Contain only letters, numbers, and underscores
    - NOT be purely numeric (those are user IDs, not usernames)
    
    Args:
        username: The username to validate (with or without @ prefix)
        
    Returns:
        True if valid username, False if invalid (including numeric IDs)
        
    Examples:
        >>> is_valid_twitter_username("elonmusk")
        True
        >>> is_valid_twitter_username("user_123")
        True
        >>> is_valid_twitter_username("911245230426525697")  # User ID
        False
        >>> is_valid_twitter_username("a" * 16)  # Too long
        False
    """
    if not username:
        return False
    
    # Remove @ prefix if present
    clean_username = username.lstrip('@')
    
    # Check length (1-15 characters for Twitter usernames)
    if not (1 <= len(clean_username) <= 15):
        return False
    
    # Check format: only letters, numbers, underscores
    if not re.match(r'^[a-zA-Z0-9_]+$', clean_username):
        return False
    
    # Reject purely numeric strings (these are user IDs, not usernames)
    # Suspended/deleted accounts often appear as numeric IDs
    if re.match(r'^\d+$', clean_username):
        return False
    
    return True


def filter_valid_usernames(usernames: list[str]) -> list[str]:
    """
    Filter a list of usernames to only include valid Twitter usernames.
    
    Removes numeric user IDs and other invalid formats.
    
    Args:
        usernames: List of usernames to filter
        
    Returns:
        List containing only valid usernames
        
    Examples:
        >>> filter_valid_usernames(["elonmusk", "911245230426525697", "user_123"])
        ["elonmusk", "user_123"]
    """
    return [u for u in usernames if is_valid_twitter_username(u)]
