# Reward Engine

Orchestrates reward calculations and weight distribution for miners based on X content performance.

## Overview

The reward engine coordinates the complete pipeline from brief fetching to final reward distribution:
1. Fetches campaign briefs from API
2. Filters briefs by reward window (2-9 days after brief ends)
3. Evaluates content using tweet scoring and filtering
4. Maps social accounts to UIDs via connection database
5. Calculates proportional reward distribution
6. Converts USD targets to normalized weights

## Architecture

```
reward_engine/
├── orchestrator.py              # Main coordinator
├── twitter_evaluator.py         # X platform evaluator
├── interfaces/                  # Abstract interfaces
│   ├── platform_evaluator.py   # Platform evaluation interface
│   ├── emission_calculator.py  # Emission calculation interface
│   └── score_aggregator.py     # Score aggregation interface
├── models/                      # Data models
│   ├── brief.py                # Brief model
│   ├── evaluation_result.py    # Evaluation results
│   ├── score_matrix.py         # Score matrix
│   └── emission_target.py      # Emission targets
├── services/                    # Core services
│   ├── platform_registry.py    # Platform registration
│   ├── score_aggregation_service.py
│   ├── emission_calculation_service.py
│   ├── reward_distribution_service.py
│   └── treasury_allocation.py
└── utils/                       # Utilities
    ├── brief_fetcher.py        # Fetch briefs from API
    ├── brief_tweet_publisher.py # Publish results
    └── reward_snapshot.py      # Snapshot management
```

## Key Concepts

### Reward Window
- **Delay Period**: 2 days after brief ends (for engagement verification)
- **Emissions Period**: 7 days total reward distribution
- **Daily Budget**: `brief.budget / 7` per day
- Only briefs that ended 2-9 days ago are eligible for rewards

### Budget Distribution
```python
# Example: $7000 brief, ended 4 days ago
daily_budget = $7000 / 7 = $1000/day

# Proportional distribution by score
uid_42: 60% of engagement → $600
uid_68: 40% of engagement → $400
```

### Scoring Snapshot
- First day brief enters reward window: Score tweets and save snapshot
- Days 2-7: Reuse existing snapshot (no re-scoring)
- Ensures consistent scores throughout reward period

## Data Flow

```
API (briefs) → Filter by reward window → Get UID mappings
                                              ↓
                                    TwitterEvaluator
                                    ├─ Score tweets
                                    ├─ Filter by LLM
                                    ├─ Map to UIDs
                                    └─ Calculate budget
                                              ↓
                              EvaluationResultCollection
                                              ↓
                              Score Aggregation → ScoreMatrix
                                              ↓
                              Emission Calculation → USD to weights
                                              ↓
                              Reward Distribution → Final weights
```

## Usage

### Automatic (via Validator)
```python
# In validator forward pass
from bitcast.validator.reward_engine import get_reward_orchestrator

orchestrator = get_reward_orchestrator()
rewards, metadata = await orchestrator.calculate_rewards(self, miner_uids)
self.update_scores(rewards, miner_uids)
```

### Manual Testing
```python
from bitcast.validator.reward_engine import RewardOrchestrator
from bitcast.validator.reward_engine.twitter_evaluator import TwitterEvaluator

orchestrator = RewardOrchestrator()
orchestrator.platforms.register('twitter', TwitterEvaluator())

# Run reward calculation
rewards, metadata = await orchestrator.calculate_rewards(validator, uids)
```

## Key Components

### RewardOrchestrator
Main coordinator that:
- Fetches and filters briefs
- Delegates evaluation to platform evaluators
- Aggregates scores across platforms
- Calculates emissions and distributions

### TwitterEvaluator
Evaluates X/Twitter content:
- Loads latest social maps
- Scores tweets by engagement (tweet_scoring module)
- Filters tweets against briefs (tweet_filtering module)
- Maps accounts to UIDs (connection database)
- Returns USD targets per UID

### Services
- **ScoreAggregationService**: Combines scores from multiple briefs into matrix
- **EmissionCalculationService**: Converts USD targets to alpha weights
- **RewardDistributionService**: Normalizes weights and allocates treasury
- **TreasuryAllocation**: Manages subnet treasury percentage

## Configuration

Environment variables (in `utils/config.py`):
```python
EMISSIONS_PERIOD = 7              # Days to distribute budget
REWARDS_DELAY_DAYS = 2            # Days to wait after brief ends
SUBNET_TREASURY_PERCENTAGE = 1.0  # Treasury allocation %
SUBNET_TREASURY_UID = 106         # Treasury UID
REWARDS_INTERVAL_HOURS = 4        # How often to run
```

## Output

### Reward Snapshots
Saved to `reward_snapshots/{pool_name}/{brief_id}_{timestamp}.json`:
```json
{
  "metadata": {
    "brief_id": "001_example",
    "pool_name": "tao",
    "daily_budget_usd": 1000.0,
    "total_uids": 25
  },
  "uid_rewards": [
    {
      "uid": 42,
      "accounts": ["user1", "user2"],
      "tweets_count": 5,
      "total_score": 0.125,
      "usd_amount": 600.0
    }
  ]
}
```

## Development

### Adding a New Platform
1. Implement `PlatformEvaluator` interface
2. Register with `PlatformRegistry`
3. Return `EvaluationResult` objects
4. Platform-agnostic from there

### Testing
```bash
# Run reward engine tests
pytest tests/validator/reward_engine/ -v

# Test specific service
pytest tests/validator/reward_engine/test_orchestrator.py -v
```

## Troubleshooting

### No briefs in reward window
- Briefs must have ended 2-9 days ago (UTC)
- Check brief `end_date` in API response

### No UID mappings found
- Run account connection scan first
- Check `connections.db` has entries for pool

### Scores not updating
- Check tweet scoring ran successfully
- Verify filtered tweets exist for brief
- Review reward snapshot files

## Integration Points

- **Social Discovery**: Provides influence scores for tweet scoring
- **Account Connection**: Maps social accounts to UIDs
- **Tweet Scoring**: Evaluates content engagement
- **Tweet Filtering**: LLM-based brief compliance
- **API**: Fetches campaign briefs

