"""
Unit tests for referral code encoding/decoding.
"""

import pytest
from bitcast.validator.account_connection.referral_code import encode_referral_code, decode_referral_code


class TestReferralCode:
    """Test referral code encoding and decoding."""
    
    @pytest.mark.parametrize("handle", [
        "bitcast_network",
        "elonmusk",
        "v",
        "dreadbong0",
        "yumagroup",
    ])
    def test_encode_decode_roundtrip(self, handle):
        """Encoding and decoding returns the original handle."""
        encoded = encode_referral_code(handle)
        assert decode_referral_code(encoded) == handle
    
    def test_encode_strips_at_prefix(self):
        """@ prefix is stripped during encoding."""
        assert encode_referral_code("@bitcast_network") == encode_referral_code("bitcast_network")
    
    def test_decode_invalid_returns_none(self):
        assert decode_referral_code("") is None
        assert decode_referral_code("!!!invalid!!!") is None
