"""
Tag parser for extracting connection tags from tweet text.

Supports two tag types, each with an optional referral code suffix:
- bitcast-hk:{substrate_hotkey}[-{referral_code}]
- bitcast-x{identifier}[-{referral_code}]

Tags are case-insensitive.
"""

import re
from typing import List, NamedTuple, Optional
from .referral_code import decode_referral_code


class ParsedTag(NamedTuple):
    """Result of parsing a connection tag from tweet text."""
    tag_type: str           # 'HK' or 'X'
    full_tag: str           # Complete tag string including referral suffix
    referred_by: Optional[str]  # Decoded X handle of referrer, or None
    referral_code: Optional[str]  # Raw referral code string, or None


class TagParser:
    """Extract and validate connection tags from tweet text."""
    
    # Hotkey pattern: substrate address (base58, 47-48 chars) with optional referral code
    BITCAST_HK_PATTERN = re.compile(r'bitcast-hk:([1-9A-HJ-NP-Za-km-z]{47,48})(?:-([a-z0-9_-]+))?', re.IGNORECASE)
    
    # X pattern: alphanumeric identifier with optional referral code
    BITCAST_X_PATTERN = re.compile(r'bitcast-x([a-z0-9]+)(?:-([a-z0-9_-]+))?', re.IGNORECASE)
    
    @staticmethod
    def extract_tags(tweet_text: str) -> List[ParsedTag]:
        """
        Extract connection tags from tweet text.
        
        Args:
            tweet_text: The text content of a tweet
            
        Returns:
            List of ParsedTag tuples with tag_type, full_tag, referred_by, referral_code
        """
        if not tweet_text:
            return []
        
        tags = []
        
        for match in TagParser.BITCAST_HK_PATTERN.finditer(tweet_text):
            hotkey = match.group(1)
            raw_referral = match.group(2)
            
            full_tag = f"bitcast-hk:{hotkey}"
            if raw_referral:
                full_tag = f"{full_tag}-{raw_referral}"
            
            referred_by = decode_referral_code(raw_referral) if raw_referral else None
            tags.append(ParsedTag('HK', full_tag, referred_by, raw_referral))
        
        for match in TagParser.BITCAST_X_PATTERN.finditer(tweet_text):
            identifier = match.group(1)
            raw_referral = match.group(2)
            
            full_tag = f"bitcast-x{identifier}"
            if raw_referral:
                full_tag = f"{full_tag}-{raw_referral}"
            
            referred_by = decode_referral_code(raw_referral) if raw_referral else None
            tags.append(ParsedTag('X', full_tag, referred_by, raw_referral))
        
        return tags
    
    @staticmethod
    def is_valid_tag(tag: str) -> bool:
        """Check if a string is a valid connection tag."""
        if not tag:
            return False
        
        hk_match = TagParser.BITCAST_HK_PATTERN.fullmatch(tag)
        x_match = TagParser.BITCAST_X_PATTERN.fullmatch(tag)
        
        return hk_match is not None or x_match is not None

