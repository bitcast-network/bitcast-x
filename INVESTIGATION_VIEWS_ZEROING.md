# Views Count Zeroing Investigation and Fix

## Problem

After the brief's end date (or end date + 1), the `views_count` field in the database was being set to 0 for all tweets, even though the tweets had non-zero view counts when initially fetched.

## Root Cause

The bug was in the **reward snapshot system**. The system has two phases:

1. **Scoring Phase** (end_date to end_date + REWARDS_DELAY_DAYS):
   - Tweets are scored fresh
   - `views_count` is properly fetched from Twitter API
   - Data is published with correct view counts

2. **Emission Phase** (end_date + REWARDS_DELAY_DAYS + 1 to end_date + REWARDS_DELAY_DAYS + EMISSIONS_PERIOD):
   - System uses reward snapshots for stable daily payouts
   - **BUG**: Snapshot did not include `views_count` field
   - When tweets were republished from snapshots, they got `views_count=0` (default value)

## The Bug Location

In `bitcast/validator/reward_engine/twitter_evaluator.py`:

### Issue 1: Snapshot Creation (line ~314-330)
```python
tweet_rewards.append({
    'tweet_id': tweet.get('tweet_id'),
    'author': author,
    'uid': uid,
    'score': tweet.get('score', 0.0),
    'total_usd': tweet.get('total_usd_target', 0.0),
    'text': tweet.get('text', ''),
    'favorite_count': tweet.get('favorite_count', 0),
    'retweet_count': tweet.get('retweet_count', 0),
    'reply_count': tweet.get('reply_count', 0),
    'quote_count': tweet.get('quote_count', 0),
    'bookmark_count': tweet.get('bookmark_count', 0),
    # views_count was MISSING here!
    'retweets': tweet.get('retweets', []),
    'quotes': tweet.get('quotes', []),
    'created_at': tweet.get('created_at', ''),
    'lang': tweet.get('lang', 'und')
})
```

### Issue 2: Snapshot Conversion (line ~598-619)
```python
tweets_with_targets.append({
    'tweet_id': tweet_reward.get('tweet_id'),
    'author': tweet_reward.get('author'),
    'text': tweet_reward.get('text', ''),
    'score': tweet_reward.get('score', 0.0),
    'usd_target': daily_usd,
    'total_usd_target': tweet_reward.get('total_usd', 0.0),
    'alpha_target': daily_usd / alpha_price,
    # Include engagement metrics from snapshot
    'favorite_count': tweet_reward.get('favorite_count', 0),
    'retweet_count': tweet_reward.get('retweet_count', 0),
    'reply_count': tweet_reward.get('reply_count', 0),
    'quote_count': tweet_reward.get('quote_count', 0),
    'bookmark_count': tweet_reward.get('bookmark_count', 0),
    # views_count was MISSING here too!
    'retweets': tweet_reward.get('retweets', []),
    'quotes': tweet_reward.get('quotes', []),
    'created_at': tweet_reward.get('created_at', ''),
    'lang': tweet_reward.get('lang', 'und')
})
```

## The Fix

Added `'views_count': tweet.get('views_count', 0)` in both locations:

1. When creating the reward snapshot (line ~326)
2. When converting snapshot back to tweets_with_targets format (line ~616)

This ensures that `views_count` is:
- Captured when the snapshot is first created (during first emission run)
- Preserved and republished when loading from snapshot (subsequent emission runs)

## Additional Improvements

Enhanced logging to help detect similar issues in the future:

1. **TwitterClient** (`bitcast/validator/clients/twitter_client.py`):
   - Added debug logging when tweets have `views_count=0`
   - Logs view state information for diagnostics

2. **Tweet Scorer** (`bitcast/validator/tweet_scoring/tweet_scorer.py`):
   - Added statistics tracking for views_count
   - Warning when >80% of tweets have zero views

3. **Brief Tweet Publisher** (`bitcast/validator/reward_engine/utils/brief_tweet_publisher.py`):
   - Logs views_count statistics for each published brief
   - Warning when ALL tweets have `views_count=0`

## Testing

Created comprehensive test suite in `tests/validator/reward_engine/test_views_count_preservation.py`:

1. `test_snapshot_includes_views_count` - Verifies views_count is saved in snapshots
2. `test_convert_snapshot_to_tweets_with_targets_preserves_views` - Verifies conversion preserves views
3. `test_views_count_zero_is_preserved` - Verifies explicit zero values are preserved
4. `test_snapshot_roundtrip_preserves_all_engagement_metrics` - Full roundtrip test

All tests pass ✅

## Timeline of Bug Effect

The bug manifests:
- ✅ **During Scoring Phase**: Views are correct (fresh Twitter data)
- ❌ **During Emission Phase**: Views become 0 (snapshot missing the field)

This explains why views were 0 "after the end date + REWARDS_DELAY_DAYS" - that's when the system switches from fresh scoring to using snapshots.

## Files Changed

1. `bitcast/validator/reward_engine/twitter_evaluator.py` - Added views_count to snapshots
2. `bitcast/validator/clients/twitter_client.py` - Added diagnostic logging
3. `bitcast/validator/tweet_scoring/tweet_scorer.py` - Added views statistics logging
4. `bitcast/validator/reward_engine/utils/brief_tweet_publisher.py` - Added views statistics logging
5. `tests/validator/reward_engine/test_views_count_preservation.py` - New test suite
6. `investigate_views_zeroing.py` - Investigation script for analyzing historical data

## Verification

To verify the fix is working:
1. Monitor logs during emission phase for "views_count stats" messages
2. Check that published brief tweets have non-zero view counts
3. Run the test suite: `pytest tests/validator/reward_engine/test_views_count_preservation.py`
4. (Optional) Run investigation script: `python investigate_views_zeroing.py --pool tao`
