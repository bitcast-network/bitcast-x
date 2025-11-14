# Account Connection

Scans pool member tweets for connection tags and tracks account-UID mappings in SQLite.

## Overview

Monitors tweets from social discovery pools for special connection tags that link X accounts to Bittensor UIDs:
- `bitcast-hk:{substrate_hotkey}` - Direct hotkey connection
- `bitcast-x` - No-code mining platform connection

By default, only scans accounts with status 'in' or 'promoted' in the social map. Use `--scan-all-accounts` to scan all accounts regardless of status.

## Architecture

```
account_connection/
├── connection_scanner.py  # Main scanner + CLI
├── connection_db.py       # SQLite operations
├── tag_parser.py          # Tag extraction/validation
└── connections.db         # SQLite database
```

## Database Schema

**Location**: `connections.db`  
**Tables**: One per pool (`connections_{pool_name}`)

```sql
CREATE TABLE connections_{pool_name} (
    connection_id INTEGER PRIMARY KEY,
    tweet_id BIGINT NOT NULL,
    tag VARCHAR(100) NOT NULL,
    account_username VARCHAR(100) NOT NULL,
    added DATETIME NOT NULL,
    updated DATETIME NOT NULL,
    UNIQUE(account_username, tag)
);
```

## Usage

### Command Line
```bash
# Scan default pool (tao)
python -m bitcast.validator.account_connection.connection_scanner

# Scan specific pool
python -m bitcast.validator.account_connection.connection_scanner --pool-name tao

# Custom lookback period
python -m bitcast.validator.account_connection.connection_scanner --lookback-days 14

# Scan all accounts in social map (not just 'in' and 'promoted')
python -m bitcast.validator.account_connection.connection_scanner --scan-all-accounts
```

### Programmatic
```python
from bitcast.validator.account_connection import ConnectionScanner, ConnectionDatabase

# Scan pool (active members only)
scanner = ConnectionScanner(lookback_days=7)
summary = scanner.scan_pool("tao")

# Scan all accounts in social map
scanner = ConnectionScanner(lookback_days=7, scan_all=True)
summary = scanner.scan_pool("tao")

# Query database
db = ConnectionDatabase(pool_name="tao")
connections = db.get_connections_by_tag("bitcast-hk:5DNm...")
accounts_with_uids = db.get_accounts_with_uids()
```

## Tag Format

### Valid Tags
- `bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq` (47-48 char hotkey)
- `BITCAST-HK:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY` (case-insensitive)
- `bitcast-x` (no-code mining)

Tags are case-insensitive and must appear in original tweets (not retweets).

## Configuration

Environment variables:
```python
TWITTER_DEFAULT_LOOKBACK_DAYS = 7  # Days to scan back
RAPID_API_KEY = "..."               # Twitter API access
```

## Key Methods

### ConnectionDatabase
```python
# Get all connections for an account
connections = db.get_connections_by_account("username")

# Get all connections for a tag
connections = db.get_connections_by_tag("bitcast-hk:...")

# Get accounts mapped to UIDs (via hotkey tags)
accounts_with_uids = db.get_accounts_with_uids()
# Returns: [{"account_username": "user1", "uid": 42}, ...]

# Check if connection exists
exists = db.connection_exists("username", "bitcast-hk:...")
```

### ConnectionScanner
```python
# Scan a pool for connection tags
summary = scanner.scan_pool()
# Returns: {
#   "accounts_checked": 128,
#   "tags_found": 15,
#   "new_connections": 12,
#   "duplicates_skipped": 3
# }
```

## Integration

### With Reward Engine
```python
# Reward engine uses this to map accounts to UIDs
from bitcast.validator.account_connection import ConnectionDatabase

db = ConnectionDatabase(pool_name="tao")
accounts_with_uids = db.get_accounts_with_uids()

# Map scored tweets to UIDs
for tweet in scored_tweets:
    account = tweet['author']
    uid = next((a['uid'] for a in accounts_with_uids 
                if a['account_username'] == account), None)
```

### With Validator
```python
# Run every 1 hour in validator forward pass
if self.step % (ACCOUNT_CONNECTION_INTERVAL_HOURS * 60) == 0:
    scanner = ConnectionScanner(pool_name="tao")
    scanner.scan_pool()
```

## Data Flow

```
Pool Config → Social Map → Active Members
                               ↓
                      Fetch Recent Tweets
                               ↓
                      Parse Connection Tags
                               ↓
                      Store in Database
                               ↓
                      Return Summary
```

## Troubleshooting

### No tags found
- Normal if pool members haven't posted tags yet
- Verify lookback period is appropriate
- Check tweets manually on X

### Database permission errors
- Ensure write access to `account_connection/` directory
- Check file permissions on `connections.db`

### API errors
- Verify `RAPID_API_KEY` in `.env`
- Check API quota on RapidAPI dashboard

## Testing

```bash
# Run tests
pytest tests/validator/account_connection/ -v

# Tests use isolated temp databases (safe to run)
```
