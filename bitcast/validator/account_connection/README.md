# Account Connection

Discovers connection tags by fetching replies to designated connection tweets, then extracting `Stitch-hk:` / `Stitch3-` tags (and legacy `bitcast-hk:` / `bitcast-x` tags) from matching replies.

## Overview

Miners register by replying to one or more designated tweets with a connection tag. The scanner fetches replies to those tweets and processes them.

### Tag Formats (Stitch3)
- `Stitch-hk:{substrate_hotkey}` - Direct hotkey connection
- `Stitch3-{identifier}` - No-code mining connection

### Legacy Tag Formats
- `bitcast-hk:{substrate_hotkey}` - Direct hotkey connection
- `bitcast-x{identifier}` - No-code mining connection

All formats support an optional referral code suffix:
- `Stitch-hk:{substrate_hotkey}-{referral_code}`
- `Stitch3-{identifier}-{referral_code}`
- `bitcast-hk:{substrate_hotkey}-{referral_code}`
- `bitcast-x{identifier}-{referral_code}`

Referral codes are URL-safe Base64-encoded X handles (no padding).

### Example Connection Replies
```
Stitch-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq
Stitch3-abc123-ZHJlYWRib25nMA
bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq
bitcast-xabc123-ZHJlYWRib25nMA
```

## How It Works

1. **Fetch replies** - For each tweet ID in `CONNECTION_TWEET_IDS`, fetches replies via `TwitterClient.fetch_post_replies()`
2. **Cross-reference** - Filters replies to authors in the social map
3. **Extract tags** - Parses `Stitch-hk:` / `Stitch3-` and legacy `bitcast-hk:` / `bitcast-x` tags from reply text
4. **Store** - Saves connections to SQLite database
5. **Publish** - Optionally publishes to data API

Provider-specific behaviour:
- **Desearch**: Uses `/twitter/replies/post` to fetch replies
- **RapidAPI**: Uses `/tweet/details` with cursor-based pagination

Runs every `SCORING_INTERVAL_MINUTES` (default: 20 min).

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
├── connection_scanner.py  # Reply-based scanner + CLI
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

Tweet IDs are hardcoded in `bitcast/validator/utils/config.py` as `CONNECTION_TWEET_IDS`.

## Data Flow

```
Fetch replies to CONNECTION_TWEET_IDS → Filter by Social Map → Extract Tags → Store in SQLite
```

## Testing

```bash
pytest tests/validator/account_connection/ -v
```
