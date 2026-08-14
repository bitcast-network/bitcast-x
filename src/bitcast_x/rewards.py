"""Finalized seven-day campaign floor and unlimited residual-emission scaling."""

import hashlib
from dataclasses import dataclass, replace
from typing import cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from bitcast_x.protocol import AttributionReason

REWARD_SMOOTHING_EXPONENT = 0.65
EMISSIONS_PERIOD_DAYS = 7


@dataclass(frozen=True, slots=True)
class RewardTweet:
    """One finalized attributed and scored tweet entering assignment."""

    campaign_id: str
    tweet_id: str
    creator_x_id: str
    miner_hotkey: str
    score: float
    author_username: str = ""
    followers_count: int = 0
    views_count: int = 0
    favorite_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    bookmark_count: int = 0
    engagement_usernames: tuple[str, ...] = ()
    performance_bonus_pct: float = 0.0
    performance_bonus_breakdown: dict[str, float] | None = None
    featured_tweet_bonus: bool = False
    featured_tweet_id: str | None = None


@dataclass(frozen=True, slots=True)
class RewardCampaign:
    """One finalized campaign floor competing in the current emission day."""

    campaign_id: str
    reward_pool_usd: float
    max_tweets_per_creator: int | None
    tweets: tuple[RewardTweet, ...]

    @property
    def daily_budget_usd(self) -> float:
        """Spread the frozen floor evenly over the existing seven-day state."""

        return self.reward_pool_usd / EMISSIONS_PERIOD_DAYS


@dataclass(frozen=True, slots=True)
class TweetReward:
    """Frozen per-tweet daily floor before residual scaling."""

    campaign_id: str
    tweet_id: str
    creator_x_id: str
    miner_hotkey: str
    score: float
    daily_usd_floor: float
    performance_bonus_pct: float = 0.0
    performance_bonus_breakdown: dict[str, float] | None = None
    featured_tweet_bonus: bool = False
    featured_tweet_id: str | None = None


class RewardDecision(BaseModel):
    """Frozen economic disposition after global assignment and campaign caps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str
    tweet_id: str
    miner_hotkey: str
    accepted: bool
    reason: AttributionReason
    daily_usd_floor: float = 0.0


@dataclass(frozen=True, slots=True)
class AssignmentOutcome:
    """Selected tweet IDs plus a deterministic reason for every candidate edge."""

    assigned: dict[str, set[str]]
    reasons: dict[tuple[str, str], AttributionReason]


def estimate_payouts(
    daily_budget: float,
    tweets: tuple[RewardTweet, ...],
    smoothing_exponent: float = REWARD_SMOOTHING_EXPONENT,
) -> list[float]:
    """Mirror v2's pre-dedup proportional power-law split."""

    smoothed = [max(tweet.score, 0.0) ** smoothing_exponent for tweet in tweets]
    total = sum(smoothed)
    if total <= 0:
        return [0.0] * len(tweets)
    # Parentheses preserve v2's float64 operation order exactly.
    return [daily_budget * (value / total) for value in smoothed]


def assign_tweets(
    campaigns: list[RewardCampaign],
    *,
    committed_tweet_ids: set[str] | None = None,
) -> dict[str, set[str]]:
    """Preserve v2 greedy max-payout assignment, caps, and deterministic tie-breaks."""

    return assign_tweets_with_reasons(
        campaigns,
        committed_tweet_ids=committed_tweet_ids,
    ).assigned


def assign_tweets_with_reasons(
    campaigns: list[RewardCampaign],
    *,
    committed_tweet_ids: set[str] | None = None,
) -> AssignmentOutcome:
    """Assign globally while recording duplicates, caps, and non-positive scores."""

    assigned: dict[str, set[str]] = {campaign.campaign_id: set() for campaign in campaigns}
    reasons: dict[tuple[str, str], AttributionReason] = {}
    taken = set(committed_tweet_ids or set())
    counts: dict[tuple[str, str], int] = {}
    edges: list[tuple[float, str, str, str]] = []
    by_id = {campaign.campaign_id: campaign for campaign in campaigns}
    for campaign in campaigns:
        payouts = estimate_payouts(campaign.daily_budget_usd, campaign.tweets)
        for tweet, payout in zip(campaign.tweets, payouts, strict=True):
            if tweet.score <= 0:
                reasons[(campaign.campaign_id, tweet.tweet_id)] = (
                    AttributionReason.SCORE_BELOW_FLOOR
                )
                continue
            edges.append((payout, campaign.campaign_id, tweet.creator_x_id, tweet.tweet_id))
    edges.sort(key=lambda edge: (-edge[0], edge[1], edge[3]))
    for _payout, campaign_id, creator_x_id, tweet_id in edges:
        if tweet_id in taken:
            reasons[(campaign_id, tweet_id)] = AttributionReason.DUPLICATE_TWEET
            continue
        cap = by_id[campaign_id].max_tweets_per_creator
        key = (creator_x_id, campaign_id)
        if cap is not None and counts.get(key, 0) >= cap:
            reasons[(campaign_id, tweet_id)] = AttributionReason.CAMPAIGN_INELIGIBLE
            continue
        assigned[campaign_id].add(tweet_id)
        reasons[(campaign_id, tweet_id)] = AttributionReason.ACCEPTED
        taken.add(tweet_id)
        counts[key] = counts.get(key, 0) + 1
    return AssignmentOutcome(assigned=assigned, reasons=reasons)


def reward_decisions(
    campaigns: list[RewardCampaign],
    outcome: AssignmentOutcome,
    floors: list[TweetReward],
) -> list[RewardDecision]:
    """Materialize one auditable economic decision for every scored candidate."""

    floor_by_key = {(item.campaign_id, item.tweet_id): item.daily_usd_floor for item in floors}
    decisions: list[RewardDecision] = []
    for campaign in campaigns:
        for tweet in campaign.tweets:
            key = (campaign.campaign_id, tweet.tweet_id)
            reason = outcome.reasons[key]
            decisions.append(
                RewardDecision(
                    campaign_id=campaign.campaign_id,
                    tweet_id=tweet.tweet_id,
                    miner_hotkey=tweet.miner_hotkey,
                    accepted=reason is AttributionReason.ACCEPTED,
                    reason=reason,
                    daily_usd_floor=floor_by_key.get(key, 0.0),
                )
            )
    return sorted(decisions, key=lambda item: (item.campaign_id, item.tweet_id))


def calculate_tweet_floors(
    campaigns: list[RewardCampaign],
    assignments: dict[str, set[str]],
) -> list[TweetReward]:
    """Freeze v2's score^0.65 USD split after global assignment."""

    output: list[TweetReward] = []
    for campaign in campaigns:
        selected = tuple(
            tweet
            for tweet in campaign.tweets
            if tweet.tweet_id in assignments.get(campaign.campaign_id, set())
        )
        payouts = estimate_payouts(campaign.daily_budget_usd, selected)
        for tweet, payout in zip(selected, payouts, strict=True):
            output.append(
                TweetReward(
                    campaign_id=campaign.campaign_id,
                    tweet_id=tweet.tweet_id,
                    creator_x_id=tweet.creator_x_id,
                    miner_hotkey=tweet.miner_hotkey,
                    score=tweet.score,
                    daily_usd_floor=payout,
                    performance_bonus_pct=tweet.performance_bonus_pct,
                    performance_bonus_breakdown=tweet.performance_bonus_breakdown,
                    featured_tweet_bonus=tweet.featured_tweet_bonus,
                    featured_tweet_id=tweet.featured_tweet_id,
                )
            )
    return output


def apply_v2_bonuses(
    campaign: RewardCampaign,
    assigned_tweet_ids: set[str],
    *,
    max_bonus_per_metric: float = 0.05,
    featured_multiplier: float = 1.05,
    featured_top_n: int = 5,
) -> RewardCampaign:
    """Apply v2 performance then featured bonuses to the final assigned subset."""

    performance_adjusted = apply_v2_performance_bonus(campaign, assigned_tweet_ids)
    selected = [
        tweet for tweet in performance_adjusted.tweets if tweet.tweet_id in assigned_tweet_ids
    ]
    if not selected:
        return campaign
    ranked = sorted(
        selected,
        key=lambda item: (-item.views_count, item.tweet_id),
    )[:featured_top_n]
    identifiers = sorted(item.tweet_id for item in ranked)
    featured = ranked[hashlib.sha256(",".join(identifiers).encode()).digest()[0] % len(ranked)]
    return apply_v2_featured_bonus(
        performance_adjusted,
        assigned_tweet_ids,
        featured.tweet_id,
        featured_multiplier=featured_multiplier,
    )


def apply_v2_performance_bonus(
    campaign: RewardCampaign,
    assigned_tweet_ids: set[str],
    *,
    max_bonus_per_metric: float = 0.05,
) -> RewardCampaign:
    """Apply v2's four relative performance bonuses to an assigned subset."""

    selected = [tweet for tweet in campaign.tweets if tweet.tweet_id in assigned_tweet_ids]
    if not selected:
        return campaign
    metrics = [_performance_metrics(tweet) for tweet in selected]
    names = ("views", "views_per_follower", "total_engagements", "engagement_per_view")
    maxima = {name: max(metric[name] for metric in metrics) for name in names}
    adjusted: dict[str, RewardTweet] = {}
    for tweet, metric in zip(selected, metrics, strict=True):
        total_bonus = 0.0
        breakdown: dict[str, float] = {}
        for name in names:
            maximum = maxima[name]
            bonus = metric[name] / maximum * max_bonus_per_metric if maximum > 0 else 0.0
            breakdown[name] = round(bonus * 100, 2)
            total_bonus += bonus
        adjusted[tweet.tweet_id] = replace(
            tweet,
            score=tweet.score * (1.0 + total_bonus),
            performance_bonus_pct=round(total_bonus * 100, 2),
            performance_bonus_breakdown=breakdown,
        )
    return RewardCampaign(
        campaign_id=campaign.campaign_id,
        reward_pool_usd=campaign.reward_pool_usd,
        max_tweets_per_creator=campaign.max_tweets_per_creator,
        tweets=tuple(adjusted.get(tweet.tweet_id, tweet) for tweet in campaign.tweets),
    )


def apply_v2_featured_bonus(
    campaign: RewardCampaign,
    assigned_tweet_ids: set[str],
    featured_tweet_id: str,
    *,
    featured_multiplier: float = 1.05,
) -> RewardCampaign:
    """Apply v2's featured bonus using an already selected, pinned tweet ID."""

    featured = next(
        (tweet for tweet in campaign.tweets if tweet.tweet_id == featured_tweet_id), None
    )
    if featured is None:
        return campaign
    bonus_accounts = {item.casefold() for item in featured.engagement_usernames}
    bonus_accounts.add(featured.author_username.casefold())
    adjusted: dict[str, RewardTweet] = {}
    for tweet in campaign.tweets:
        if tweet.tweet_id not in assigned_tweet_ids:
            continue
        receives_bonus = tweet.author_username.casefold() in bonus_accounts
        adjusted[tweet.tweet_id] = replace(
            tweet,
            score=tweet.score * featured_multiplier if receives_bonus else tweet.score,
            featured_tweet_bonus=receives_bonus,
            featured_tweet_id=featured_tweet_id,
        )
    return RewardCampaign(
        campaign_id=campaign.campaign_id,
        reward_pool_usd=campaign.reward_pool_usd,
        max_tweets_per_creator=campaign.max_tweets_per_creator,
        tweets=tuple(adjusted.get(tweet.tweet_id, tweet) for tweet in campaign.tweets),
    )


def _performance_metrics(tweet: RewardTweet) -> dict[str, float]:
    views = tweet.views_count
    total_engagements = (
        tweet.favorite_count
        + tweet.retweet_count
        + tweet.reply_count
        + tweet.quote_count
        + tweet.bookmark_count
    )
    return {
        "views": float(views),
        "views_per_follower": views / tweet.followers_count if tweet.followers_count > 0 else 0.0,
        "total_engagements": float(total_engagements),
        "engagement_per_view": total_engagements / views if views > 0 else 0.0,
    }


def aggregate_productive_weights(
    tweet_rewards: list[TweetReward],
    hotkey_to_uid: dict[str, int],
    uids: list[int],
) -> NDArray[np.float64]:
    """Allocate all emissions across productive miners in floor proportions."""

    uid_to_index = {uid: index for index, uid in enumerate(uids)}
    floors = np.zeros(len(uids), dtype=np.float64)
    for reward in tweet_rewards:
        uid = hotkey_to_uid.get(reward.miner_hotkey)
        index = uid_to_index.get(uid) if uid is not None else None
        if index is not None:
            floors[index] += reward.daily_usd_floor
    productive_total = floors.sum()
    if productive_total <= 0:
        return _burn(uids)
    return cast(NDArray[np.float64], floors / productive_total)


def _burn(uids: list[int]) -> NDArray[np.float64]:
    """Preserve the availability/no-content fallback: only UID 0 receives weight."""

    return np.array([1.0 if uid == 0 else 0.0 for uid in uids], dtype=np.float64)


def calculate_rewards(
    campaigns: list[RewardCampaign],
    hotkey_to_uid: dict[str, int],
    uids: list[int],
    *,
    committed_tweet_ids: set[str] | None = None,
) -> tuple[NDArray[np.float64], list[TweetReward]]:
    """Run assignment, floor calculation, and approved unlimited multiplier."""

    assignments = assign_tweets(campaigns, committed_tweet_ids=committed_tweet_ids)
    floors = calculate_tweet_floors(campaigns, assignments)
    return aggregate_productive_weights(floors, hotkey_to_uid, uids), floors
