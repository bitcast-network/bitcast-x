"""Tests for social map loader."""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bitcast.validator.tweet_scoring.social_map_loader import (
    load_latest_social_map,
    get_active_members,
    get_considered_accounts,
    get_active_members_for_brief
)


@pytest.fixture
def sample_social_map():
    """Sample social map data."""
    return {
        'metadata': {
            'created_at': '2025-11-27T12:00:00',
            'pool_name': 'test',
            'total_accounts': 5
        },
        'accounts': {
            'user1': {'score': 0.30},
            'user2': {'score': 0.25},
            'user3': {'score': 0.20},
            'user4': {'score': 0.15},
            'user5': {'score': 0.10}
        }
    }


@pytest.fixture
def sample_social_map_with_status():
    """Sample social map with legacy status field (ignored)."""
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
    
    def test_extracts_all_accounts(self, sample_social_map):
        """Should extract all accounts sorted by score."""
        active = get_active_members(sample_social_map)
        assert set(active) == {'user1', 'user2', 'user3', 'user4', 'user5'}
    
    def test_ignores_status_field(self, sample_social_map_with_status):
        """Should extract all accounts even if status field present (ignored)."""
        active = get_active_members(sample_social_map_with_status)
        # All 5 accounts returned, status field is ignored
        assert set(active) == {'user1', 'user2', 'user3', 'user4', 'user5'}
    
    def test_returns_score_sorted_list(self, sample_social_map):
        """Should return list sorted by score (highest first)."""
        active = get_active_members(sample_social_map)
        # user1 has highest score (0.30), user5 has lowest (0.10)
        assert active[0] == 'user1'
        assert active[-1] == 'user5'
    
    def test_respects_limit(self, sample_social_map):
        """Should respect limit parameter."""
        active = get_active_members(sample_social_map, limit=2)
        assert len(active) == 2
        assert 'user1' in active  # Highest score
        assert 'user2' in active  # Second highest
    
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
    
    def test_includes_all_accounts(self, sample_social_map):
        """Should include all accounts when limit is high enough."""
        considered = get_considered_accounts(sample_social_map, limit=5)
        usernames = {username for username, _ in considered}
        assert len(usernames) == 5
        assert 'user4' in usernames
        assert 'user5' in usernames


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


class TestGetActiveMembersForBrief:
    """Test get_active_members_for_brief function."""
    
    def test_recent_date_range_returns_members(self):
        """Recent date range should return active members."""
        try:
            # Use a recent date range (last 7 days)
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=7)
            
            members = get_active_members_for_brief('tao', start_date, end_date)
            
            # Get active members from latest map for comparison
            social_map, _ = load_latest_social_map('tao')
            active = get_active_members(social_map)
            
            # Should have at least the active members
            assert len(members) >= len(active) * 0.8  # Allow for some variation
            assert isinstance(members, list)
            
        except FileNotFoundError:
            pytest.skip("No social map found for 'tao' pool")
    
    def test_with_date_range_includes_relegated_if_map_updated(self):
        """If map updated during brief, should include active + relegated."""
        try:
            # Use date range that spans recent map updates (last 30 days)
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=30)
            
            members = get_active_members_for_brief(
                'tao',
                start_date=start_date,
                end_date=end_date
            )
            
            # Should return a list
            assert isinstance(members, list)
            
            # If any maps were created during period, should have more than just active
            social_map, _ = load_latest_social_map('tao')
            active_only = get_active_members(social_map)
            
            # Should have >= active members
            assert len(members) >= len(active_only)
            
        except FileNotFoundError:
            pytest.skip("No social map found for 'tao' pool")
    
    def test_short_date_range(self):
        """Short date range should return active members from relevant maps."""
        try:
            # Use a short date range (3 days)
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=3)
            
            members = get_active_members_for_brief('tao', start_date, end_date)
            
            # Should return a list of usernames
            assert isinstance(members, list)
            assert len(members) > 0
            
            # Should contain only strings
            assert all(isinstance(username, str) for username in members)
            
        except FileNotFoundError:
            pytest.skip("No social map found for 'tao' pool")
    
    def test_nonexistent_pool_raises_error(self):
        """Should raise FileNotFoundError for nonexistent pool."""
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=7)
        
        with pytest.raises(FileNotFoundError, match="No social maps found"):
            get_active_members_for_brief('nonexistent_pool_xyz', start_date, end_date)
    
    def test_returns_list_type(self):
        """Should always return a list."""
        try:
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=7)
            result = get_active_members_for_brief('tao', start_date, end_date)
            assert isinstance(result, list)
        except FileNotFoundError:
            pytest.skip("No social map found for 'tao' pool")
    
    def test_returns_non_empty_list(self):
        """Should return non-empty list for valid pool."""
        try:
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=7)
            result = get_active_members_for_brief('tao', start_date, end_date)
            assert len(result) > 0
            assert all(isinstance(username, str) for username in result)
        except FileNotFoundError:
            pytest.skip("No social map found for 'tao' pool")

