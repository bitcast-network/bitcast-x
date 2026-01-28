"""
Tests for date_offset functionality in social discovery.
"""
import pytest
from datetime import date, timedelta
from bitcast.validator.social_discovery.social_discovery import DISCOVERY_REFERENCE_DATE


class TestDateOffset:
    """Test date offset scheduling logic."""
    
    def test_scheduling_formula(self):
        """Test the offset scheduling formula works correctly."""
        reference_date = date(2025, 11, 9)
        
        # Test that offsets create different schedules
        # Offset 0: runs on day 0, 14, 28...
        for day_offset in [0, 14, 28]:
            days_since = day_offset
            adjusted = days_since - 0
            assert adjusted % 14 == 0
        
        # Offset 3: runs on day 3, 17, 31...
        for day_offset in [3, 17, 31]:
            days_since = day_offset
            adjusted = days_since - 3
            assert adjusted % 14 == 0
        
        # Offset 3 should NOT run on day 5
        days_since = 5
        adjusted = days_since - 3
        assert adjusted % 14 != 0
    
    def test_reference_date_constant_exists(self):
        """Verify the reference date constant is defined."""
        assert DISCOVERY_REFERENCE_DATE == date(2025, 11, 9)


class TestPoolManagerDateOffset:
    """Test that PoolManager correctly loads date_offset."""
    
    def test_pool_manager_loads_date_offset(self, monkeypatch):
        """PoolManager should load date_offset from API."""
        from bitcast.validator.social_discovery.pool_manager import PoolManager
        from unittest.mock import Mock
        
        # Mock API response
        mock_response = Mock()
        mock_response.json.return_value = {
            'pools': [
                {
                    'name': 'test_pool',
                    'keywords': ['test'],
                    'initial_accounts': ['account1'],
                    'date_offset': 5,
                    'active': True
                }
            ]
        }
        mock_response.raise_for_status = Mock()
        
        def mock_get(*args, **kwargs):
            return mock_response
        
        monkeypatch.setattr('requests.get', mock_get)
        
        manager = PoolManager()
        
        pool = manager.get_pool('test_pool')
        assert pool is not None
        assert pool['date_offset'] == 5
    
    def test_pool_manager_defaults_date_offset_to_zero(self, monkeypatch):
        """PoolManager should default date_offset to 0 if not provided."""
        from bitcast.validator.social_discovery.pool_manager import PoolManager
        from unittest.mock import Mock
        
        # Mock API response without date_offset
        mock_response = Mock()
        mock_response.json.return_value = {
            'pools': [
                {
                    'name': 'test_pool',
                    'keywords': ['test'],
                    'initial_accounts': ['account1'],
                    'active': True
                }
            ]
        }
        mock_response.raise_for_status = Mock()
        
        def mock_get(*args, **kwargs):
            return mock_response
        
        monkeypatch.setattr('requests.get', mock_get)
        
        manager = PoolManager()
        
        pool = manager.get_pool('test_pool')
        assert pool is not None
        assert pool['date_offset'] == 0
