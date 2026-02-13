# Tweet Scoring

Scores tweets from pool members based on engagement from influential accounts, weighted by PageRank scores.

## Overview

Two discovery modes feed the same accumulative `TweetStore`:
- **Lightweight (every 45 min)**: Search API queries by tag/QRT -- fast but may miss tweets
- **Thorough (every 8 hours)**: Fetches connected accounts' timelines -- slower but comprehensive

Both modes share scoring logic:
- **Direct Engagement Retrieval**: Gets retweeters and QRTs via dedicated API endpoints
- **Baseline Score**: All tweets get author's influence × 0.5
- **Engagement Bonus**: Additional score from RTs (1.0x) and quotes (3.0x)
- **Considered Accounts**: Top 300 accounts by PageRank from social map

**Important**: Briefs must specify either a `tag` or `qrt` field.

## Architecture

```
tweet_scoring/
├── tweet_scorer.py           # Main orchestrator + CLI
├── tweet_discovery.py        # Search-based tweet discovery and engagement retrieval
├── social_map_loader.py      # Social map utilities
├── tweet_filter.py           # Content filtering
├── score_calculator.py       # Weighted scoring
└── scored_tweets/{pool}/     # Output directory
```

## Scoring Formula

```python
# All tweets start with baseline
score = author_influence × 0.5

# Add engagement contributions
for each RT: score += influencer_score × 1.0
for each quote: score += influencer_score × 3.0

# Self-engagement excluded
# Only top 256 accounts' engagement counts
```

## Multi-Map Support for Long Briefs

When a brief spans a social map update (maps refresh every ~2 weeks), the system ensures fairness:

- **If map updated during brief**: Includes **active + relegated** members from latest map
- **If no update during brief**: Includes only **active** members from latest map
- **Considered Accounts (for engagement weighting)**: Always uses **latest map** only
- **Benefit**: Relegated miners remain eligible (they were active when they posted)

Example:
```
Brief: Nov 20 - Nov 25
- Social Map created Nov 23 (during brief)

Latest map (Nov 23):
  Active members (in/promoted): 150 accounts
  Relegated members: 28 accounts (were active before Nov 23)
  
Result:
- Eligible members: 150 + 28 = 178 total
- Engagement weights: From latest map (300 considered accounts)
- @alice (relegated on Nov 23) can still earn from tweets posted Nov 20-22
```

This prevents miners from being retroactively penalized for status changes after they've already posted qualifying content.

## Usage

### Command Line
```bash
# Score tweets for a brief (lightweight search mode)
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123

# Thorough mode (fetches connected accounts' timelines)
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123 \
    --thorough

# Combined filters
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123 \
    --pool-name tao \
    --start-date 2024-01-01 \
    --end-date 2024-01-07 \
    --tag "#bittensor"
```

### Programmatic
```python
from bitcast.validator.tweet_scoring import score_tweets_for_pool
from datetime import datetime

# Score all tweets (past 30 days)
results = score_tweets_for_pool(
    pool_name="tao",
    brief_id="my_brief_123"
)

# Score tweets within brief window
results = score_tweets_for_pool(
    pool_name="tao",
    brief_id="my_brief_123",
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 1, 7)
)

# With filters
results = score_tweets_for_pool(
    pool_name="tao",
    brief_id="my_brief_123",
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 1, 7),
    tag="#bitcast",          # Optional: filter by tag
    qrt="198321..."          # Optional: filter by quoted tweet
)

# Returns list of dicts
for tweet in results[:5]:
    print(f"@{tweet['author']}: {tweet['score']:.6f}")
```

## Data Flow

```
Brief (tag/qrt) → TweetDiscovery.discover_tweets()
                      ↓
              Search API (by tag or quoted_tweet_id)
                      ↓
          Filter to Active Members + Date Range
                      ↓
     For each tweet: TweetDiscovery.get_engagements_for_tweet()
       ├── Get Retweeters API → filter to considered accounts
       └── Search QRTs → filter to considered accounts
                      ↓
    Calculate Weighted Scores (with cabal protection)
                      ↓
             Save Results
```

## Output Format

Saved to `scored_tweets/{pool_name}/{brief_id}_{timestamp}.json`:

```json
{
  "metadata": {
    "run_id": "tweet_scoring_vali_x_{hotkey}_{timestamp}",
    "brief_id": "my_brief_123",
    "pool_name": "tao",
    "tag_filter": "#bittensor",
    "qrt_filter": "1983210945288569177",
    "total_tweets_scored": 1523,
    "tweets_with_engagement": 458,
    "weights": {
      "retweet_weight": 1.0,
      "mention_weight": 2.0,
      "quote_weight": 3.0,
      "BASELINE_TWEET_SCORE_FACTOR": 0.5
    }
  },
  "scored_tweets": [
    {
      "tweet_id": "1234567890",
      "author": "opentensor",
      "text": "Excited about the new update!",
      "url": "https://twitter.com/opentensor/status/1234567890",
      "created_at": "Wed Oct 15 12:00:00 +0000 2025",
      "score": 0.0358,
      "retweets": ["cryptouser1", "anotherfan"],
      "quotes": ["bittensorfan"],
      "quoted_tweet_id": "1234567888"  // If this is a quote tweet
    }
  ]
}
```

## Content Filtering

Filters tweets by:
- **Type**: Original tweets and quote tweets only (no pure RTs)
- **Date**: Tweets within brief window (start_date to end_date, inclusive)
  - Falls back to past 30 days if dates not provided
  - Only tweets in date range are scored
  - RTs/QRTs from outside range still contribute to engagement
- **Language**: Matches pool language if specified
- **Tag**: Optional substring match (case-insensitive)
- **QRT**: Optional filter for tweets quoting specific tweet ID

## Configuration

Environment variables:
```python
TWITTER_DEFAULT_LOOKBACK_DAYS = 30  # Days to look back
PAGERANK_RETWEET_WEIGHT = 1.0       # RT weight
PAGERANK_QUOTE_WEIGHT = 3.0         # Quote weight
BASELINE_TWEET_SCORE_FACTOR = 0.5   # Baseline factor
SOCIAL_DISCOVERY_MAX_WORKERS = 10   # Parallel workers
```

## Key Components

### TweetDiscovery
```python
from bitcast.validator.tweet_scoring import TweetDiscovery, build_search_query

discovery = TweetDiscovery(
    client=twitter_client,
    active_accounts={"alice", "bob"},
    considered_accounts={"alice": 0.5, "bob": 0.4, "influencer": 0.9}
)

# Discover tweets by tag or QRT
tweets = discovery.discover_tweets(
    tag="#bittensor",
    qrt=None,
    start_date=start,
    end_date=end
)

# Get engagements for a tweet
engagements = discovery.get_engagements_for_tweet(tweet)
```

### TweetFilter
```python
tweet_filter = TweetFilter(
    language="en",
    tag="#bittensor",
    qrt="1983210945288569177"
)
filtered = tweet_filter.filter_tweets(tweets)
```

### ScoreCalculator
```python
calculator = ScoreCalculator(considered_accounts_map)
score, details = calculator.calculate_tweet_score(engagements, author_influence)
```

## Integration

### With Reward Engine
```python
# Called by TwitterEvaluator during reward calculation
from bitcast.validator.tweet_scoring import score_tweets_for_pool

scored_tweets = score_tweets_for_pool(
    pool_name=brief.pool,
    brief_id=brief.id,
    tag=brief.tag,
    qrt=brief.qrt
)
```

### With Tweet Filtering
```python
# Scoring output is input to filtering
scored_tweets = score_tweets_for_pool(...)
filtered_tweets = filter_tweets_for_brief(brief_id, brief_text)
```

## Performance

With search-based discovery (typical brief with 50-100 matching tweets):
- Tweet discovery: 5-15 seconds (dual-sort API calls)
- Engagement retrieval: 30-60 seconds (1 RT + 1 QRT call per tweet)
- Scoring: <5 seconds
- **Total**: 1-2 minutes

### Accumulative TweetStore

Data accumulates permanently in TweetStore:
- Tweets found once are scored in all future runs (even if API stops returning them)
- Engagements (RTs/QRTs) accumulate across runs
- Each run makes fresh API calls to discover new data

## Troubleshooting

### Brief missing tag or qrt
```
ValueError: Brief 'my_brief' must specify either 'tag' or 'qrt' field.
```
Briefs now require at least one of `tag` or `qrt` for search-based scoring.

### No social map found
```bash
# Run social discovery first
python -m bitcast.validator.social_discovery.social_discovery --pool-name tao
```

### Twitter API errors
- Check `DESEARCH_API_KEY` or RapidAPI key in `.env`
- Verify API quota
- Wait if rate limited

### No tweets scored
- Verify the tag/qrt matches actual tweet content
- Check date range includes when tweets were posted
- Ensure active members have original tweets matching the filter

## Testing

```bash
# Run tests
pytest tests/validator/tweet_scoring/ -v

# With coverage
pytest tests/validator/tweet_scoring/ --cov=bitcast.validator.tweet_scoring
```
