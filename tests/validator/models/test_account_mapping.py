"""Tests for AccountMapping dataclass."""

import pytest
from bitcast.validator.account_connection.models import AccountMapping


class TestAccountMappingCreation:
    """Tests for creating AccountMapping instances."""
    
    def test_account_mapping_creation_with_all_fields(self):
        """Can create valid AccountMapping with all fields."""
        mapping = AccountMapping(
            account_username="test_user",
            uid=42,
            pool="tao",
            connection_tag="bitcast-hk:5ABC123...",
            hotkey="5ABC123..."
        )
        
        assert mapping.account_username == "test_user"
        assert mapping.uid == 42
        assert mapping.pool == "tao"
        assert mapping.connection_tag == "bitcast-hk:5ABC123..."
        assert mapping.hotkey == "5ABC123..."
    
    def test_account_mapping_creation_required_only(self):
        """Can create AccountMapping with only required fields."""
        mapping = AccountMapping(
            account_username="minimal_user",
            uid=100,
            pool="ai_crypto"
        )
        
        assert mapping.account_username == "minimal_user"
        assert mapping.uid == 100
        assert mapping.pool == "ai_crypto"
        assert mapping.connection_tag is None
        assert mapping.hotkey is None


class TestAccountMappingValidation:
    """Tests for AccountMapping validation."""
    
    def test_empty_username_raises_error(self):
        """AccountMapping rejects empty username."""
        with pytest.raises(ValueError, match="Account username cannot be empty"):
            AccountMapping(
                account_username="",
                uid=42,
                pool="tao"
            )
    
    def test_negative_uid_raises_error(self):
        """AccountMapping rejects negative UID."""
        with pytest.raises(ValueError, match="UID must be non-negative"):
            AccountMapping(
                account_username="test",
                uid=-1,
                pool="tao"
            )
    
    def test_empty_pool_raises_error(self):
        """AccountMapping rejects empty pool name."""
        with pytest.raises(ValueError, match="Pool name cannot be empty"):
            AccountMapping(
                account_username="test",
                uid=42,
                pool=""
            )


class TestAccountMappingFromDict:
    """Tests for creating AccountMapping from dictionary."""
    
    def test_from_dict_with_all_fields(self):
        """Can create AccountMapping from complete dictionary."""
        data = {
            'account_username': 'dict_user',
            'uid': 68,
            'pool': 'tao',
            'connection_tag': 'bitcast-hk:5XYZ...',
            'hotkey': '5XYZ...'
        }
        
        mapping = AccountMapping.from_dict(data)
        
        assert mapping.account_username == 'dict_user'
        assert mapping.uid == 68
        assert mapping.pool == 'tao'
        assert mapping.connection_tag == 'bitcast-hk:5XYZ...'
        assert mapping.hotkey == '5XYZ...'
    
    def test_from_dict_with_defaults(self):
        """from_dict uses defaults for missing optional fields."""
        data = {
            'account_username': 'minimal',
            'uid': 50
        }
        
        mapping = AccountMapping.from_dict(data)
        
        assert mapping.account_username == 'minimal'
        assert mapping.uid == 50
        assert mapping.pool == 'tao'  # Default
        assert mapping.connection_tag is None
        assert mapping.hotkey is None


class TestAccountMappingToDict:
    """Tests for converting AccountMapping to dictionary."""
    
    def test_to_dict_creates_valid_dict(self):
        """to_dict creates dictionary with all fields."""
        mapping = AccountMapping(
            account_username="convert_user",
            uid=99,
            pool="ai_crypto",
            connection_tag="bitcast-xabc12345",
            hotkey=None
        )
        
        data = mapping.to_dict()
        
        assert data['account_username'] == "convert_user"
        assert data['uid'] == 99
        assert data['pool'] == "ai_crypto"
        assert data['connection_tag'] == "bitcast-xabc12345"
        assert data['hotkey'] is None
    
    def test_round_trip_dict_conversion(self):
        """Can convert AccountMapping to dict and back."""
        original = AccountMapping(
            account_username="roundtrip",
            uid=77,
            pool="tao",
            connection_tag="test_tag"
        )
        
        data = original.to_dict()
        recreated = AccountMapping.from_dict(data)
        
        assert original.account_username == recreated.account_username
        assert original.uid == recreated.uid
        assert original.pool == recreated.pool
        assert original.connection_tag == recreated.connection_tag
        assert original.hotkey == recreated.hotkey

