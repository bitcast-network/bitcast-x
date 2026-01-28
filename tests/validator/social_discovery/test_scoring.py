"""
Essential tests for social discovery functionality.
"""

import pytest
import unittest.mock as mock
import tempfile
import json
import numpy as np
from pathlib import Path

from bitcast.validator.social_discovery.social_discovery import (
    TwitterNetworkAnalyzer,
    discover_social_network
)
from bitcast.validator.social_discovery.pool_manager import PoolManager


class TestPoolManager:
    """Essential pool manager tests."""
    
    @mock.patch('bitcast.validator.social_discovery.pool_manager.requests.get')
    def test_load_pools(self, mock_get):
        """Test pool loading from API."""
        # Mock API response
        mock_response = mock.Mock()
        mock_response.json.return_value = {
            "pools": [
                {
                    "name": "tao",
                    "keywords": ["tao", "bittensor"],
                    "initial_accounts": ["opentensor"],
                    "active": True
                }
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        # Create manager (will fetch from mocked API)
        manager = PoolManager(api_url="http://test.api/pools")
        
        pools = manager.get_pools()
        assert "tao" in pools
        
        tao_config = manager.get_pool("tao")
        assert tao_config['keywords'] == ["tao", "bittensor"]
        assert tao_config['initial_accounts'] == ["opentensor"]
        
        # Verify API was called
        mock_get.assert_called_once_with("http://test.api/pools", timeout=10)


class TestTwitterNetworkAnalyzer:
    """Essential network analyzer tests."""
    
    def test_analyze_network_basic(self):
        """Test basic network analysis."""
        # Mock Twitter client
        mock_client = mock.Mock()
        
        # Mock tweet data with interactions
        mock_tweets = {
            'user1': [
                {'text': 'Hello @user2', 'tagged_accounts': ['user2'], 'retweeted_user': None, 'quoted_user': None, 'author': 'user1'},
                {'text': 'RT @user3: Great', 'tagged_accounts': [], 'retweeted_user': 'user3', 'quoted_user': None, 'author': 'user1'}
            ],
            'user2': [
                {'text': 'Thanks @user1', 'tagged_accounts': ['user1'], 'retweeted_user': None, 'quoted_user': None, 'author': 'user2'}
            ],
            'user3': [
                {'text': 'Original tweet', 'tagged_accounts': [], 'retweeted_user': None, 'quoted_user': None, 'author': 'user3'}
            ]
        }
        
        def mock_fetch(username, force_refresh=False):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1):
            return True  # All users are relevant
        
        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance
        
        analyzer = TwitterNetworkAnalyzer(mock_client)
        
        scores, matrix, relationship_matrix, usernames, user_info_map, total_pool_followers = analyzer.analyze_network(['user1', 'user2'], ['test'])
        
        # Should have scored all users
        assert len(scores) == 3
        assert set(scores.keys()) == {'user1', 'user2', 'user3'}
        
        # Pool difficulty equals sum of followers for fetched accounts only
        # user1 and user2 have 1000 each, user3 was discovered (no follower info)
        assert total_pool_followers == 2000
        
        # Scores should be absolute (PageRank × pool_difficulty / 1000), summing to pool_difficulty / 1000
        scaled_pool_difficulty = total_pool_followers / 1000
        assert abs(sum(scores.values()) - scaled_pool_difficulty) < 0.1
        
        # All scores should be positive
        assert all(score > 0 for score in scores.values())
        
        # Matrix should be correct size
        assert matrix.shape == (3, 3)
        assert len(usernames) == 3
    
    def test_analyze_network_filters_replies(self):
        """Test that reply tweets are filtered out from network analysis."""
        # Mock Twitter client
        mock_client = mock.Mock()
        
        # Mock tweet data with replies (which should be filtered out)
        mock_tweets = {
            'user1': [
                {'text': 'Hello @user2', 'tagged_accounts': ['user2'], 'retweeted_user': None, 'quoted_user': None, 'in_reply_to_status_id': None, 'author': 'user1'},
                {'text': 'Reply @user3', 'tagged_accounts': ['user3'], 'retweeted_user': None, 'quoted_user': None, 'in_reply_to_status_id': '123456', 'author': 'user1'}  # Filtered (reply)
            ],
            'user2': [
                {'text': 'Original tweet', 'tagged_accounts': [], 'retweeted_user': None, 'quoted_user': None, 'in_reply_to_status_id': None, 'author': 'user2'},
                {'text': 'Another reply', 'tagged_accounts': ['user1'], 'retweeted_user': None, 'quoted_user': None, 'in_reply_to_status_id': '789012', 'author': 'user2'}  # Filtered (reply)
            ]
        }
        
        def mock_fetch(username, force_refresh=False):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1):
            return True  # All users are relevant
        
        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance
        
        analyzer = TwitterNetworkAnalyzer(mock_client)
        
        scores, matrix, relationship_matrix, usernames, user_info_map, total_pool_followers = analyzer.analyze_network(['user1', 'user2'], ['test'])
        
        # Should only have user1 and user2 (user3 was only in a reply, which got filtered)
        assert len(scores) == 2
        assert set(scores.keys()) == {'user1', 'user2'}
        
        # Pool difficulty should equal sum of followers (1000 per user × 2 users)
        assert total_pool_followers == 2000
        
        # Scores should be absolute (PageRank × pool_difficulty / 1000), summing to pool_difficulty / 1000
        scaled_pool_difficulty = total_pool_followers / 1000
        assert abs(sum(scores.values()) - scaled_pool_difficulty) < 0.1
        
        # All scores should be positive
        assert all(score > 0 for score in scores.values())
    
    
    def test_no_interactions_error(self):
        """Test error when no interactions found."""
        mock_client = mock.Mock()
        mock_client.fetch_user_tweets.return_value = {'tweets': [], 'user_info': {'followers_count': 0}}
        mock_client.check_user_relevance.return_value = True
        
        analyzer = TwitterNetworkAnalyzer(mock_client)
        
        with pytest.raises(ValueError, match="No interactions found"):
            analyzer.analyze_network(['lonely_user'], ['test'])
    
    def test_analyze_network_min_interaction_weight_filter(self):
        """Test that min_interaction_weight filters accounts with low incoming weight."""
        mock_client = mock.Mock()
        
        # user1 mentions user2, user3 and retweets user4
        # user2 mentions user3, user4
        # Incoming weight accumulates from all edges pointing to each user
        mock_tweets = {
            'user1': [
                {'text': 'Hello @user2 @user3', 'tagged_accounts': ['user2', 'user3'], 'retweeted_user': None, 'quoted_user': None, 'author': 'user1'},
                {'text': 'RT @user4: Great', 'tagged_accounts': [], 'retweeted_user': 'user4', 'quoted_user': None, 'author': 'user1'}
            ],
            'user2': [
                {'text': 'Hello @user3 @user4', 'tagged_accounts': ['user3', 'user4'], 'retweeted_user': None, 'quoted_user': None, 'author': 'user2'}
            ]
        }
        
        def mock_fetch(username, force_refresh=False):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1):
            return True
        
        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance
        
        analyzer = TwitterNetworkAnalyzer(mock_client, max_workers=1)
        
        # Without filter: should have all 4 users
        scores_no_filter, _, _, _, _, _ = analyzer.analyze_network(
            ['user1', 'user2'], ['test'], min_interaction_weight=0
        )
        assert len(scores_no_filter) == 4
        
        # With moderate filter: some non-seeds may be filtered, but seeds preserved
        scores_filtered, _, _, _, _, _ = analyzer.analyze_network(
            ['user1', 'user2'], ['test'], min_interaction_weight=3.0
        )
        
        # Seeds are always preserved
        assert 'user1' in scores_filtered
        assert 'user2' in scores_filtered
        # user3 and user4 have sufficient incoming weight
        assert 'user3' in scores_filtered
        assert 'user4' in scores_filtered
        # Total should be 4 (seeds + qualified non-seeds)
        assert len(scores_filtered) == 4
        
        # With high filter: only seeds remain (non-seeds filtered out)
        scores_high_filter, _, _, _, _, _ = analyzer.analyze_network(
            ['user1', 'user2'], ['test'], min_interaction_weight=4.5
        )
        assert 'user1' in scores_high_filter  # seed
        assert 'user2' in scores_high_filter  # seed
        # user3 and user4 don't meet threshold, so filtered
        assert 'user3' not in scores_high_filter
        assert 'user4' not in scores_high_filter


class TestSocialDiscoveryIntegration:
    """Integration tests for social discovery."""
    
    def setup_method(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
    
    def teardown_method(self):
        """Cleanup."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @pytest.mark.asyncio
    @mock.patch('bitcast.validator.social_discovery.social_discovery.PoolManager')
    @mock.patch('bitcast.validator.social_discovery.social_discovery.TwitterNetworkAnalyzer')
    async def test_discover_social_network_success(self, mock_analyzer_class, mock_pool_manager_class):
        """Test successful social discovery."""
        # Mock pool manager
        mock_pool_manager = mock.Mock()
        mock_pool_manager.get_pool.return_value = {
            'keywords': ['test'],
            'initial_accounts': ['user1'],
            'max_members': 64,
            'min_interaction_weight': 0
        }
        mock_pool_manager_class.return_value = mock_pool_manager
        
        # Mock analyzer
        mock_analyzer = mock.Mock()
        mock_analyzer.analyze_network.return_value = (
            {'user1': 900.0, 'user2': 600.0},  # absolute scores (PageRank × pool_difficulty)
            np.array([[0, 1], [1, 0]]),    # adjacency matrix (max weights)
            np.array([[0, 1.5], [2.0, 0]]),  # relationship scores matrix
            ['user1', 'user2'],            # usernames
            {'user1': {'username': 'user1', 'followers_count': 1000}, 'user2': {'username': 'user2', 'followers_count': 500}},  # user_info_map
            1500  # total_pool_followers (pool difficulty)
        )
        mock_analyzer_class.return_value = mock_analyzer
        
        # Test with mocked file operations
        with mock.patch('builtins.open', mock.mock_open()), \
             mock.patch('pathlib.Path.mkdir'), \
             mock.patch('pathlib.Path.exists', return_value=False), \
             mock.patch('json.dump'):
            
            result = await discover_social_network("test_pool")
            
            # Should have called the analyzer
            mock_analyzer.analyze_network.assert_called_once()
            assert "test_pool" in result
    
    @pytest.mark.asyncio
    async def test_regenerate_with_nonexistent_pool(self):
        """Test error handling for nonexistent pool."""
        with pytest.raises(Exception, match="not found"):
            await discover_social_network("nonexistent_pool")
