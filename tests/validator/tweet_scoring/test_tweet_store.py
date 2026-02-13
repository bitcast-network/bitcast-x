"""Tests for TweetStore accumulative cache."""

import pytest
import tempfile
import os
from datetime import datetime, timezone
from unittest.mock import patch

from bitcast.validator.tweet_scoring.tweet_store import TweetStore


@pytest.fixture
def store(tmp_path):
    """Create a TweetStore with a temporary directory."""
    with patch.object(TweetStore, '_cache', None), \
         patch.object(TweetStore, '_instance', None), \
         patch('bitcast.validator.tweet_scoring.tweet_store.TWEET_STORE_DIR', str(tmp_path)):
        s = TweetStore()
        yield s
        TweetStore.cleanup()
        TweetStore._instance = None


class TestTweetStorage:
    """Test tweet storage and retrieval."""
    
    def test_store_new_tweet(self, store):
        tweet = {
            'tweet_id': '123',
            'author': 'alice',
            'text': 'Hello world',
            'created_at': 'Mon Feb 03 10:00:00 +0000 2026',
            'lang': 'en',
            'favorite_count': 10,
            'retweet_count': 5,
            'quoted_tweet_id': '999',
            'quoted_user': 'opentensor',
        }
        
        is_new = store.store_tweet(tweet)
        assert is_new is True
        
        stored = store.get_tweet('123')
        assert stored is not None
        assert stored['author'] == 'alice'
        assert stored['text'] == 'Hello world'
        assert stored['favorite_count'] == 10
        assert stored['quoted_tweet_id'] == '999'
        assert stored['quoted_user'] == 'opentensor'
        assert 'first_seen' in stored
        assert 'last_updated' in stored
    
    def test_update_existing_tweet(self, store):
        """Test that updating a tweet merges engagement stats."""
        tweet_v1 = {
            'tweet_id': '123',
            'author': 'alice',
            'text': 'Hello',
            'created_at': 'Mon Feb 03 10:00:00 +0000 2026',
            'favorite_count': 10,
            'retweet_count': 5,
        }
        
        assert store.store_tweet(tweet_v1) is True  # New
        
        tweet_v2 = {
            'tweet_id': '123',
            'author': 'alice',
            'text': 'Hello',
            'created_at': 'Mon Feb 03 10:00:00 +0000 2026',
            'favorite_count': 20,  # Updated
            'retweet_count': 12,   # Updated
        }
        
        assert store.store_tweet(tweet_v2) is False  # Updated existing
        
        stored = store.get_tweet('123')
        assert stored['favorite_count'] == 20
        assert stored['retweet_count'] == 12
    
    def test_store_tweets_batch(self, store):
        tweets = [
            {'tweet_id': '1', 'author': 'alice', 'text': 'Tweet 1'},
            {'tweet_id': '2', 'author': 'bob', 'text': 'Tweet 2'},
            {'tweet_id': '3', 'author': 'charlie', 'text': 'Tweet 3'},
        ]
        
        stats = store.store_tweets(tweets)
        assert stats['new'] == 3
        assert stats['updated'] == 0
        
        # Store same tweets again
        stats = store.store_tweets(tweets)
        assert stats['new'] == 0
        assert stats['updated'] == 3
    
    def test_store_tweet_without_id_returns_false(self, store):
        assert store.store_tweet({'author': 'alice'}) is False
    
    def test_get_nonexistent_tweet_returns_none(self, store):
        assert store.get_tweet('nonexistent') is None


class TestTweetQuery:
    """Test querying tweets from the store."""
    
    @pytest.fixture(autouse=True)
    def seed_store(self, store):
        """Seed store with test tweets."""
        self.store = store
        tweets = [
            {
                'tweet_id': '1', 'author': 'alice', 'text': 'QRT about bitcoin #bitcoin',
                'created_at': 'Mon Feb 03 10:00:00 +0000 2026',
                'quoted_tweet_id': '999', 'lang': 'en',
            },
            {
                'tweet_id': '2', 'author': 'bob', 'text': 'Another QRT',
                'created_at': 'Tue Feb 04 12:00:00 +0000 2026',
                'quoted_tweet_id': '999', 'lang': 'en',
            },
            {
                'tweet_id': '3', 'author': 'charlie', 'text': 'Not a QRT #bitcoin',
                'created_at': 'Wed Feb 05 08:00:00 +0000 2026',
                'lang': 'en',
            },
            {
                'tweet_id': '4', 'author': 'dave', 'text': 'Old QRT',
                'created_at': 'Wed Jan 15 08:00:00 +0000 2026',
                'quoted_tweet_id': '999', 'lang': 'en',
            },
        ]
        store.store_tweets(tweets)
    
    def test_query_by_quoted_tweet_id(self):
        results = self.store.query_tweets(quoted_tweet_id='999')
        assert len(results) == 3  # alice, bob, dave
        tweet_ids = {t['tweet_id'] for t in results}
        assert tweet_ids == {'1', '2', '4'}
    
    def test_query_by_authors(self):
        results = self.store.query_tweets(authors={'alice', 'bob'})
        assert len(results) == 2
        authors = {t['author'] for t in results}
        assert authors == {'alice', 'bob'}
    
    def test_query_by_tag(self):
        results = self.store.query_tweets(tag='#bitcoin')
        assert len(results) == 2  # alice and charlie
    
    def test_query_by_date_range(self):
        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 4, 23, 59, 59, tzinfo=timezone.utc)
        
        results = self.store.query_tweets(start_date=start, end_date=end)
        assert len(results) == 2  # alice (Feb 3) and bob (Feb 4)
    
    def test_query_combined_filters(self):
        """Test that filters are ANDed together."""
        results = self.store.query_tweets(
            authors={'alice', 'bob', 'charlie'},
            quoted_tweet_id='999'
        )
        assert len(results) == 2  # alice and bob (charlie has no QRT)
    
    def test_query_no_filters_returns_all(self):
        results = self.store.query_tweets()
        assert len(results) == 4


class TestEngagementStorage:
    """Test engagement (RT/QRT) storage."""
    
    def test_store_retweeters(self, store):
        stats = store.store_retweeters('123', ['alice', 'bob'])
        assert stats['new'] == 2
        assert stats['total'] == 2
        
        # Add more retweeters (including duplicate)
        stats = store.store_retweeters('123', ['bob', 'charlie'])
        assert stats['new'] == 1  # Only charlie is new
        assert stats['total'] == 3
    
    def test_store_quoters(self, store):
        qrt_tweets = [
            {'tweet_id': 'qrt1', 'author': 'alice', 'text': 'Quote 1'},
            {'tweet_id': 'qrt2', 'author': 'bob', 'text': 'Quote 2'},
        ]
        
        stats = store.store_quoters('123', qrt_tweets)
        assert stats['new'] == 2
        assert stats['total'] == 2
        
        # Add more (including duplicate)
        more_qrts = [
            {'tweet_id': 'qrt2', 'author': 'bob', 'text': 'Quote 2'},  # Duplicate
            {'tweet_id': 'qrt3', 'author': 'charlie', 'text': 'Quote 3'},
        ]
        
        stats = store.store_quoters('123', more_qrts)
        assert stats['new'] == 1  # Only charlie is new
        assert stats['total'] == 3
    
    def test_get_engagements(self, store):
        store.store_retweeters('123', ['alice', 'bob'])
        store.store_quoters('123', [
            {'tweet_id': 'qrt1', 'author': 'charlie'}
        ])
        
        engagements = store.get_engagements('123')
        assert 'alice' in engagements['retweeters']
        assert 'bob' in engagements['retweeters']
        assert 'charlie' in engagements['quoters']
        assert engagements['quoters']['charlie']['quote_tweet_id'] == 'qrt1'
    
    def test_get_engagements_nonexistent(self, store):
        engagements = store.get_engagements('nonexistent')
        assert engagements['retweeters'] == {}
        assert engagements['quoters'] == {}
    
    def test_engagements_accumulate(self, store):
        """Test that engagements accumulate across multiple stores."""
        # Run 1: Find alice and bob as retweeters
        store.store_retweeters('123', ['alice', 'bob'])
        
        # Run 2: API only returns bob (alice disappeared from API)
        store.store_retweeters('123', ['bob'])
        
        # Alice should still be there (accumulative)
        engagements = store.get_engagements('123')
        assert 'alice' in engagements['retweeters']
        assert 'bob' in engagements['retweeters']
    
    def test_quoter_stores_tweet_id(self, store):
        """Test that quoter records include the quote tweet ID."""
        store.store_quoters('123', [
            {'tweet_id': 'qrt1', 'author': 'alice'}
        ])
        
        engagements = store.get_engagements('123')
        assert engagements['quoters']['alice']['quote_tweet_id'] == 'qrt1'


class TestStats:
    """Test store statistics."""
    
    def test_get_stats(self, store):
        store.store_tweets([
            {'tweet_id': '1', 'author': 'alice'},
            {'tweet_id': '2', 'author': 'bob'},
        ])
        store.store_retweeters('1', ['charlie'])
        
        stats = store.get_stats()
        assert stats['tweets'] == 2
        assert stats['engagement_records'] == 1
