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
    
    def setup_method(self):
        """Set up test config."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / "pools_config.json"
        
        config = {
            "pools": [
                {
                    "name": "tao",
                    "keywords": ["tao", "bittensor"],
                    "initial_accounts": ["opentensor"],
                    "active": True
                }
            ]
        }
        
        with open(self.config_file, 'w') as f:
            json.dump(config, f)
    
    def teardown_method(self):
        """Cleanup."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_load_pools(self):
        """Test pool loading."""
        manager = PoolManager(str(self.config_file))
        
        pools = manager.get_pools()
        assert "tao" in pools
        
        tao_config = manager.get_pool("tao")
        assert tao_config['keywords'] == ["tao", "bittensor"]
        assert tao_config['initial_accounts'] == ["opentensor"]


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
        
        def mock_fetch(username):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1):
            return True  # All users are relevant
        
        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance
        
        analyzer = TwitterNetworkAnalyzer(mock_client)
        
        scores, matrix, usernames = analyzer.analyze_network(['user1', 'user2'], ['test'])
        
        # Should have scored all users
        assert len(scores) == 3
        assert set(scores.keys()) == {'user1', 'user2', 'user3'}
        
        # Scores should sum to 1.0
        assert abs(sum(scores.values()) - 1.0) < 1e-10
        
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
        
        def mock_fetch(username):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1):
            return True  # All users are relevant
        
        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance
        
        analyzer = TwitterNetworkAnalyzer(mock_client)
        
        scores, matrix, usernames = analyzer.analyze_network(['user1', 'user2'], ['test'])
        
        # Should only have user1 and user2 (user3 was only in a reply, which got filtered)
        assert len(scores) == 2
        assert set(scores.keys()) == {'user1', 'user2'}
        
        # Scores should sum to 1.0
        assert abs(sum(scores.values()) - 1.0) < 1e-10
        
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
        
        def mock_fetch(username):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1):
            return True
        
        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance
        
        analyzer = TwitterNetworkAnalyzer(mock_client, max_workers=1)
        
        # Without filter: should have all 4 users
        scores_no_filter, _, _ = analyzer.analyze_network(
            ['user1', 'user2'], ['test'], min_interaction_weight=0
        )
        assert len(scores_no_filter) == 4
        
        # With moderate filter: some non-seeds may be filtered, but seeds preserved
        scores_filtered, _, _ = analyzer.analyze_network(
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
        scores_high_filter, _, _ = analyzer.analyze_network(
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
        
        # Create test pool config
        self.pools_config = Path(self.temp_dir) / "pools_config.json"
        config = {
            "pools": [
                {"name": "test_pool", "keywords": ["test"], "initial_accounts": ["user1"], "active": True}
            ]
        }
        with open(self.pools_config, 'w') as f:
            json.dump(config, f)
    
    def teardown_method(self):
        """Cleanup."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @mock.patch('bitcast.validator.social_discovery.social_discovery.PoolManager')
    @mock.patch('bitcast.validator.social_discovery.social_discovery.TwitterNetworkAnalyzer')
    def test_discover_social_network_success(self, mock_analyzer_class, mock_pool_manager_class):
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
            {'user1': 0.6, 'user2': 0.4},  # scores
            np.array([[0, 1], [1, 0]]),    # adjacency matrix  
            ['user1', 'user2']             # usernames
        )
        mock_analyzer_class.return_value = mock_analyzer
        
        # Test with mocked file operations
        with mock.patch('builtins.open', mock.mock_open()), \
             mock.patch('pathlib.Path.mkdir'), \
             mock.patch('pathlib.Path.exists', return_value=False), \
             mock.patch('json.dump'):
            
            result = discover_social_network("test_pool")
            
            # Should have called the analyzer
            mock_analyzer.analyze_network.assert_called_once()
            assert "test_pool" in result
    
    def test_regenerate_with_nonexistent_pool(self):
        """Test error handling for nonexistent pool."""
        with pytest.raises(Exception, match="not found"):
            discover_social_network("nonexistent_pool")
