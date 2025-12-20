# Tweet Scoring

Scores tweets from pool members based on engagement from influential accounts, weighted by PageRank scores.

## Overview

Calculates weighted scores for tweets based on:
- **Baseline Score**: All tweets get author's influence × 0.5
- **Engagement Bonus**: Additional score from RTs (1.0x) and quotes (3.0x)
- **Scoring Accounts**: Top 256 accounts by PageRank from social map
- Only engagement from influential accounts counts

## Architecture

```
tweet_scoring/
├── tweet_scorer.py           # Main orchestrator + CLI
├── social_map_loader.py      # Social map utilities
├── tweet_filter.py           # Content filtering
├── engagement_analyzer.py    # RT/QRT detection
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
# Score tweets for a brief
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123 \
    --pool-name tao

# With date range (scores only tweets within brief window)
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123 \
    --start-date 2024-01-01 \
    --end-date 2024-01-07

# With tag filter
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123 \
    --tag "#bittensor"

# With quoted tweet filter
python -m bitcast.validator.tweet_scoring.tweet_scorer \
    --brief-id my_brief_123 \
    --qrt "1983210945288569177"

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
Maps at brief start + during brief → Union of Active Members (who can post)
Latest Social Map → Considered Accounts (for engagement weighting)
                      ↓
            Fetch Tweets (parallel)
                      ↓
      Filter (type, date, language, tag, qrt)
                      ↓
         Analyze Engagement (RTs/QRTs)
                      ↓
    Calculate Weighted Scores (using latest map weights)
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

### TweetFilter
```python
tweet_filter = TweetFilter(
    language="en",
    tag="#bittensor",
    qrt="1983210945288569177"
)
filtered = tweet_filter.filter_tweets(tweets)
```

### EngagementAnalyzer
```python
analyzer = EngagementAnalyzer()
engagements = analyzer.get_engagements_for_tweet(
    tweet, all_tweets, considered_accounts
)
```

### ScoreCalculator
```python
calculator = ScoreCalculator(considered_accounts_map)
score, details = calculator.calculate_tweet_score(engagements)
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

Typical runtime for 128-member pool:
- Tweet fetching: 2-3 minutes (parallel, cached)
- Filtering: <10 seconds
- Scoring: <30 seconds
- **Total**: 3-5 minutes with caching

## Troubleshooting

### No social map found
```bash
# Run social discovery first
python -m bitcast.validator.social_discovery.social_discovery --pool-name tao
```

### Twitter API errors
- Check `DESEARCH_API_KEY` in `.env` (required for scoring)
- Verify API quota on Desearch.ai
- Wait if rate limited

### No tweets scored
- Check language setting matches tweets
- Verify tweets within lookback period
- Ensure active members have original tweets

## Testing

```bash
# Run tests
pytest tests/validator/tweet_scoring/ -v

# With coverage
pytest tests/validator/tweet_scoring/ --cov=bitcast.validator.tweet_scoring
```
