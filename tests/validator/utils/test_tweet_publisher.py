"""
Unit tests for tweet publisher module.

Tests the tweet publishing functionality including payload creation,
publishing success/failure scenarios, and data serialization.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime
import asyncio

from bitcast.validator.reward_engine.utils import (
    publish_brief_tweets,
    create_tweet_payload
)


class TestPublishBriefTweets:
    """Test the publish_brief_tweets async function."""
    
    @pytest.fixture
    def sample_brief_data(self):
        """Sample brief tweets data for testing."""
        return {
            "brief_id": "test_brief_001",
            "tweets": [
                {
                    "tweet_id": "123456789",
                    "author": "test_user",
                    "text": "Sample tweet text",
                    "created_at": "Wed Nov 03 12:00:00 +0000 2025",
                    "lang": "en",
                    "score": 0.5,
                    "favorite_count": 10,
                    "retweet_count": 5,
                    "reply_count": 2,
                    "quote_count": 1,
                    "bookmark_count": 3,
                    "retweets": [],
                    "quotes": [],
                    "meets_brief": True,
                    "usd_target": 50.0,
                    "alpha_target": 0.05
                }
            ],
            "summary": {
                "total_tweets": 1,
                "total_usd_target": 50.0,
                "unique_creators": 1
            }
        }
    
    @pytest.mark.asyncio
    async def test_publish_success(self, sample_brief_data):
        """Test successful publishing."""
        with patch('bitcast.validator.reward_engine.utils.brief_tweet_publisher.get_global_publisher') as mock_get_publisher:
            # Mock publisher
            mock_publisher = AsyncMock()
            mock_publisher.publish_unified_payload.return_value = True
            mock_get_publisher.return_value = mock_publisher
            
            # Test publishing
            result = await publish_brief_tweets(
                brief_tweets_data=sample_brief_data,
                run_id="test_run_123",
                endpoint="https://test.example.com/api"
            )
            
            # Verify success
            assert result is True
            mock_publisher.publish_unified_payload.assert_called_once()
            call_args = mock_publisher.publish_unified_payload.call_args
            assert call_args[1]["payload_type"] == "brief_tweets"
            assert call_args[1]["run_id"] == "test_run_123"
            assert call_args[1]["payload_data"] == sample_brief_data
    
    @pytest.mark.asyncio 
    async def test_publish_failure(self, sample_brief_data):
        """Test publishing failure handling."""
        with patch('bitcast.validator.reward_engine.utils.brief_tweet_publisher.get_global_publisher') as mock_get_publisher:
            # Mock publisher failure
            mock_publisher = AsyncMock()
            mock_publisher.publish_unified_payload.return_value = False
            mock_get_publisher.return_value = mock_publisher
            
            # Test publishing
            result = await publish_brief_tweets(
                brief_tweets_data=sample_brief_data,
                run_id="test_run_123", 
                endpoint="https://test.example.com/api"
            )
            
            # Verify failure handled gracefully
            assert result is False
            mock_publisher.publish_unified_payload.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_publish_exception_handling(self, sample_brief_data):
        """Test exception handling during publishing."""
        with patch('bitcast.validator.reward_engine.utils.brief_tweet_publisher.get_global_publisher') as mock_get_publisher:
            # Mock publisher exception
            mock_publisher = AsyncMock()
            mock_publisher.publish_unified_payload.side_effect = Exception("Test error")
            mock_get_publisher.return_value = mock_publisher
            
            # Test publishing - should not raise exception
            result = await publish_brief_tweets(
                brief_tweets_data=sample_brief_data,
                run_id="test_run_123",
                endpoint="https://test.example.com/api"
            )
            
            # Verify exception handled (fire-and-forget)
            assert result is False
    
    @pytest.mark.asyncio
    async def test_invalid_data_validation(self):
        """Test validation of invalid brief data."""
        # Test with None data
        result = await publish_brief_tweets(
            brief_tweets_data=None,
            run_id="test_run",
            endpoint="https://test.example.com/api"
        )
        assert result is False
        
        # Test with missing brief_id
        invalid_data = {"tweets": [], "summary": {}}
        result = await publish_brief_tweets(
            brief_tweets_data=invalid_data,
            run_id="test_run",
            endpoint="https://test.example.com/api"
        )
        assert result is False
    
    @pytest.mark.asyncio
    async def test_timestamp_auto_generation(self, sample_brief_data):
        """Test that timestamp is added if missing."""
        # Remove timestamp from sample data
        if "timestamp" in sample_brief_data:
            del sample_brief_data["timestamp"]
        
        with patch('bitcast.validator.reward_engine.utils.brief_tweet_publisher.get_global_publisher') as mock_get_publisher:
            mock_publisher = AsyncMock()
            mock_publisher.publish_unified_payload.return_value = True
            mock_get_publisher.return_value = mock_publisher
            
            await publish_brief_tweets(
                brief_tweets_data=sample_brief_data,
                run_id="test_run",
                endpoint="https://test.example.com/api"
            )
            
            # Verify timestamp was added
            assert "timestamp" in sample_brief_data
            # Verify it's a valid ISO format timestamp
            datetime.fromisoformat(sample_brief_data["timestamp"])


class TestCreateTweetPayload:
    """Test the create_tweet_payload function."""
    
    def test_basic_payload_creation(self):
        """Test basic payload structure creation."""
        filtered_tweets = [
            {
                "tweet_id": "123",
                "author": "user1", 
                "text": "Test tweet",
                "score": 0.5,
                "reasoning": "Meets criteria",
                "favorite_count": 10,
                "retweet_count": 5
            }
        ]
        
        brief_metadata = {
            "tag": "test_tag",
            "budget": 1000.0,
            "daily_budget": 142.86
        }
        
        uid_targets = {
            42: 50.0
        }
        
        payload = create_tweet_payload(
            brief_id="test_brief",
            pool_name="tao", 
            tweets_with_targets=filtered_tweets,
            brief_metadata=brief_metadata,
            uid_targets=uid_targets
        )
        
        # Verify structure
        assert payload["brief_id"] == "test_brief"
        assert len(payload["tweets"]) == 1
        assert payload["summary"]["total_tweets"] == 1
        assert payload["summary"]["unique_creators"] == 1
        assert "timestamp" in payload
        
        # Verify tweet structure
        tweet = payload["tweets"][0]
        assert tweet["tweet_id"] == "123"
        assert tweet["author"] == "user1"  # Author included
        assert tweet["text"] == "Test tweet"  # Text now included
        assert "created_at" in tweet  # Creation timestamp included
        assert "lang" in tweet  # Language included
        assert tweet["score"] == 0.5
        assert tweet["meets_brief"] is True
        assert tweet["favorite_count"] == 10
        assert tweet["retweet_count"] == 5
        assert "usd_target" in tweet
        assert "alpha_target" in tweet
        # Fields that were removed
        assert "url" not in tweet
        assert "reasoning" not in tweet
    
    def test_empty_tweets_handling(self):
        """Test handling of empty filtered tweets."""
        payload = create_tweet_payload(
            brief_id="empty_brief",
            pool_name="tao",
            tweets_with_targets=[],
            brief_metadata={"budget": 1000.0},
            uid_targets={}
        )
        
        assert payload["brief_id"] == "empty_brief"
        assert len(payload["tweets"]) == 0
        assert payload["summary"]["total_tweets"] == 0
        assert payload["summary"]["unique_creators"] == 0
        assert payload["summary"]["total_usd_target"] == 0.0
    
    def test_exception_handling_in_payload_creation(self):
        """Test graceful handling of exceptions during payload creation."""
        # Simulate error by passing invalid data
        invalid_tweets = [{"invalid": "structure"}]
        
        payload = create_tweet_payload(
            brief_id="error_brief",
            pool_name="tao",
            tweets_with_targets=invalid_tweets,
            brief_metadata={},
            uid_targets={}
        )
        
        # Function is robust - processes invalid tweets with default values
        assert payload["brief_id"] == "error_brief"
        assert len(payload["tweets"]) == 1  # One invalid tweet processed
        assert payload["summary"]["total_tweets"] == 1
        # Verify tweet has default values for missing fields
        tweet = payload["tweets"][0]
        assert tweet["tweet_id"] == ""  # Default value
        assert tweet["author"] == ""    # Default value for missing author
        assert tweet["score"] == 0.0    # Default value
    
    def test_lang_field_included(self):
        """Test that language field is included in tweets."""
        filtered_tweets = [
            {
                "tweet_id": "123456789",
                "author": "testuser",
                "text": "Test",
                "lang": "es",
                "score": 0.1
            }
        ]
        
        payload = create_tweet_payload(
            brief_id="lang_test",
            pool_name="tao",
            tweets_with_targets=filtered_tweets,
            brief_metadata={},
            uid_targets={}
        )
        
        tweet = payload["tweets"][0]
        assert tweet["lang"] == "es"
        assert "url" not in tweet  # URL removed from payload
    
    def test_unique_creators_counting(self):
        """Test unique creators counting in summary."""
        filtered_tweets = [
            {"tweet_id": "1", "author": "user1", "text": "Tweet 1", "score": 0.1},
            {"tweet_id": "2", "author": "user1", "text": "Tweet 2", "score": 0.2},
            {"tweet_id": "3", "author": "user2", "text": "Tweet 3", "score": 0.3}
        ]
        
        payload = create_tweet_payload(
            brief_id="creators_test",
            pool_name="tao",
            tweets_with_targets=filtered_tweets,
            brief_metadata={},
            uid_targets={}
        )
        
        assert payload["summary"]["total_tweets"] == 3
        assert payload["summary"]["unique_creators"] == 2  # user1 and user2


class TestIntegration:
    """Integration tests for tweet publisher."""
    
    @pytest.mark.asyncio
    async def test_end_to_end_flow(self):
        """Test complete flow from payload creation to publishing."""
        # Create sample data
        filtered_tweets = [
            {
                "tweet_id": "999",
                "author": "integration_user",
                "text": "Integration test tweet",
                "score": 1.0,
                "reasoning": "Perfect match",
                "favorite_count": 100,
                "retweet_count": 50,
                "reply_count": 25,
                "quote_count": 10,
                "bookmark_count": 5,
                "tagged_accounts": ["user2"],
                "created_at": "Wed Nov 03 12:00:00 +0000 2025",
                "lang": "en",
                "engaged_accounts": {"retweets": ["retweeter1"], "quotes": ["quoter1"]}
            }
        ]
        
        brief_metadata = {
            "tag": "integration",
            "qrt": None,
            "budget": 5000.0,
            "daily_budget": 714.29
        }
        
        uid_targets = {
            123: 100.0
        }
        
        # Create payload
        payload = create_tweet_payload(
            brief_id="integration_test_001",
            pool_name="tao",
            tweets_with_targets=filtered_tweets,
            brief_metadata=brief_metadata,
            uid_targets=uid_targets
        )
        
        # Mock publisher for end-to-end test
        with patch('bitcast.validator.reward_engine.utils.brief_tweet_publisher.get_global_publisher') as mock_get_publisher:
            mock_publisher = AsyncMock()
            mock_publisher.publish_unified_payload.return_value = True
            mock_get_publisher.return_value = mock_publisher
            
            # Publish
            success = await publish_brief_tweets(
                brief_tweets_data=payload,
                run_id="integration_run_001",
                endpoint="https://api.test.com/brief-tweets"
            )
            
            # Verify success
            assert success is True
            
            # Verify payload structure passed to publisher
            call_args = mock_publisher.publish_unified_payload.call_args
            published_payload = call_args[1]["payload_data"]
            
            assert published_payload["brief_id"] == "integration_test_001"
            assert len(published_payload["tweets"]) == 1
            
            tweet = published_payload["tweets"][0]
            assert tweet["tweet_id"] == "999"
            assert tweet["author"] == "integration_user"  # Author included
            assert tweet["text"] == "Integration test tweet"  # Text now included
            assert tweet["lang"] == "en"  # Language included
            assert tweet["favorite_count"] == 100
            assert tweet["meets_brief"] is True
            assert "usd_target" in tweet
            assert "alpha_target" in tweet
            # Removed fields should not be present
            assert "url" not in tweet
            assert "reasoning" not in tweet
