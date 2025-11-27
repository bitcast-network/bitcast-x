# Social Discovery

PageRank-based social network discovery engine for X platform that analyzes influence networks.

## Overview

Discovers and analyzes Twitter/X social influence networks using PageRank algorithm. Analyzes interactions (mentions, retweets, quotes) to identify influential accounts within keyword-defined pools. Results are used for scoring tweets in the reward pipeline.

**Key Features:**
- PageRank-based influence scoring
- Keyword and language filtering
- Dynamic pool membership management
- Recursive discovery with convergence detection
- Concurrent processing for performance
- Automatic social map publishing

## Architecture

```
social_discovery/
├── social_discovery.py          # Main analyzer & CLI
├── pool_manager.py               # Pool configuration loader
├── pool_status_manager.py        # Membership & transitions
├── recursive_discovery.py        # Iterative discovery
├── social_map_publisher.py       # Publishing to API
├── pools_config.json             # Pool definitions
└── social_maps/                  # Output directory
    ├── tao/
    ├── ai_crypto/
    └── tao_mandarin/
```

## Usage

### Command Line

```bash
# Single discovery run
python -m bitcast.validator.social_discovery.social_discovery \
  --pool-name tao

# Recursive discovery (runs until convergence)
python -m bitcast.validator.social_discovery.recursive_discovery \
  --pool-name tao \
  --max-iterations 10 \
  --convergence-threshold 0.95
```

### Programmatic

```python
from bitcast.validator.social_discovery import discover_social_network

# Single discovery
social_map_path = discover_social_network(pool_name="tao")

# Recursive discovery until convergence
from bitcast.validator.social_discovery.recursive_discovery import recursive_social_discovery

path, iterations, converged, metrics = recursive_social_discovery(
    pool_name="tao",
    max_iterations=10,
    convergence_threshold=0.95
)
```

### Integration (Validator)

```python
from bitcast.validator.social_discovery import run_discovery_for_stale_pools

# Auto-run for pools needing updates (every 2 weeks on Sunday)
# Automatically forces cache refresh for fresh data
results = run_discovery_for_stale_pools()
# Returns: {'tao': '/path/to/social_map.json', ...}
```

## Data Flow

```
Load Pool Config → Fetch Seed Tweets → Build Interaction Network
                                              ↓
                                    Filter by Keywords/Language
                                              ↓
                                    Calculate PageRank Scores
                                              ↓
                                    Apply Pool Membership Logic
                                              ↓
                                    Save Results + Publish
```

## Key Components

### TwitterNetworkAnalyzer

Main analysis engine that:
- Fetches tweets from seed accounts (concurrent/sequential)
- Builds weighted interaction graph
- Calculates PageRank scores
- Normalizes scores to sum to 1.0
- Generates adjacency matrix

**Interaction Weights:**
- Mentions: 1.0
- Retweets: 2.0  
- Quotes: 1.5

### PoolManager

Loads pool configurations from `pools_config.json`:
```json
{
  "pools": [
    {
      "name": "tao",
      "keywords": ["bittensor", "$tao"],
      "initial_accounts": ["opentensor"],
      "max_members": 64,
      "min_interaction_weight": 0,
      "lang": "en"
    }
  ]
}
```

### Network Discovery

Discovers and ranks all accounts in the social network:
1. Filters accounts by `min_interaction_weight` threshold (quality check)
2. Ranks all accounts by PageRank score
3. Saves all discovered accounts sorted by score

### RecursiveDiscovery

Iteratively runs discovery until convergence:
1. Uses top N accounts (configured via `max_seed_accounts`) from previous iteration as seeds
2. Discovers new accounts through their interactions
3. Calculates stability metric (overlap between iterations)
4. Stops when `stability >= convergence_threshold`

**Stability Formula:**
```
stability = intersection(prev_members, current_members) / union(prev_members, current_members)
```

## Output Format

### Social Map
Saved to `social_maps/{pool_name}/{timestamp}.json`:

```json
{
  "metadata": {
    "created_at": "2025-11-11T12:00:00",
    "pool_name": "tao",
    "total_accounts": 500
  },
  "accounts": {
    "opentensor": {
      "score": 0.045678
    },
    "username2": {
      "score": 0.034521
    }
  }
}
```

**Note:** Accounts are stored sorted by score (highest to lowest). All discovered accounts are included - eligibility filtering is handled at the brief level.

### Adjacency Matrix
Saved to `social_maps/{pool_name}/{timestamp}_adjacency.json`:

```json
{
  "usernames": ["user1", "user2", "user3"],
  "adjacency_matrix": [[0, 2.0, 1.0], [1.0, 0, 0], [1.5, 0, 0]],
  "created_at": "2025-11-11T12:00:00"
}
```

### Metadata
Saved to `social_maps/{pool_name}/{timestamp}_metadata.json`:

```json
{
  "run_id": "vali_x_5ABC...XYZ_20251111_120000",
  "validator_hotkey": "5ABC...XYZ",
  "created_at": "2025-11-11T12:00:00",
  "pool_name": "tao"
}
```

### Recursive Summary
Saved to `social_maps/{pool_name}/recursive_summary_{timestamp}.json`:

```json
{
  "pool_name": "tao",
  "converged": true,
  "total_iterations": 4,
  "convergence_threshold": 0.95,
  "final_social_map": "/path/to/final_map.json",
  "metrics": {
    "total_iterations": 4,
    "final_stability": 0.96,
    "iterations": [...]
  }
}
```

## Configuration

Environment variables (in `utils/config.py`):

```python
# PageRank parameters
PAGERANK_MENTION_WEIGHT = 1.0    # Weight for mentions
PAGERANK_RETWEET_WEIGHT = 2.0    # Weight for retweets
PAGERANK_QUOTE_WEIGHT = 1.5      # Weight for quotes
PAGERANK_ALPHA = 0.85            # Damping factor

# Concurrency
SOCIAL_DISCOVERY_MAX_WORKERS = 10  # Parallel workers (1=sequential)

# Publishing
ENABLE_DATA_PUBLISH = True       # Enable API publishing
```

## Pipeline Integration

```
Social Discovery → Tweet Scoring → Tweet Filtering → Reward Engine
```

Social discovery provides the foundation for the entire reward pipeline:
1. **Discovers** influential accounts in each pool
2. **Scores** their influence via PageRank
3. **Tweet Scoring** uses these scores to weight engagement
4. **Reward Engine** distributes rewards based on weighted performance

## Scheduling

### Automatic Schedule
- Runs **every 2 weeks on Sunday** (UTC)
- Reference date: November 09, 2025
- Only processes pools with stale social maps (not updated today)

### Manual Override
Can be run anytime via CLI for immediate discovery.

## Cache Behavior

**Bi-weekly discovery** automatically forces fresh Twitter API data to ensure accurate analysis.

**Manual/CLI runs** use cached data (refreshed if stale) for faster iteration.

## Performance

**Typical Performance (tao pool, 64 members):**
- **Sequential mode** (1 worker): 180-240 seconds
- **Concurrent mode** (10 workers): 45-90 seconds
- Speedup: ~3-4x with concurrency

**Factors affecting speed:**
- Number of seed accounts
- Tweets per account (fetches last 50)
- Total discovered accounts
- API rate limits
- Network latency

## Troubleshooting

### Pool not found
```
ValueError: Pool 'xyz' not found in configuration
```
**Solution:** Add pool to `pools_config.json` with required fields.

### No interactions found
```
ValueError: No interactions found in network
```
**Causes:**
- Seed accounts have no recent interactions
- Keywords too restrictive (no matches)
- Min followers threshold too high
- Language filter excluding all accounts

**Solution:** Review pool config, relax filters, check seed accounts.

### Twitter API failures
- Verify API credentials in `.env`
- Check rate limits (429 errors)
- Ensure accounts are public
- Check for suspended/deleted accounts

### Recursive discovery not converging
- Increase `max_iterations` (default: 10)
- Lower `convergence_threshold` (default: 0.95)
- Check if pool is inherently unstable (borderline members)
- Review pool size vs available accounts

### Publishing failures
```
⚠️ Social map publishing failed
```
- Non-blocking: Local results are always saved
- Check `ENABLE_DATA_PUBLISH` setting
- Verify global publisher initialized
- Review API connectivity

## Development

### Adding a New Pool

1. Add to `pools_config.json`:
```json
{
  "name": "my_pool",
  "keywords": ["keyword1", "keyword2"],
  "initial_accounts": ["user1", "user2"],
  "max_members": 64,
  "min_interaction_weight": 0,
  "lang": "en"
}
```

2. Run discovery:
```bash
python -m bitcast.validator.social_discovery.social_discovery --pool-name my_pool
```

### Testing

```bash
# Activate virtual environment
source ~/venv_bitcast_x/bin/activate

# Run social discovery tests
pytest tests/validator/social_discovery/ -v

# Test specific component
pytest tests/validator/social_discovery/test_network_analyzer.py -v

# With coverage
pytest tests/validator/social_discovery/ --cov=bitcast.validator.social_discovery
```

### Adjusting PageRank Weights

Edit `bitcast/validator/utils/config.py`:
```python
PAGERANK_MENTION_WEIGHT = 1.0  # Increase for more mention influence
PAGERANK_RETWEET_WEIGHT = 2.0  # Retweets = strongest signal
PAGERANK_QUOTE_WEIGHT = 1.5    # Quotes = medium signal
PAGERANK_ALPHA = 0.85          # Higher = more weight on links vs random jumps
```

## Technical Details

### PageRank Algorithm

Uses NetworkX's PageRank implementation with:
- **Weighted edges**: Different interaction types have different weights
- **Damping factor (α)**: 0.85 (standard)
- **Max iterations**: 1000
- **Convergence tolerance**: 1.0e-6

### Normalization

Scores are normalized to sum exactly to 1.0:
1. Calculate raw PageRank scores
2. Normalize: `score / total_score`
3. Round to 6 decimal places
4. Adjust highest scorer to ensure exact sum of 1.0

### Concurrency Model

- **Sequential mode** (1 worker): Processes accounts one by one
- **Concurrent mode** (2+ workers): ThreadPoolExecutor for parallel API calls
- Applies to: Tweet fetching, relevance checking
- Not applied to: Graph construction, PageRank calculation (CPU-bound)

### Account Ranking

All discovered accounts are ranked by PageRank score. Accounts are stored in descending score order in the social map. Brief-level configuration determines which top N accounts are eligible for mining and whose engagement is considered for scoring.

## Integration Points

- **Tweet Scoring**: Uses social map scores to weight engagement metrics
- **Reward Engine**: Indirectly via tweet scoring influence on rewards
- **Account Connection**: Social maps help identify accounts to track
- **API Publishing**: Shares social maps with validators and network
- **Pools Config**: Centralized pool definitions used across validator
