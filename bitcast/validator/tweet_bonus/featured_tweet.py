"""
Featured tweet bonus for tweet scoring pipeline.

Selects a high-performing "Featured Tweet" from the top 5 by views and awards
a 5% multiplicative bonus (score *= 1.05) to participants who retweet or
quote-tweet it. The featured tweet author also receives the bonus.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt

FEATURED_BONUS_MULTIPLIER = 1.05
FEATURED_DIR = Path(__file__).parent / "featured"


def select_featured_tweet(
    scored_tweets: List[Dict],
    brief: Dict,
    pool_name: str,
) -> Optional[Dict]:
    """
    Select a featured tweet from the top performers by views.

    Selection only happens within 1 day of the brief end_date. Once selected,
    the result is persisted to disk and reused on subsequent calls.

    Args:
        scored_tweets: Scored tweet dicts with views_count, tweet_id, author
        brief: Brief dict with 'id' and 'end_date' fields
        pool_name: Pool name for file organization

    Returns:
        Selection dict with tweet_id, author, views_count, etc. or None if too early
    """
    brief_id = brief['id']
    end_date_str = brief.get('end_date')
    if not end_date_str:
        return None

    # Parse end_date (date string like "2026-03-15")
    try:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        bt.logging.warning(f"Could not parse end_date '{end_date_str}' for brief {brief_id}")
        return None

    # Too early: more than 1 day before end_date
    now = datetime.now(timezone.utc)
    if now < (end_date - timedelta(days=1)):
        return None

    # Check for existing selection on disk
    output_dir = FEATURED_DIR / pool_name
    selection_file = output_dir / f"{brief_id}.json"

    if selection_file.exists():
        with open(selection_file) as f:
            return json.load(f)

    if not scored_tweets:
        return None

    # Sort by views_count descending, take top 5
    sorted_tweets = sorted(scored_tweets, key=lambda t: t.get('views_count', 0), reverse=True)
    pool = sorted_tweets[:5]

    # Deterministic selection via SHA256
    tweet_ids = sorted([t['tweet_id'] for t in pool])
    digest = hashlib.sha256(",".join(tweet_ids).encode()).digest()
    index = digest[0] % len(pool)
    selected = pool[index]

    selection = {
        "brief_id": brief_id,
        "tweet_id": selected['tweet_id'],
        "author": selected['author'],
        "views_count": selected.get('views_count', 0),
        "selected_at": now.isoformat(),
        "selection_pool": [t['tweet_id'] for t in pool],
        "selection_method": "sha256_mod",
    }

    # Persist to disk
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(selection_file, 'w') as f:
        json.dump(selection, f, indent=2)

    bt.logging.info(
        f"Featured tweet selected for brief {brief_id}: "
        f"@{selected['author']} ({selected.get('views_count', 0)} views)"
    )

    return selection


def apply_featured_tweet_bonus(
    tweets: List[Dict],
    featured_selection: Optional[Dict],
    tweet_discovery,
    pool_name: str,
    brief_id: str,
) -> List[Dict]:
    """
    Apply 5% bonus to participants who retweeted/quoted the featured tweet.

    The featured tweet author also receives the bonus.

    Args:
        tweets: Filtered tweet dicts with score and author fields
        featured_selection: Selection dict from select_featured_tweet, or None
        tweet_discovery: TweetDiscovery instance for engagement lookup
        pool_name: Pool name for file organization
        brief_id: Brief identifier

    Returns:
        The same list with updated scores and featured_tweet_bonus flag
    """
    if featured_selection is None:
        return tweets

    featured_tweet_id = featured_selection['tweet_id']
    featured_author = featured_selection['author']

    # Get all engagements (no excluded_engagers — we want participants)
    engagements = tweet_discovery.get_engagements_for_tweet({
        "tweet_id": featured_tweet_id,
        "author": featured_author,
    })

    bonus_accounts = {username.lower() for username in engagements.keys()}
    bonus_accounts.add(featured_author.lower())

    bonus_count = 0
    for tweet in tweets:
        if tweet.get('author', '').lower() in bonus_accounts:
            tweet['score'] = tweet.get('score', 0.0) * FEATURED_BONUS_MULTIPLIER
            tweet['featured_tweet_bonus'] = True
            bonus_count += 1
        else:
            tweet['featured_tweet_bonus'] = False

    # Save bonus results
    _save_featured_bonus_results(
        tweets, featured_selection, bonus_accounts, pool_name, brief_id
    )

    bt.logging.info(
        f"Featured tweet bonus: {bonus_count}/{len(tweets)} tweets received 5% bonus "
        f"for brief {brief_id}"
    )

    return tweets


def _save_featured_bonus_results(
    tweets: List[Dict],
    featured_selection: Dict,
    bonus_accounts: set,
    pool_name: str,
    brief_id: str,
) -> None:
    """Save featured bonus results to disk for auditing."""
    output_dir = FEATURED_DIR / pool_name
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{brief_id}_bonus.json"

    results = {
        "brief_id": brief_id,
        "featured_tweet": featured_selection,
        "bonus_accounts": sorted(bonus_accounts),
        "tweets": [
            {
                "tweet_id": t.get("tweet_id"),
                "author": t.get("author"),
                "score": t.get("score", 0.0),
                "featured_tweet_bonus": t.get("featured_tweet_bonus", False),
            }
            for t in tweets
        ],
    }

    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)

    bt.logging.debug(f"Saved featured bonus results to {output_file}")
