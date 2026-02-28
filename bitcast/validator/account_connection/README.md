# Account Connection

Discovers connection tags by searching for tweets containing a search keyword (e.g. `@bitcast`), then extracting `bitcast-hk:` and `bitcast-x` tags from matching tweets.

## Overview

Connection tweets must include:
1. The search keyword (default: `@bitcast`) - so the search API can find them
2. A connection tag - to link the X account to a Bittensor UID

### Tag Formats
- `bitcast-hk:{substrate_hotkey}` - Direct hotkey connection
- `bitcast-x{identifier}` - No-code mining connection

Both formats support an optional referral code suffix:
- `bitcast-hk:{substrate_hotkey}-{referral_code}`
- `bitcast-x{identifier}-{referral_code}`

Referral codes are URL-safe Base64-encoded X handles (no padding).

### Example Connection Tweets
```
@bitcast bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq
@bitcast bitcast-xabc123-ZHJlYWRib25nMA
```

## How It Works

1. **Search API** - Searches for tweets containing `@bitcast` (configurable via `CONNECTION_SEARCH_TAG`)
2. **Cross-reference** - Filters results to authors in the social map
3. **Extract tags** - Parses `bitcast-hk:` and `bitcast-x` tags from tweet text
4. **Store** - Saves connections to SQLite database
5. **Publish** - Optionally publishes to data API

Uses dual-sort search (both "latest" and "top") for maximum coverage. Runs every hour.

## Automatic Connection Download

New validators automatically download existing connections from the reference validator on startup:

1. Checks if `connections.db` is empty
2. Downloads all connections from reference validator API (`/account-connections`)
3. Stores in local database

```bash
# Manual download
python -m bitcast.validator.account_connection.download_connections

# Force download even with existing connections
python -m bitcast.validator.account_connection.download_connections --force
```

## Architecture

```
account_connection/
├── connection_scanner.py  # Search-based scanner + CLI
├── connection_db.py       # SQLite operations
├── tag_parser.py          # Tag extraction/validation
├── referral_code.py       # Referral code encode/decode
├── connection_publisher.py # Data API publishing
├── download_connections.py # Bootstrap from reference validator
└── connections.db         # SQLite database
```

## Usage

### Command Line
```bash
# Scan all pools
python -m bitcast.validator.account_connection.connection_scanner

# Scan specific pool
python -m bitcast.validator.account_connection.connection_scanner --pool-name tao

# Custom lookback period
python -m bitcast.validator.account_connection.connection_scanner --lookback-days 14
```

### Programmatic
```python
from bitcast.validator.account_connection import ConnectionScanner, ConnectionDatabase

# Scan all pools
scanner = ConnectionScanner()
summary = await scanner.scan_all_pools()

# Query database
db = ConnectionDatabase()
connections = db.get_connections_by_account("username")
accounts = db.get_accounts_with_uids("tao", metagraph)
```

## Configuration

```python
CONNECTION_SEARCH_TAG = '@bitcast'  # Keyword to search for (env: CONNECTION_SEARCH_TAG)
SCORING_INTERVAL_MINUTES = 45  # Scoring + connection scan frequency
```

## Data Flow

```
Search API ("@bitcast") → Filter by Social Map → Extract Tags → Store in SQLite
```

## Testing

```bash
pytest tests/validator/account_connection/ -v
```
