"""Tests for engagement analyzer."""

import pytest
from bitcast.validator.tweet_scoring.engagement_analyzer import EngagementAnalyzer


@pytest.fixture
def analyzer():
    """Create engagement analyzer instance."""
    return EngagementAnalyzer()


@pytest.fixture
def sample_tweets():
    """Sample tweets with author information."""
    return [
        {
            'tweet_id': '001',
            'author': 'alice',
            'text': 'Original tweet about bittensor',
            'retweeted_user': None,
            'quoted_user': None,
            'retweeted_tweet_id': None,
            'quoted_tweet_id': None
        },
        {
            'tweet_id': '002',
            'author': 'bob',
            'text': 'RT @alice: Original tweet about bittensor',
            'retweeted_user': 'alice',
            'quoted_user': None,
            'retweeted_tweet_id': '001',
            'quoted_tweet_id': None
        },
        {
            'tweet_id': '003',
            'author': 'charlie',
            'text': 'Great point! [quote of alice tweet]',
            'retweeted_user': None,
            'quoted_user': 'alice',
            'retweeted_tweet_id': None,
            'quoted_tweet_id': '001'
        },
        {
            'tweet_id': '004',
            'author': 'alice',  # Self-engagement - should be excluded
            'text': 'RT @alice: Original tweet about bittensor',
            'retweeted_user': 'alice',
            'quoted_user': None,
            'retweeted_tweet_id': '001',
            'quoted_tweet_id': None
        },
        {
            'tweet_id': '005',
            'author': 'david',
            'text': 'Another original tweet',
            'retweeted_user': None,
            'quoted_user': None,
            'retweeted_tweet_id': None,
            'quoted_tweet_id': None
        },
        {
            'tweet_id': '006',
            'author': 'eve',
            'text': 'Commenting on this [quote]',
            'retweeted_user': None,
            'quoted_user': 'alice',
            'retweeted_tweet_id': None,
            'quoted_tweet_id': '001'
        },
        {
            'tweet_id': '007',
            'author': 'bob',  # Bob both RTs and quotes alice
            'text': 'My thoughts on this [quote]',
            'retweeted_user': None,
            'quoted_user': 'alice',
            'retweeted_tweet_id': None,
            'quoted_tweet_id': '001'
        }
    ]


@pytest.fixture
def considered_accounts():
    """Map of considered accounts with influence scores."""
    return {
        'alice': 0.10,
        'bob': 0.08,
        'charlie': 0.06,
        'eve': 0.04,
        'frank': 0.02  # Not in sample tweets
    }


class TestGetEngagementsForTweet:
    """Test the main engagement detection method."""
    
    def test_detects_retweets(self, analyzer, sample_tweets, considered_accounts):
        """Should detect retweets from considered accounts."""
        original = sample_tweets[0]  # Alice's original tweet
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            considered_accounts
        )
        
        # Bob retweeted (tweet 002)
        assert 'bob' in engagements
    
    def test_detects_quotes(self, analyzer, sample_tweets, considered_accounts):
        """Should detect quote tweets from considered accounts."""
        original = sample_tweets[0]  # Alice's original tweet
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            considered_accounts
        )
        
        # Charlie quoted (tweet 003)
        assert 'charlie' in engagements
        assert engagements['charlie'] == 'quote'
        
        # Eve quoted (tweet 006)
        assert 'eve' in engagements
        assert engagements['eve'] == 'quote'
    
    def test_excludes_self_engagement(self, analyzer, sample_tweets, considered_accounts):
        """Should exclude self-engagement."""
        original = sample_tweets[0]  # Alice's original tweet
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            considered_accounts
        )
        
        # Alice retweeted herself (tweet 004) - should be excluded
        assert 'alice' not in engagements
    
    def test_quote_priority_over_retweet(self, analyzer, sample_tweets, considered_accounts):
        """Should prioritize quote over retweet when both exist."""
        original = sample_tweets[0]  # Alice's original tweet
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            considered_accounts
        )
        
        # Bob both retweeted (002) and quoted (007)
        # Should only count as quote (higher priority)
        assert engagements.get('bob') == 'quote'
    
    def test_only_includes_considered_accounts(self, analyzer, considered_accounts):
        """Should only include engagements from considered accounts."""
        original = {
            'tweet_id': '100',
            'author': 'alice',
            'text': 'Original'
        }
        
        all_tweets = [
            original,
            {
                'tweet_id': '101',
                'author': 'bob',  # In considered accounts
                'text': 'RT @alice: Original',
                'retweeted_user': 'alice',
                'quoted_user': None,
                'retweeted_tweet_id': '100',
                'quoted_tweet_id': None
            },
            {
                'tweet_id': '102',
                'author': 'stranger',  # NOT in considered accounts
                'text': 'RT @alice: Original',
                'retweeted_user': 'alice',
                'quoted_user': None,
                'retweeted_tweet_id': '100',
                'quoted_tweet_id': None
            }
        ]
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            all_tweets,
            considered_accounts
        )
        
        # Only bob should be included
        assert 'bob' in engagements
        assert 'stranger' not in engagements
    
    def test_handles_missing_author(self, analyzer, considered_accounts):
        """Should handle tweets with missing author gracefully."""
        original = {
            'tweet_id': '200',
            'author': 'alice',
            'text': 'Original'
        }
        
        all_tweets = [
            original,
            {
                'tweet_id': '201',
                # Missing 'author' field
                'text': 'RT @alice: Original',
                'retweeted_user': 'alice',
                'quoted_user': None,
                'retweeted_tweet_id': '200',
                'quoted_tweet_id': None
            }
        ]
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            all_tweets,
            considered_accounts
        )
        
        # Should not crash, should skip tweet without author
        assert len(engagements) == 0
    
    def test_handles_missing_tweet_id(self, analyzer, sample_tweets, considered_accounts):
        """Should handle original tweet missing ID."""
        original = {
            # Missing 'tweet_id'
            'author': 'alice',
            'text': 'Original'
        }
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            considered_accounts
        )
        
        # Should return empty dict
        assert engagements == {}
    
    def test_case_insensitive_matching(self, analyzer, considered_accounts):
        """Should match usernames case-insensitively."""
        original = {
            'tweet_id': '300',
            'author': 'ALICE',  # Uppercase
            'text': 'Original'
        }
        
        all_tweets = [
            original,
            {
                'tweet_id': '301',
                'author': 'bob',
                'text': 'RT @alice: Original',
                'retweeted_user': 'alice',  # Lowercase
                'quoted_user': None,
                'retweeted_tweet_id': '300',
                'quoted_tweet_id': None
            }
        ]
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            all_tweets,
            considered_accounts
        )
        
        assert 'bob' in engagements
    
    def test_no_engagements(self, analyzer, considered_accounts):
        """Should return empty dict when no engagements."""
        original = {
            'tweet_id': '400',
            'author': 'alice',
            'text': 'Original'
        }
        
        all_tweets = [
            original,
            {
                'tweet_id': '401',
                'author': 'bob',
                'text': 'Unrelated tweet',
                'retweeted_user': None,
                'quoted_user': None
            }
        ]
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            all_tweets,
            considered_accounts
        )
        
        assert engagements == {}
    
    def test_multiple_engagements(self, analyzer, sample_tweets, considered_accounts):
        """Should track multiple engagements correctly."""
        original = sample_tweets[0]  # Alice's original
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            considered_accounts
        )
        
        # Should have engagements from bob, charlie, eve
        assert len(engagements) >= 3
        assert 'bob' in engagements
        assert 'charlie' in engagements
        assert 'eve' in engagements
    
    def test_empty_tweet_list(self, analyzer, considered_accounts):
        """Should handle empty tweet list."""
        original = {
            'tweet_id': '500',
            'author': 'alice',
            'text': 'Original'
        }
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            [],
            considered_accounts
        )
        
        assert engagements == {}
    
    def test_empty_considered_accounts(self, analyzer, sample_tweets):
        """Should return empty when no considered accounts."""
        original = sample_tweets[0]
        
        engagements = analyzer.get_engagements_for_tweet(
            original,
            sample_tweets,
            {}  # Empty considered accounts
        )
        
        assert engagements == {}

