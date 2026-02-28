"""
Essential tests for social discovery functionality.
"""

import pytest
import unittest.mock as mock
import tempfile
import json
import numpy as np
from pathlib import Path

from bitcast.validator.social_discovery.social_discovery import TwitterNetworkAnalyzer
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
        
        def mock_fetch(username, fetch_days=30, skip_if_cache_fresh=False):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1, skip_if_cache_fresh=False):
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
        
        def mock_fetch(username, fetch_days=30, skip_if_cache_fresh=False):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}
        
        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1, skip_if_cache_fresh=False):
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

        # Create 20+ seed accounts to avoid auto-relaxation of min_interaction_weight
        # when seed count < 20, parameters are relaxed to bootstrap discovery
        seed_accounts = [f'seed{i}' for i in range(20)]
        non_seed_accounts = ['user_a', 'user_b', 'user_c']
        all_accounts = seed_accounts + non_seed_accounts

        # Build mock tweets:
        # - Each seed mentions user_a (gives user_a 20 mentions)
        # - Note: PAGERANK_MENTION_WEIGHT = 2.0, so user_a gets 40.0 incoming weight
        # - Only 2 seeds mention user_b (gives user_b 2 mentions = 4.0 weight)
        # - Only 1 seed mentions user_c (gives user_c 1 mention = 2.0 weight)
        # - Add seed-to-seed interactions to ensure network connectivity when non-seeds are filtered
        mock_tweets = {}
        for i, seed in enumerate(seed_accounts):
            mock_tweets[seed] = [
                {'text': f'Hello @user_a', 'tagged_accounts': ['user_a'], 'retweeted_user': None, 'quoted_user': None, 'author': seed},
                # Add interaction to another seed to ensure network remains connected
                {'text': f'Hello @{seed_accounts[(i+1) % 20]}', 'tagged_accounts': [seed_accounts[(i+1) % 20]], 'retweeted_user': None, 'quoted_user': None, 'author': seed}
            ]
        # Add extra mentions for user_b and user_c from a couple seeds
        mock_tweets['seed0'].append({'text': 'Hello @user_b', 'tagged_accounts': ['user_b'], 'retweeted_user': None, 'quoted_user': None, 'author': 'seed0'})
        mock_tweets['seed1'].append({'text': 'Hello @user_b @user_c', 'tagged_accounts': ['user_b', 'user_c'], 'retweeted_user': None, 'quoted_user': None, 'author': 'seed1'})

        def mock_fetch(username, fetch_days=30, skip_if_cache_fresh=False):
            return {'tweets': mock_tweets.get(username, []), 'user_info': {'followers_count': 1000}}

        def mock_relevance(username, keywords, min_followers, lang=None, min_tweets=1, skip_if_cache_fresh=False):
            # All accounts (seeds and non-seeds) are relevant
            return username in all_accounts

        mock_client.fetch_user_tweets.side_effect = mock_fetch
        mock_client.check_user_relevance.side_effect = mock_relevance

        analyzer = TwitterNetworkAnalyzer(mock_client, max_workers=1)

        # Without filter: should have all relevant accounts (20 seeds + 3 non-seeds)
        scores_no_filter, _, _, _, _, _ = analyzer.analyze_network(
            seed_accounts, ['test'], min_interaction_weight=0
        )
        assert len(scores_no_filter) == 23

        # With filter weight=5: user_a has 40.0, user_b has 4.0, user_c has 2.0
        # user_a (40.0 >= 5) stays, user_b and user_c are filtered out
        scores_filtered, _, _, _, _, _ = analyzer.analyze_network(
            seed_accounts, ['test'], min_interaction_weight=5.0
        )

        # All seeds are preserved regardless of their outgoing interactions
        for seed in seed_accounts:
            assert seed in scores_filtered
        # user_a has weight 40.0, should be included
        assert 'user_a' in scores_filtered
        # user_b (weight 4.0) and user_c (weight 2.0) are filtered out
        assert 'user_b' not in scores_filtered
        assert 'user_c' not in scores_filtered

        # With high filter (weight=41): user_a is filtered (40.0 < 41), only seeds remain
        # Seeds have interactions with each other, so network stays valid
        scores_high_filter, _, _, _, _, _ = analyzer.analyze_network(
            seed_accounts, ['test'], min_interaction_weight=41.0
        )
        # Seeds preserved
        for seed in seed_accounts:
            assert seed in scores_high_filter
        # All non-seeds filtered (user_a has 40.0 < 41.0)
        assert 'user_a' not in scores_high_filter
        assert 'user_b' not in scores_high_filter
        assert 'user_c' not in scores_high_filter
