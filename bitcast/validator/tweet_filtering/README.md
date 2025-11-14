# Tweet Filtering

Evaluates scored tweets against campaign briefs using LLM to determine brief compliance.

## Overview

Takes scored tweets and evaluates each one against campaign brief requirements using LLM-based content analysis. Returns tweets that pass or fail the brief with detailed reasoning.

## Architecture

```
tweet_filtering/
├── tweet_filter.py          # Main orchestrator + CLI
├── scored_tweets_loader.py  # Load scored tweets
├── brief_evaluator.py       # LLM evaluation wrapper
└── filtered_tweets/         # Output directory
```

## Usage

### Command Line
```bash
python -m bitcast.validator.tweet_filtering.tweet_filter \
  --brief-id my_brief_123 \
  --brief "Talk about BitCast and tag @bitcast_network" \
  --prompt-version 1 \
  --max-workers 10
```

### Programmatic
```python
from bitcast.validator.tweet_filtering import filter_tweets_for_brief

# Filter tweets for a brief
results = filter_tweets_for_brief(
    brief_id="001_bitcast",
    brief_text="Talk about the bitcast mobile app. Tag @bitcast",
    prompt_version=1,
    max_workers=10
)

# Results: list of dicts with meets_brief boolean
for tweet in results:
    if tweet['meets_brief']:
        print(f"✓ @{tweet['author']}: {tweet['tweet_id']}")
```

## Data Flow

```
Load Scored Tweets → Construct Brief → Evaluate (LLM, parallel)
                                              ↓
                              Separate Passed/Failed
                                              ↓
                              Save Results + Return
```

## Output Format

Saved to `filtered_tweets/{brief_id}_{timestamp}.json`:

```json
{
  "metadata": {
    "run_id": "tweet_filtering_001_bitcast_...",
    "brief_id": "001_bitcast",
    "brief_text": "Talk about the bitcast mobile app...",
    "pool_name": "tao",
    "prompt_version": 1,
    "total_evaluated": 150,
    "passed_count": 23,
    "failed_count": 127,
    "pass_rate": 0.153,
    "execution_time_seconds": 45.2
  },
  "filtered_tweets": [
    {
      "tweet_id": "123456789",
      "author": "username",
      "text": "Check out the new bitcast app! @bitcast",
      "score": 0.0358,
      "meets_brief": true,
      "reasoning": "Tweet mentions bitcast app and tags @bitcast"
    }
  ],
  "passed_tweets": [...],  // Subset where meets_brief=true
  "failed_tweets": [...]   // Subset where meets_brief=false
}
```

## Configuration

Environment variables:
```python
CHUTES_API_KEY = "..."           # Required for LLM
DISABLE_LLM_CACHING = False      # Enable caching
LLM_CACHE_EXPIRY = 7 * 86400     # 7 days
SOCIAL_DISCOVERY_MAX_WORKERS = 10  # Parallel workers
```

## Pipeline Integration

```
Social Discovery → Tweet Scoring → Tweet Filtering
```

### Full Pipeline Example
```bash
# Step 1: Social discovery
python -m bitcast.validator.social_discovery.social_discovery --pool-name tao

# Step 2: Tweet scoring
python -m bitcast.validator.tweet_scoring.tweet_scorer \
  --pool-name tao \
  --brief-id 001_bitcast \
  --tag "@bitcast"

# Step 3: Tweet filtering
python -m bitcast.validator.tweet_filtering.tweet_filter \
  --brief-id 001_bitcast \
  --brief "Talk about the bitcast mobile app. Tag @bitcast"
```

## LLM Evaluation

- **Model**: Configured in `ChuteClient.py` (default: DeepSeek-V3)
- **Caching**: Disk-based cache enabled by default
- **Temperature**: 0 (deterministic)
- **Parallel Processing**: 10 workers default

### Prompt Versions
Brief evaluation supports versioned prompts:
- **Version 1** (default): Requirement-by-requirement evaluation with evidence

Specify via `--prompt-version` CLI arg or in brief dict.

## Integration

### With Reward Engine
```python
# Called by TwitterEvaluator after scoring
from bitcast.validator.tweet_filtering import filter_tweets_for_brief

filtered = filter_tweets_for_brief(
    brief_id=brief.id,
    brief_text=brief.brief,
    prompt_version=brief.get('prompt_version', 1)
)

# Only passed tweets contribute to rewards
passed_tweets = [t for t in filtered if t['meets_brief']]
```

## Error Handling

- Individual evaluation failures don't stop batch
- Failed evaluations marked as `meets_brief=False`
- Reasoning field contains error details
- All tweets included in output

## Performance

**Typical Performance:**
- Small batch (10-50 tweets): 5-15 seconds
- Medium batch (100-200 tweets): 20-40 seconds
- Large batch (500+ tweets): 1-2 minutes

LLM caching significantly speeds up re-evaluations.

## Troubleshooting

### No scored tweets found
```bash
# Run tweet scoring first
python -m bitcast.validator.tweet_scoring.tweet_scorer --brief-id my_brief
```

### LLM evaluation failures
1. Verify `CHUTES_API_KEY` in `.env`
2. Check network connectivity
3. Review API quota/limits
4. Check logs for detailed errors

### Missing tweet text
- Re-run tweet scoring (recent versions include text)
- Older files may need regeneration

## Testing

```bash
# Run tests
pytest tests/validator/tweet_filtering/ -v

# With coverage
pytest tests/validator/tweet_filtering/ --cov=bitcast.validator.tweet_filtering
```

## Adding Prompt Versions

1. Create new prompt generator in `clients/prompts.py`
2. Add to `PROMPT_GENERATORS` registry
3. Test with sample brief
4. Update documentation
