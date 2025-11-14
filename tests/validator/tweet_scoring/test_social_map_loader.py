"""Tests for social map loader."""

import json
import pytest
from pathlib import Path
from bitcast.validator.tweet_scoring.social_map_loader import (
    load_latest_social_map,
    get_active_members,
    get_considered_accounts
)


@pytest.fixture
def sample_social_map():
    """Sample social map data for testing."""
    return {
        'metadata': {
            'created_at': '2025-10-30T12:00:00',
            'pool_name': 'test',
            'total_accounts': 5
        },
        'accounts': {
            'user1': {'score': 0.30, 'status': 'in'},
            'user2': {'score': 0.25, 'status': 'promoted'},
            'user3': {'score': 0.20, 'status': 'in'},
            'user4': {'score': 0.15, 'status': 'out'},
            'user5': {'score': 0.10, 'status': 'relegated'}
        }
    }


class TestGetActiveMembers:
    """Test get_active_members function."""
    
    def test_extracts_in_and_promoted(self, sample_social_map):
        """Should extract only 'in' and 'promoted' members."""
        active = get_active_members(sample_social_map)
        assert set(active) == {'user1', 'user2', 'user3'}
    
    def test_returns_sorted_list(self, sample_social_map):
        """Should return alphabetically sorted list."""
        active = get_active_members(sample_social_map)
        assert active == sorted(active)
    
    def test_empty_accounts(self):
        """Should handle empty accounts dict."""
        social_map = {'accounts': {}}
        active = get_active_members(social_map)
        assert active == []
    
    def test_missing_accounts_field(self):
        """Should handle missing accounts field."""
        social_map = {}
        active = get_active_members(social_map)
        assert active == []


class TestGetConsideredAccounts:
    """Test get_considered_accounts function."""
    
    def test_returns_top_n_by_score(self, sample_social_map):
        """Should return top N accounts sorted by score."""
        considered = get_considered_accounts(sample_social_map, limit=3)
        assert len(considered) == 3
        assert considered[0] == ('user1', 0.30)
        assert considered[1] == ('user2', 0.25)
        assert considered[2] == ('user3', 0.20)
    
    def test_sorted_descending_by_score(self, sample_social_map):
        """Should be sorted by score in descending order."""
        considered = get_considered_accounts(sample_social_map, limit=5)
        scores = [score for _, score in considered]
        assert scores == sorted(scores, reverse=True)
    
    def test_limit_exceeds_available(self, sample_social_map):
        """Should return all accounts if limit exceeds available."""
        considered = get_considered_accounts(sample_social_map, limit=100)
        assert len(considered) == 5
    
    def test_zero_limit(self, sample_social_map):
        """Should return empty list for zero limit."""
        considered = get_considered_accounts(sample_social_map, limit=0)
        assert considered == []
    
    def test_includes_all_statuses(self, sample_social_map):
        """Should include accounts regardless of status."""
        considered = get_considered_accounts(sample_social_map, limit=5)
        usernames = {username for username, _ in considered}
        assert 'user4' in usernames  # 'out' status
        assert 'user5' in usernames  # 'relegated' status


class TestLoadLatestSocialMap:
    """Test load_latest_social_map function."""
    
    def test_loads_real_social_map_if_exists(self):
        """Integration test: load real social map if it exists."""
        # This will only pass if a social map exists for 'tao' pool
        try:
            social_map, file_path = load_latest_social_map('tao')
            
            assert isinstance(social_map, dict)
            assert 'accounts' in social_map
            assert isinstance(file_path, str)
            assert Path(file_path).exists()
            
        except FileNotFoundError:
            # No social map exists - skip test
            pytest.skip("No social map found for 'tao' pool")
    
    def test_raises_on_nonexistent_pool(self):
        """Should raise FileNotFoundError for nonexistent pool."""
        with pytest.raises(FileNotFoundError, match="No social map directory found"):
            load_latest_social_map('nonexistent_pool_xyz')
    
    def test_returns_file_path(self):
        """Should return valid file path along with social map."""
        try:
            social_map, file_path = load_latest_social_map('tao')
            
            # Verify file path is returned and exists
            assert isinstance(file_path, str)
            assert len(file_path) > 0
            assert Path(file_path).exists()
            assert file_path.endswith('.json')
            
        except FileNotFoundError:
            # No social map exists - skip test
            pytest.skip("No social map found for 'tao' pool")

