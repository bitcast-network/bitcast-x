"""Tests for tweet filter."""

import pytest
from bitcast.validator.tweet_scoring.tweet_filter import TweetFilter


class TestMatchesLanguage:
    """Test language matching."""
    
    def test_no_language_requirement_accepts_all(self):
        """Should accept all languages when no requirement."""
        filter = TweetFilter(language=None)
        
        assert filter.matches_language({'lang': 'en'})
        assert filter.matches_language({'lang': 'zh'})
        assert filter.matches_language({'lang': 'fr'})
        assert filter.matches_language({'lang': 'und'})
        assert filter.matches_language({})  # Missing lang
    
    def test_exact_language_match(self):
        """Should match exact language code."""
        filter = TweetFilter(language='zh')
        
        assert filter.matches_language({'lang': 'zh'})
        assert not filter.matches_language({'lang': 'en'})
        assert not filter.matches_language({'lang': 'fr'})
    
    def test_permissive_undefined_language(self):
        """Should accept undefined/missing language (permissive per BA)."""
        filter = TweetFilter(language='zh')
        
        # Permissive: accept undefined and missing
        assert filter.matches_language({'lang': 'und'})
        assert filter.matches_language({'lang': ''})
        assert filter.matches_language({})  # Missing 'lang' field
    
    def test_different_languages(self):
        """Should work with different language codes."""
        filter_en = TweetFilter(language='en')
        filter_fr = TweetFilter(language='fr')
        filter_ko = TweetFilter(language='ko')
        
        tweet_en = {'lang': 'en'}
        tweet_fr = {'lang': 'fr'}
        tweet_ko = {'lang': 'ko'}
        
        assert filter_en.matches_language(tweet_en)
        assert not filter_en.matches_language(tweet_fr)
        
        assert filter_fr.matches_language(tweet_fr)
        assert not filter_fr.matches_language(tweet_ko)
        
        assert filter_ko.matches_language(tweet_ko)
        assert not filter_ko.matches_language(tweet_en)


class TestMatchesQrt:
    """Test QRT (quoted tweet ID) matching."""
    
    def test_no_qrt_requirement_accepts_all(self):
        """Should accept all tweets when no QRT requirement."""
        filter = TweetFilter(qrt=None)
        
        assert filter.matches_qrt({'quoted_tweet_id': '123456'})
        assert filter.matches_qrt({'quoted_tweet_id': '789012'})
        assert filter.matches_qrt({})  # Missing quoted_tweet_id
    
    def test_empty_string_qrt_accepts_all(self):
        """Should treat empty string same as None - accept all tweets."""
        filter = TweetFilter(qrt="")
        
        assert filter.matches_qrt({'quoted_tweet_id': '123456'})
        assert filter.matches_qrt({'quoted_tweet_id': '789012'})
        assert filter.matches_qrt({})
    
    def test_exact_qrt_match(self):
        """Should match exact quoted tweet ID."""
        filter = TweetFilter(qrt='1983210945288569177')
        
        assert filter.matches_qrt({'quoted_tweet_id': '1983210945288569177'})
        assert not filter.matches_qrt({'quoted_tweet_id': '1234567890'})
        assert not filter.matches_qrt({})  # Missing quoted_tweet_id
    
    def test_non_matching_qrt(self):
        """Should reject tweets that quote different tweet IDs."""
        filter = TweetFilter(qrt='1983210945288569177')
        
        assert not filter.matches_qrt({'quoted_tweet_id': '9999999999'})
        assert not filter.matches_qrt({'quoted_tweet_id': '1111111111'})
    
    def test_missing_quoted_tweet_id(self):
        """Should reject tweets without quoted_tweet_id when QRT filter is set."""
        filter = TweetFilter(qrt='1983210945288569177')
        
        # Non-quote tweets should be filtered out
        assert not filter.matches_qrt({})
        assert not filter.matches_qrt({'text': 'Original tweet'})


class TestIsScoreableTweetType:
    """Test tweet type filtering."""
    
    def test_original_tweets_are_scoreable(self):
        """Should accept original tweets."""
        filter = TweetFilter()
        
        assert filter.is_scoreable_tweet_type({'text': 'This is my original tweet'})
        assert filter.is_scoreable_tweet_type({'text': 'Just sharing my thoughts'})
    
    def test_quote_tweets_are_scoreable(self):
        """Should accept quote tweets."""
        filter = TweetFilter()
        
        # Quote tweets don't start with "RT @"
        assert filter.is_scoreable_tweet_type({'text': 'My comment on this interesting post'})
    
    def test_pure_retweets_not_scoreable(self):
        """Should reject pure retweets."""
        filter = TweetFilter()
        
        assert not filter.is_scoreable_tweet_type({'text': 'RT @user: Original content'})
        assert not filter.is_scoreable_tweet_type({'text': 'RT @another_user: Something interesting'})
    
    def test_empty_text(self):
        """Should handle empty text gracefully."""
        filter = TweetFilter()
        
        assert filter.is_scoreable_tweet_type({'text': ''})
        assert filter.is_scoreable_tweet_type({})  # Missing 'text' field


class TestFilterTweets:
    """Test batch filtering."""
    
    def test_filters_by_type_and_language(self):
        """Should filter by type and language."""
        filter = TweetFilter(language='en')
        
        tweets = [
            {'text': 'Original tweet', 'lang': 'en'},  # Should pass
            {'text': 'RT @user: something', 'lang': 'en'},  # Fail: RT
            {'text': 'Another tweet', 'lang': 'zh'},  # Fail: wrong language
            {'text': 'Good tweet', 'lang': 'en'},  # Should pass
            {'text': 'Undefined lang', 'lang': 'und'},  # Should pass (permissive)
        ]
        
        filtered = filter.filter_tweets(tweets)
        
        assert len(filtered) == 3
        assert filtered[0]['text'] == 'Original tweet'
        assert filtered[1]['text'] == 'Good tweet'
        assert filtered[2]['text'] == 'Undefined lang'
    
    def test_empty_list(self):
        """Should handle empty tweet list."""
        filter = TweetFilter()
        
        filtered = filter.filter_tweets([])
        
        assert filtered == []
    
    def test_no_language_requirement(self):
        """Should accept all languages when no requirement."""
        filter = TweetFilter(language=None)
        
        tweets = [
            {'text': 'Tweet in English', 'lang': 'en'},
            {'text': 'Tweet en français', 'lang': 'fr'},
            {'text': 'Tweet 中文', 'lang': 'zh'},
        ]
        
        filtered = filter.filter_tweets(tweets)
        
        # All should pass (no language filter, none are RTs)
        assert len(filtered) == 3
    
    def test_filters_out_retweets(self):
        """Should filter out all retweets."""
        filter = TweetFilter(language='en')
        
        tweets = [
            {'text': 'RT @user: some tweet', 'lang': 'en'},
            {'text': 'RT @another: more content', 'lang': 'en'},
            {'text': 'Original content', 'lang': 'en'},
        ]
        
        filtered = filter.filter_tweets(tweets)
        
        assert len(filtered) == 1
        assert filtered[0]['text'] == 'Original content'
    
    def test_filters_by_qrt(self):
        """Should filter by quoted tweet ID."""
        filter = TweetFilter(qrt='1983210945288569177')
        
        tweets = [
            # Should pass: quotes the target tweet
            {'text': 'My thoughts on this', 'quoted_tweet_id': '1983210945288569177'},
            # Fail: quotes different tweet
            {'text': 'Quote of something else', 'quoted_tweet_id': '9999999999'},
            # Fail: not a quote tweet
            {'text': 'Original tweet'},
            # Should pass: quotes the target tweet
            {'text': 'Great point!', 'quoted_tweet_id': '1983210945288569177'},
        ]
        
        filtered = filter.filter_tweets(tweets)
        
        assert len(filtered) == 2
        assert filtered[0]['text'] == 'My thoughts on this'
        assert filtered[1]['text'] == 'Great point!'
    
    def test_filters_by_tag_and_qrt_combined(self):
        """Should filter by both tag and QRT when both specified."""
        filter = TweetFilter(tag='#bittensor', qrt='1983210945288569177')
        
        tweets = [
            # Should pass: has tag AND quotes target
            {'text': 'Love #bittensor!', 'quoted_tweet_id': '1983210945288569177'},
            # Fail: has tag but wrong QRT
            {'text': 'Another #bittensor post', 'quoted_tweet_id': '9999999999'},
            # Fail: correct QRT but missing tag
            {'text': 'Interesting point', 'quoted_tweet_id': '1983210945288569177'},
            # Should pass: has tag AND quotes target
            {'text': 'Check out #Bittensor', 'quoted_tweet_id': '1983210945288569177'},
        ]
        
        filtered = filter.filter_tweets(tweets)
        
        assert len(filtered) == 2
        assert filtered[0]['text'] == 'Love #bittensor!'
        assert filtered[1]['text'] == 'Check out #Bittensor'
    
    def test_empty_string_filters_accept_all(self):
        """Should treat empty strings same as None - no filtering."""
        filter = TweetFilter(tag="", qrt="")
        
        tweets = [
            {'text': 'Tweet without tag or qrt'},
            {'text': 'Tweet with #tag', 'quoted_tweet_id': '123'},
            {'text': 'Another tweet'},
        ]
        
        filtered = filter.filter_tweets(tweets)
        
        # All should pass - empty string means no filter
        assert len(filtered) == 3

