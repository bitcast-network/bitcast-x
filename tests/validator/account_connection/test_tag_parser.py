"""
Unit tests for TagParser.
"""

import pytest
from bitcast.validator.account_connection.tag_parser import TagParser
from bitcast.validator.account_connection.referral_code import encode_referral_code


class TestTagParser:
    """Test tag extraction and validation."""
    
    def test_extract_hk_tag(self):
        """Test extraction of bitcast-hk tags with substrate hotkey."""
        text = "Check out bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq for more info"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 1
        assert tags[0].tag_type == "HK"
        assert tags[0].full_tag == "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        assert tags[0].referred_by is None
        assert tags[0].referral_code is None
    
    def test_extract_x_tag(self):
        """Test extraction of bitcast-x tags."""
        text = "Visit bitcast-xxyz78900"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 1
        assert tags[0].tag_type == "X"
        assert tags[0].full_tag == "bitcast-xxyz78900"
        assert tags[0].referred_by is None
    
    def test_extract_hk_tag_with_referral(self):
        """Test extraction of bitcast-hk tag with a referral code suffix."""
        referral_code = encode_referral_code("dreadbong0")
        text = f"bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq-{referral_code}"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 1
        assert tags[0].tag_type == "HK"
        assert tags[0].referred_by == "dreadbong0"
        assert tags[0].referral_code == referral_code
    
    def test_extract_x_tag_with_referral(self):
        """Test extraction of bitcast-x tag with a referral code suffix."""
        referral_code = encode_referral_code("yumagroup")
        text = f"bitcast-xabc123-{referral_code}"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 1
        assert tags[0].tag_type == "X"
        assert tags[0].full_tag == f"bitcast-xabc123-{referral_code}"
        assert tags[0].referred_by == "yumagroup"
        assert tags[0].referral_code == referral_code
    
    def test_extract_multiple_tags(self):
        """Test extraction of multiple tags from one tweet."""
        text = "Check bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq and bitcast-xxyz78900 now!"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 2
        assert tags[0].tag_type == "HK"
        assert tags[1].tag_type == "X"
    
    def test_case_insensitive_matching(self):
        """Test that tags are matched case-insensitively."""
        text1 = "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        text2 = "BITCAST-HK:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        
        assert len(TagParser.extract_tags(text1)) == 1
        assert len(TagParser.extract_tags(text2)) == 1
    
    def test_no_tags_found(self):
        assert len(TagParser.extract_tags("This is just a regular tweet")) == 0
    
    def test_empty_and_none_input(self):
        assert len(TagParser.extract_tags("")) == 0
        assert len(TagParser.extract_tags(None)) == 0
    
    def test_malformed_tags(self):
        """Test various malformed tags are not matched."""
        assert len(TagParser.extract_tags("bitcast-hk:5DNm")) == 0  # Too short
        assert len(TagParser.extract_tags("bitcast-hk{5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq}")) == 0  # Wrong separator
        assert len(TagParser.extract_tags("bitcast-hk:0OIlabc12345678901234567890123456789012345678")) == 0  # Invalid base58
    
    def test_valid_substrate_addresses(self):
        """Test various valid substrate addresses."""
        valid_hotkeys = [
            "5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        ]
        
        for hotkey in valid_hotkeys:
            text = f"bitcast-hk:{hotkey}"
            tags = TagParser.extract_tags(text)
            assert len(tags) == 1, f"Failed for hotkey: {hotkey}"
            assert tags[0].full_tag == f"bitcast-hk:{hotkey}"
    
    def test_is_valid_tag(self):
        """Test is_valid_tag with valid and invalid tags."""
        assert TagParser.is_valid_tag("bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq") is True
        assert TagParser.is_valid_tag("bitcast-xxyz78900") is True
        assert TagParser.is_valid_tag("bitcast-hk:abc") is False
        assert TagParser.is_valid_tag("") is False
        assert TagParser.is_valid_tag(None) is False
    
    def test_multiple_same_type_tags(self):
        """Test multiple tags of the same type."""
        text = "Check bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq and bitcast-hk:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        tags = TagParser.extract_tags(text)
        assert len(tags) == 2
        assert all(t.tag_type == "HK" for t in tags)
