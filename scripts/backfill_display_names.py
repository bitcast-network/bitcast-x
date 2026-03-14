#!/usr/bin/env python3
"""
Backfill display_name for all x_accounts where display_name IS NULL.

Fetches display names from Desearch /twitter/user/posts (count=1) to
minimise API cost, then updates x_accounts in the DB.

Usage (on validator server):
    cd /path/to/bitcast-x
    source bitcast/validator/.env  # or however you load the env
    python3 scripts/backfill_display_names.py

    # Dry run (no DB writes):
    python3 scripts/backfill_display_names.py --dry-run

    # Limit to first N accounts (for testing):
    python3 scripts/backfill_display_names.py --limit 10

Env vars required:
    DESEARCH_API_KEY  - Desearch.ai API key
    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
"""

import argparse
import os
import sys
import time
import mysql.connector
import requests
from dotenv import load_dotenv

# Load .env from bitcast/validator/.env if not already in environment
script_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(script_dir, '..', 'bitcast', 'validator', '.env')
if os.path.exists(env_path):
    load_dotenv(dotenv_path=env_path)

DESEARCH_BASE_URL = "https://api.desearch.ai"
DELAY_BETWEEN_CALLS = 0.5   # seconds between API calls
BATCH_UPDATE_SIZE = 50       # write to DB every N accounts
MAX_RETRIES = 2
RETRY_DELAY = 5.0


def get_db_conn():
    return mysql.connector.connect(
        host=os.environ['MYSQL_HOST'],
        port=int(os.environ.get('MYSQL_PORT', 25060)),
        user=os.environ['MYSQL_USER'],
        password=os.environ['MYSQL_PASSWORD'],
        database=os.environ['MYSQL_DATABASE'],
        charset='utf8mb4',
    )


def fetch_display_name(username: str, api_key: str) -> str | None:
    """
    Fetch display name for a username via Desearch /twitter/user/posts (count=1).
    Returns display name string or None on failure.
    """
    url = f"{DESEARCH_BASE_URL}/twitter/user/posts"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"username": username, "count": 1}

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 429:
                print(f"  [rate-limit] @{username}, sleeping {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue
            if resp.status_code == 404:
                print(f"  [not-found] @{username}")
                return None
            if resp.status_code >= 400:
                print(f"  [error {resp.status_code}] @{username}")
                return None
            data = resp.json()
            user = data.get('user', {})
            name = user.get('name', '').strip() if user else ''
            return name if name else None
        except Exception as e:
            print(f"  [exception] @{username}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def main():
    parser = argparse.ArgumentParser(description="Backfill display_name for x_accounts")
    parser.add_argument('--dry-run', action='store_true', help="Fetch but don't write to DB")
    parser.add_argument('--limit', type=int, default=0, help="Max accounts to process (0 = all)")
    args = parser.parse_args()

    api_key = os.environ.get('DESEARCH_API_KEY', '')
    if not api_key:
        print("ERROR: DESEARCH_API_KEY not set")
        sys.exit(1)

    conn = get_db_conn()
    cursor = conn.cursor(dictionary=True)

    # Fetch all accounts missing display_name
    query = "SELECT id_x_accounts, username FROM x_accounts WHERE display_name IS NULL ORDER BY id_x_accounts"
    if args.limit:
        query += f" LIMIT {args.limit}"
    cursor.execute(query)
    accounts = cursor.fetchall()
    total = len(accounts)
    print(f"Found {total} accounts with null display_name")

    if args.dry_run:
        print("[DRY RUN] No DB writes will occur\n")

    updates = []  # (display_name, id_x_accounts)
    found = 0
    not_found = 0

    for i, row in enumerate(accounts, 1):
        uid = row['id_x_accounts']
        username = row['username']
        display_name = fetch_display_name(username, api_key)

        if display_name:
            found += 1
            print(f"[{i}/{total}] @{username} -> '{display_name}'")
            updates.append((display_name, uid))
        else:
            not_found += 1
            print(f"[{i}/{total}] @{username} -> (no display name)")

        # Flush batch to DB
        if not args.dry_run and len(updates) >= BATCH_UPDATE_SIZE:
            cursor.executemany(
                "UPDATE x_accounts SET display_name = %s WHERE id_x_accounts = %s",
                updates
            )
            conn.commit()
            print(f"  [db] Wrote {len(updates)} updates")
            updates = []

        time.sleep(DELAY_BETWEEN_CALLS)

    # Final flush
    if not args.dry_run and updates:
        cursor.executemany(
            "UPDATE x_accounts SET display_name = %s WHERE id_x_accounts = %s",
            updates
        )
        conn.commit()
        print(f"  [db] Wrote {len(updates)} updates")

    cursor.close()
    conn.close()

    print(f"\n--- Done ---")
    print(f"  Found:     {found}")
    print(f"  Not found: {not_found}")
    print(f"  Total:     {total}")


if __name__ == '__main__':
    main()
