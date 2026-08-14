"""V1-compatible monitoring bonuses and pinned featured-tweet state."""

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.rewards import (
    RewardCampaign,
    TweetReward,
    apply_v2_featured_bonus,
    apply_v2_performance_bonus,
)
from bitcast_x.validator.scoring import ScoredAttribution


class FeaturedSelection(BaseModel):
    """The exact durable JSON record written by v1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    brief_id: str = Field(min_length=1)
    tweet_id: str = Field(pattern=r"^[0-9]+$")
    author: str = Field(min_length=1)
    views_count: int = Field(ge=0)
    selected_at: datetime
    selection_pool: tuple[str, ...]
    selection_method: str


class FeaturedSelectionStore:
    """Read and atomically create v1's pinned featured selections."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, campaign: CampaignRecord) -> FeaturedSelection | None:
        """Load an existing selection without creating one."""

        path = self.root / campaign.primary_pool / f"{campaign.access.campaign_id}.json"
        if not path.exists():
            return None
        try:
            selection = FeaturedSelection.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ProtocolError(f"invalid legacy featured selection: {path}") from exc
        if selection.brief_id != campaign.access.campaign_id:
            raise ProtocolError(f"legacy featured selection identity mismatch: {path}")
        return selection

    def select(
        self,
        campaign: CampaignRecord,
        scores: list[ScoredAttribution],
        *,
        now: datetime,
    ) -> FeaturedSelection | None:
        threshold = datetime.combine(campaign.closes_at.date(), time.min, UTC) - timedelta(days=1)
        if now.astimezone(UTC) < threshold:
            return None
        path = self.root / campaign.primary_pool / f"{campaign.access.campaign_id}.json"
        selection = self.load(campaign)
        if selection is not None:
            return selection
        passing = [item for item in scores if item.meets_brief]
        if not passing:
            return None
        pool = sorted(passing, key=lambda item: -item.tweet.views_count)[:5]
        identifiers = sorted(item.attribution.tweet_id for item in pool)
        selected = pool[hashlib.sha256(",".join(identifiers).encode()).digest()[0] % len(pool)]
        selection = FeaturedSelection(
            brief_id=campaign.access.campaign_id,
            tweet_id=selected.attribution.tweet_id,
            author=selected.tweet.author,
            views_count=selected.tweet.views_count,
            selected_at=now,
            selection_pool=tuple(item.attribution.tweet_id for item in pool),
            selection_method="sha256_mod",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(selection.model_dump(mode="json"), indent=2) + "\n")
        temporary.replace(path)
        return selection


def monitoring_bonus_rewards(
    campaign: CampaignRecord,
    scores: list[ScoredAttribution],
    selection: FeaturedSelection | None,
) -> list[TweetReward]:
    """Return zero-value display rewards carrying v1 monitoring bonus fields."""

    passing = [item for item in scores if item.meets_brief]
    campaign_rewards = RewardCampaign(
        campaign_id=campaign.access.campaign_id,
        reward_pool_usd=float(campaign.reward_pool_usd),
        max_tweets_per_creator=campaign.max_tweets_per_creator,
        tweets=tuple(
            __import__("bitcast_x.rewards", fromlist=["RewardTweet"]).RewardTweet(
                campaign_id=campaign.access.campaign_id,
                tweet_id=item.attribution.tweet_id,
                creator_x_id=item.tweet.author_x_id,
                miner_hotkey=item.attribution.miner_hotkey or "",
                score=item.score,
                author_username=item.tweet.author,
                followers_count=item.author_followers_count,
                views_count=item.tweet.views_count,
                favorite_count=item.tweet.favorite_count,
                retweet_count=item.tweet.retweet_count,
                reply_count=item.tweet.reply_count,
                quote_count=item.tweet.quote_count,
                bookmark_count=item.tweet.bookmark_count,
                engagement_usernames=item.engagements,
            )
            for item in passing
        ),
    )
    assigned = {item.attribution.tweet_id for item in passing}
    adjusted = apply_v2_performance_bonus(campaign_rewards, assigned)
    if selection is not None:
        adjusted = apply_v2_featured_bonus(adjusted, assigned, selection.tweet_id)
    return [
        TweetReward(
            campaign_id=item.campaign_id,
            tweet_id=item.tweet_id,
            creator_x_id=item.creator_x_id,
            miner_hotkey=item.miner_hotkey,
            score=item.score,
            daily_usd_floor=0.0,
            performance_bonus_pct=item.performance_bonus_pct,
            performance_bonus_breakdown=item.performance_bonus_breakdown,
            featured_tweet_bonus=item.featured_tweet_bonus,
            featured_tweet_id=item.featured_tweet_id,
        )
        for item in adjusted.tweets
    ]
