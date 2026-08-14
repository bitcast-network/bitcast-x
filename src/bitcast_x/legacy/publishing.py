"""Backward-compatible legacy connection and referral ingestion payloads."""

from datetime import UTC, date, datetime

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.legacy.connections import Connection
from bitcast_x.legacy.pricing import LegacyPricingSnapshot
from bitcast_x.legacy.snapshots import LegacyRewardSnapshot
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.publishing import create_brief_tweets_payload
from bitcast_x.validator.scoring import ScoredAttribution

ACCOUNT_CONNECTIONS_PAYLOAD_TYPE = "x_account_connections"
REFERRAL_BONUSES_PAYLOAD_TYPE = "referral_bonuses"


def capped_monitoring_scores(
    campaign: CampaignRecord,
    scored: list[ScoredAttribution],
) -> list[ScoredAttribution]:
    """Apply v1's per-creator display cap during the monitoring window.

    V1 applies ``max_tweets`` before publishing a scoring-phase brief. It
    ranks passing tweets by score, views, likes, then oldest timestamp, while
    retaining every failed evaluation. First-emission publication is different:
    all passing candidates remain visible at zero when assignment excludes
    them, so callers must use this helper only through ``scoring_close_block``.
    """

    cap = campaign.max_tweets_per_creator
    if cap is None:
        return scored
    passing_by_author: dict[str, list[ScoredAttribution]] = {}
    failed: list[ScoredAttribution] = []
    for item in scored:
        if not item.meets_brief:
            failed.append(item)
            continue
        passing_by_author.setdefault(item.tweet.author.casefold(), []).append(item)
    passing: list[ScoredAttribution] = []
    for author_scores in passing_by_author.values():
        passing.extend(
            sorted(
                author_scores,
                key=lambda item: (
                    -item.score,
                    -item.tweet.views_count,
                    -item.tweet.favorite_count,
                    item.tweet.created_at,
                ),
            )[:cap]
        )
    return sorted(
        passing + failed,
        key=lambda item: (
            item.attribution.campaign_id,
            item.attribution.tweet_id,
            item.attribution.miner_hotkey or "",
        ),
    )


def brief_tweets_payload(
    campaign: CampaignRecord,
    rewards: list[TweetReward],
    scored: list[ScoredAttribution],
    hotkey_to_uid: dict[str, int],
    *,
    pricing: LegacyPricingSnapshot,
    timestamp: datetime | None = None,
    snapshot: LegacyRewardSnapshot | None = None,
) -> dict[str, object]:
    """Produce v2's legacy tweet contract without preclaim-only evidence."""

    # V2 freezes the rewarded tweet set at the start of emission and publishes
    # that snapshot for the full seven-day window. Fresh search results must
    # not be mixed into a replay or the visible/paid recipient set changes.
    payload_rewards = [] if snapshot is not None else rewards
    payload_scores = [] if snapshot is not None else scored
    payload = create_brief_tweets_payload(
        campaign,
        payload_rewards,
        {
            (item.attribution.campaign_id, item.attribution.tweet_id): item
            for item in payload_scores
        },
        hotkey_to_uid,
        timestamp=timestamp,
    )
    payload.pop("attribution_decisions", None)
    tweets = payload.get("tweets", [])
    if isinstance(tweets, list):
        for tweet in tweets:
            if isinstance(tweet, dict):
                tweet.pop("attribution", None)
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary.pop("attribution_accepted", None)
        summary.pop("attribution_pending", None)
        summary.pop("attribution_rejected", None)
    if snapshot is not None:
        _append_snapshot_tweets(payload, snapshot, pricing=pricing)
    return payload


def _append_snapshot_tweets(
    payload: dict[str, object],
    snapshot: LegacyRewardSnapshot,
    *,
    pricing: LegacyPricingSnapshot,
) -> None:
    """Restore v2 snapshot rows when a frozen tweet is absent from fresh search."""

    tweets = payload.get("tweets")
    summary = payload.get("summary")
    if not isinstance(tweets, list) or not isinstance(summary, dict):
        return
    present = {str(item.get("tweet_id")) for item in tweets if isinstance(item, dict)}
    uid_targets: dict[int, float] = {}
    existing_targets = summary.get("uid_usd_targets")
    if isinstance(existing_targets, dict):
        uid_targets = {int(uid): float(amount) for uid, amount in existing_targets.items()}
    for item in snapshot.tweet_rewards:
        if item.tweet_id in present:
            continue
        daily = item.total_usd / 7
        uid_targets[item.uid] = uid_targets.get(item.uid, 0.0) + daily
        tweets.append(
            {
                "tweet_id": item.tweet_id,
                "author": item.author,
                "text": item.text,
                "created_at": item.created_at,
                "lang": item.lang,
                "favorite_count": item.favorite_count,
                "retweet_count": item.retweet_count,
                "reply_count": item.reply_count,
                "quote_count": item.quote_count,
                "bookmark_count": item.bookmark_count,
                "views_count": item.views_count,
                "score": item.score,
                "performance_bonus_pct": item.performance_bonus_pct,
                "performance_bonus_breakdown": item.performance_bonus_breakdown,
                "featured_tweet_bonus": item.featured_tweet_bonus,
                "retweets": list(item.retweets),
                "quotes": list(item.quotes),
                "author_influence": item.author_influence,
                "baseline_score": item.baseline_score,
                "score_breakdown": list(item.score_breakdown),
                "meets_brief": True,
                "reasoning": "frozen legacy reward snapshot",
                "prescreen_passed": True,
                "usd_target": daily,
                "total_usd_target": item.total_usd,
                "alpha_target": daily / pricing.alpha_price_usd,
                "weight": daily / pricing.daily_miner_usd,
            }
        )
    total = sum(uid_targets.values())
    summary["total_tweets"] = len(tweets)
    summary["total_usd_target"] = total
    summary["unique_creators"] = len(
        {str(item.get("author")) for item in tweets if isinstance(item, dict)}
    )
    summary["uid_usd_targets"] = uid_targets


def connection_payload(
    connections: tuple[Connection, ...], *, timestamp: datetime | None = None
) -> dict[str, object]:
    """Serialize the full frozen connection table exactly as ingestion expects."""

    return {
        "connections": [
            {
                "tweet_id": int(item.tweet_id),
                "tag": item.tag,
                "username": item.account_username,
                "referred_by": item.referred_by,
                "referee_amount": item.referee_amount,
                "referrer_amount": item.referrer_amount,
                "referee_amount_usd": item.referee_amount,
                "referrer_amount_usd": item.referrer_amount,
            }
            for item in connections
        ],
        "timestamp": (timestamp or datetime.now(UTC)).replace(tzinfo=None).isoformat(),
    }


def referral_payload(
    referrals: tuple[Connection, ...],
    account_to_uid: dict[str, int],
    *,
    payout_date: date,
    activated: int = 0,
    timestamp: datetime | None = None,
) -> dict[str, object]:
    """Serialize locked legacy referral amounts without recomputation."""

    bonuses: list[dict[str, object]] = []
    total = 0.0
    for item in referrals:
        if item.referred_by is None:
            continue
        referee_uid = account_to_uid.get(item.account_username)
        if referee_uid is None:
            continue
        bonuses.append(
            {
                "referee": item.account_username,
                "referrer": item.referred_by,
                "referee_uid": referee_uid,
                "referrer_uid": account_to_uid.get(item.referred_by),
                "referee_amount_usd": item.referee_amount,
                "referrer_amount_usd": item.referrer_amount,
            }
        )
        total += item.referee_amount + item.referrer_amount
    return {
        "payout_date": payout_date.isoformat(),
        "bonuses": bonuses,
        "total_usd": total,
        "activated": activated,
        "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
    }
