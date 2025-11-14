"""
Unit tests for TagParser.
"""

import pytest
from bitcast.validator.account_connection.tag_parser import TagParser


class TestTagParser:
    """Test tag extraction and validation."""
    
    def test_extract_hk_tag(self):
        """Test extraction of bitcast-hk tags with substrate hotkey."""
        text = "Check out bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq for more info"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 1
        assert tags[0] == ("HK", "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq")
    
    def test_extract_x_tag(self):
        """Test extraction of bitcast-x tags."""
        text = "Visit bitcast-xxyz78900"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 1
        assert tags[0] == ("X", "bitcast-xxyz78900")
    
    def test_extract_multiple_tags(self):
        """Test extraction of multiple tags from one tweet."""
        text = "Check bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq and bitcast-xxyz78900 now!"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 2
        assert tags[0] == ("HK", "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq")
        assert tags[1] == ("X", "bitcast-xxyz78900")
    
    def test_case_insensitive_matching(self):
        """Test that tags are matched case-insensitively."""
        text1 = "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"  # lowercase prefix
        text2 = "BITCAST-HK:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"  # uppercase prefix
        text3 = "BiTcAsT-Hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"  # mixed case prefix
        
        tags1 = TagParser.extract_tags(text1)
        tags2 = TagParser.extract_tags(text2)
        tags3 = TagParser.extract_tags(text3)
        
        assert len(tags1) == 1
        assert len(tags2) == 1
        assert len(tags3) == 1
    
    def test_no_tags_found(self):
        """Test tweet with no tags."""
        text = "This is just a regular tweet with no connection tags"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 0
    
    def test_empty_string(self):
        """Test empty string input."""
        tags = TagParser.extract_tags("")
        assert len(tags) == 0
    
    def test_none_input(self):
        """Test None input."""
        tags = TagParser.extract_tags(None)
        assert len(tags) == 0
    
    def test_malformed_tag_wrong_length(self):
        """Test that tags with wrong hotkey length are not matched."""
        text1 = "bitcast-hk:5DNm"  # Too short (4 chars, need 47-48)
        text2 = "bitcast-hk:toolongbutnotbase58characters12345678"  # Wrong length and chars
        
        tags1 = TagParser.extract_tags(text1)
        tags2 = TagParser.extract_tags(text2)
        
        assert len(tags1) == 0
        assert len(tags2) == 0
    
    def test_malformed_tag_wrong_separator(self):
        """Test that tags with wrong separator are not matched."""
        text1 = "bitcast-hk{5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq}"  # Curly braces
        text2 = "bitcast-hk-5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"  # Hyphen instead of colon
        
        tags1 = TagParser.extract_tags(text1)
        tags2 = TagParser.extract_tags(text2)
        
        assert len(tags1) == 0
        assert len(tags2) == 0
    
    def test_malformed_tag_invalid_base58(self):
        """Test that tags with invalid base58 characters are not matched."""
        text = "bitcast-hk:0OIlabc12345678901234567890123456789012345678"  # Contains 0, O, I, l
        tags = TagParser.extract_tags(text)
        assert len(tags) == 0
    
    def test_valid_substrate_addresses(self):
        """Test various valid substrate addresses."""
        valid_hotkeys = [
            "5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",  # 48 chars
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",  # Another valid
            "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",  # Another valid
        ]
        
        for hotkey in valid_hotkeys:
            text = f"bitcast-hk:{hotkey}"
            tags = TagParser.extract_tags(text)
            assert len(tags) == 1, f"Failed for hotkey: {hotkey}"
            assert tags[0][1] == f"bitcast-hk:{hotkey}"
    
    def test_is_valid_tag_valid_cases(self):
        """Test is_valid_tag with valid tags."""
        assert TagParser.is_valid_tag("bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq") is True
        assert TagParser.is_valid_tag("bitcast-xxyz78900") is True
        assert TagParser.is_valid_tag("BITCAST-HK:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq") is True
    
    def test_is_valid_tag_invalid_cases(self):
        """Test is_valid_tag with invalid tags."""
        assert TagParser.is_valid_tag("bitcast-hk:abc") is False
        assert TagParser.is_valid_tag("bitcast-hk{5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq}") is False
        assert TagParser.is_valid_tag("invalid") is False
        assert TagParser.is_valid_tag("") is False
        assert TagParser.is_valid_tag(None) is False
    
    def test_multiple_same_type_tags(self):
        """Test multiple tags of the same type."""
        text = "Check bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq and bitcast-hk:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 2
        assert all(tag_type == "HK" for tag_type, _ in tags)

