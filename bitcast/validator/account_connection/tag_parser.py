"""
Tag parser for extracting connection tags from tweet text.

Supports two tag types:
- bitcast-hk:{substrate_hotkey} (e.g., bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq)
- bitcast-x{identifier} (no-code mining tag, e.g., bitcast-xabc123)

Tags are case-insensitive.
"""

import re
from typing import List, Tuple


class TagParser:
    """Extract and validate connection tags from tweet text."""
    
    # Case-insensitive regex patterns for both tag types
    # Hotkey pattern: matches substrate addresses (base58, typically 47-48 chars)
    BITCAST_HK_PATTERN = re.compile(r'bitcast-hk:([1-9A-HJ-NP-Za-km-z]{47,48})', re.IGNORECASE)
    
    # X pattern: matches bitcast-x followed by alphanumeric identifier (no-code mining)
    # Example: bitcast-xabc123
    BITCAST_X_PATTERN = re.compile(r'bitcast-x([a-z0-9]+)', re.IGNORECASE)
    
    @staticmethod
    def extract_tags(tweet_text: str) -> List[Tuple[str, str]]:
        """
        Extract connection tags from tweet text.
        
        Args:
            tweet_text: The text content of a tweet
            
        Returns:
            List of tuples: [(tag_type, full_tag), ...]
            where tag_type is 'HK' or 'X' and full_tag is the complete tag string
            
        Example:
            >>> TagParser.extract_tags("Check out bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq and bitcast-xabc123")
            [('HK', 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq'), ('X', 'bitcast-xabc123')]
        """
        if not tweet_text:
            return []
        
        tags = []
        
        # Find all HK (hotkey) tags
        for match in TagParser.BITCAST_HK_PATTERN.finditer(tweet_text):
            hotkey = match.group(1)
            # Reconstruct the full tag
            full_tag = f"bitcast-hk:{hotkey}"
            tags.append(('HK', full_tag))
        
        # Find all X tags (no-code mining)
        for match in TagParser.BITCAST_X_PATTERN.finditer(tweet_text):
            identifier = match.group(1)
            # Reconstruct the full tag with identifier
            full_tag = f"bitcast-x{identifier}"
            tags.append(('X', full_tag))
        
        return tags
    
    @staticmethod
    def is_valid_tag(tag: str) -> bool:
        """
        Check if a string is a valid connection tag.
        
        Args:
            tag: String to validate
            
        Returns:
            True if tag matches expected format, False otherwise
        """
        if not tag:
            return False
        
        # Check if it matches either pattern
        hk_match = TagParser.BITCAST_HK_PATTERN.fullmatch(tag)
        x_match = TagParser.BITCAST_X_PATTERN.fullmatch(tag)
        
        return hk_match is not None or x_match is not None

