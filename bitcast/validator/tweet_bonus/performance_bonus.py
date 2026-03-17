"""
Performance bonus calculator for tweet scoring pipeline.

Applies a multiplicative bonus (up to 10%) based on 4 metrics:
1. Views — raw views_count
2. Views per follower — views_count / followers_count
3. Total engagements — favorite_count + retweet_count + reply_count + quote_count + bookmark_count
4. Engagement per view — total_engagements / views_count

Each metric contributes up to 2.5%. The tweet with the highest value for a metric
gets the full 2.5%; others get a proportional amount (value / max * 2.5%).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import bittensor as bt

MAX_BONUS_PER_METRIC = 0.025  # 2.5%


def calculate_performance_bonus(
    tweets: List[Dict],
    follower_counts: Dict[str, int],
    pool_name: str,
    brief_id: str,
) -> List[Dict]:
    """
    Calculate and apply performance bonus to filtered tweets.

    Args:
        tweets: Filtered tweet dicts, each with: score, views_count, favorite_count,
                retweet_count, reply_count, quote_count, bookmark_count, author
        follower_counts: username -> follower count mapping
        pool_name: Pool name (for saving results)
        brief_id: Brief identifier (for saving results)

    Returns:
        The same list with updated score and added performance_bonus_pct field
    """
    if not tweets:
        return tweets

    metrics = _compute_metrics(tweets, follower_counts)
    bonus_results = _compute_bonuses(metrics)

    for tweet, result in zip(tweets, bonus_results):
        tweet['performance_bonus_pct'] = round(result['total'] * 100, 2)
        tweet['performance_bonus_breakdown'] = result['breakdown']
        tweet['score'] = tweet.get('score', 0.0) * (1.0 + result['total'])

    _save_bonus_results(tweets, metrics, pool_name, brief_id)

    totals = [r['total'] for r in bonus_results]
    bt.logging.info(
        f"Applied performance bonus to {len(tweets)} tweets for brief {brief_id} "
        f"(avg bonus: {sum(totals) / len(totals) * 100:.1f}%)"
    )

    return tweets


def _compute_metrics(tweets: List[Dict], follower_counts: Dict[str, int]) -> List[Dict]:
    """Compute the 4 performance metrics for each tweet."""
    metrics = []
    for tweet in tweets:
        views = tweet.get('views_count', 0) or 0
        followers = follower_counts.get(tweet.get('author', ''), 0) or 0

        total_engagements = (
            (tweet.get('favorite_count', 0) or 0)
            + (tweet.get('retweet_count', 0) or 0)
            + (tweet.get('reply_count', 0) or 0)
            + (tweet.get('quote_count', 0) or 0)
            + (tweet.get('bookmark_count', 0) or 0)
        )

        metrics.append({
            'views': views,
            'views_per_follower': views / followers if followers > 0 else 0.0,
            'total_engagements': total_engagements,
            'engagement_per_view': total_engagements / views if views > 0 else 0.0,
        })

    return metrics


def _compute_bonuses(metrics: List[Dict]) -> List[Dict]:
    """Compute proportional bonus for each tweet across all 4 metrics.

    Returns list of dicts with 'total' (float fraction) and 'breakdown' (metric -> pct).
    """
    metric_names = ['views', 'views_per_follower', 'total_engagements', 'engagement_per_view']

    maxes = {
        name: max((m[name] for m in metrics), default=0)
        for name in metric_names
    }

    results = []
    for m in metrics:
        total = 0.0
        breakdown = {}
        for name in metric_names:
            max_val = maxes[name]
            metric_bonus = (m[name] / max_val) * MAX_BONUS_PER_METRIC if max_val > 0 else 0.0
            breakdown[name] = round(metric_bonus * 100, 2)
            total += metric_bonus
        results.append({'total': total, 'breakdown': breakdown})

    return results


def _save_bonus_results(tweets: List[Dict], metrics: List[Dict], pool_name: str, brief_id: str) -> None:
    """Save bonus results to disk for auditing."""
    output_dir = Path(__file__).parent / "tweet_bonus" / pool_name
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    output_file = output_dir / f"{brief_id}_{timestamp_str}.json"

    results = [
        {
            'tweet_id': t.get('tweet_id'),
            'author': t.get('author'),
            'score': t.get('score', 0.0),
            'performance_bonus_pct': t.get('performance_bonus_pct', 0.0),
            'metrics': {
                'views': m['views'],
                'views_per_follower': round(m['views_per_follower'], 4),
                'total_engagements': m['total_engagements'],
                'engagement_per_view': round(m['engagement_per_view'], 4),
            },
        }
        for t, m in zip(tweets, metrics)
    ]

    with open(output_file, 'w') as f:
        json.dump({'brief_id': brief_id, 'pool_name': pool_name, 'results': results}, f, indent=2)

    bt.logging.debug(f"Saved bonus results to {output_file}")
