"""Local legacy snapshot freezing, replay, and emission-weight calculation."""

from datetime import UTC, datetime

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.legacy.connections import Connection
from bitcast_x.legacy.pricing import LegacyPricingSnapshot
from bitcast_x.legacy.snapshots import (
    LegacyRewardSnapshot,
    LegacySnapshotStore,
    LegacyTweetReward,
)
from bitcast_x.protocol import MiningProtocol
from bitcast_x.rewards import (
    RewardCampaign,
    RewardTweet,
    TweetReward,
    apply_v2_bonuses,
    assign_tweets,
    calculate_tweet_floors,
)
from bitcast_x.validator.scoring import ScoredAttribution


class LegacyRewardCoordinator:
    """Preserve v2 first-run freeze and seven-day replay inside v3."""

    def __init__(self, snapshots: LegacySnapshotStore) -> None:
        self._snapshots = snapshots

    def scoring_feed(self, feed: CampaignFeed) -> CampaignFeed:
        """Exclude legacy campaigns whose canonical rewards are already frozen."""

        return feed.model_copy(
            update={
                "campaigns": tuple(
                    campaign
                    for campaign in feed.campaigns
                    if campaign.access.mining_protocol is not MiningProtocol.LEGACY_CONNECTION
                    or self._snapshots.load(
                        campaign.primary_pool,
                        campaign.access.campaign_id,
                    )
                    is None
                )
            }
        )

    def calculate(
        self,
        feed: CampaignFeed,
        scored: list[ScoredAttribution],
        *,
        block: int,
        hotkey_to_uid: dict[str, int],
        uids: list[int],
        pricing: LegacyPricingSnapshot,
        now: datetime | None = None,
    ) -> tuple[dict[int, float], list[TweetReward]]:
        """Return the exact local legacy vector and current daily tweet floors."""

        active = [
            item
            for item in feed.campaigns
            if item.access.mining_protocol is MiningProtocol.LEGACY_CONNECTION
            and item.emission_start_block is not None
            and item.emission_end_block is not None
            and item.emission_start_block <= block <= item.emission_end_block
        ]
        by_campaign: dict[str, list[ScoredAttribution]] = {}
        for item in scored:
            by_campaign.setdefault(item.attribution.campaign_id, []).append(item)

        daily_by_campaign: dict[str, dict[int, float]] = {}
        floors: list[TweetReward] = []
        pending: list[tuple[CampaignRecord, RewardCampaign]] = []
        committed: set[str] = set()
        for campaign in active:
            replay = self._snapshots.replay(campaign.primary_pool, campaign.access.campaign_id)
            if replay is not None:
                committed.update(replay.committed_tweet_ids)
                campaign_daily = daily_by_campaign.setdefault(campaign.access.campaign_id, {})
                for uid, amount in replay.daily_usd_by_uid.items():
                    campaign_daily[uid] = campaign_daily.get(uid, 0.0) + amount
                floors.extend(self._replayed_floors(campaign, replay.snapshot, hotkey_to_uid))
                continue
            tweets = tuple(
                self._reward_tweet(item)
                for item in by_campaign.get(campaign.access.campaign_id, [])
                if item.meets_brief and item.attribution.miner_hotkey in hotkey_to_uid
            )
            pending.append(
                (
                    campaign,
                    RewardCampaign(
                        campaign_id=campaign.access.campaign_id,
                        reward_pool_usd=float(campaign.reward_pool_usd),
                        max_tweets_per_creator=campaign.max_tweets_per_creator,
                        tweets=tweets,
                    ),
                )
            )

        assignments = assign_tweets([item[1] for item in pending], committed_tweet_ids=committed)
        bonus_campaigns = [
            apply_v2_bonuses(item, assignments[item.campaign_id]) for _, item in pending
        ]
        new_floors = calculate_tweet_floors(bonus_campaigns, assignments)
        floors.extend(new_floors)
        scored_by_key = {
            (item.attribution.campaign_id, item.attribution.tweet_id): item for item in scored
        }
        timestamp = now or datetime.now(UTC)
        for campaign, _reward_campaign in pending:
            campaign_floors = [
                item for item in new_floors if item.campaign_id == campaign.access.campaign_id
            ]
            if not campaign_floors:
                continue
            snapshot = LegacyRewardSnapshot(
                brief_id=campaign.access.campaign_id,
                pool_name=campaign.primary_pool,
                created_at=timestamp,
                tweet_rewards=tuple(
                    self._snapshot_reward(item, scored_by_key, hotkey_to_uid)
                    for item in campaign_floors
                ),
            )
            self._snapshots.write_new(snapshot)
        for floor in new_floors:
            floor_uid = hotkey_to_uid.get(floor.miner_hotkey)
            if floor_uid is not None:
                campaign_daily = daily_by_campaign.setdefault(floor.campaign_id, {})
                campaign_daily[floor_uid] = (
                    campaign_daily.get(floor_uid, 0.0) + floor.daily_usd_floor
                )
        caps = {campaign.access.campaign_id: campaign.cap or 1.0 for campaign in active}
        return (
            self._weights(daily_by_campaign, caps, uids, pricing.daily_miner_usd),
            floors,
        )

    @staticmethod
    def _weights(
        daily_by_campaign: dict[str, dict[int, float]],
        caps: dict[str, float],
        uids: list[int],
        daily_usd: float,
    ) -> dict[int, float]:
        if daily_usd <= 0:
            raise ValueError("daily miner USD must be positive")
        weights = {uid: 0.0 for uid in uids}
        for campaign_id, daily_by_uid in daily_by_campaign.items():
            column = {uid: max(daily_by_uid.get(uid, 0.0), 0.0) / daily_usd for uid in uids}
            column_total = sum(column.values())
            cap = caps.get(campaign_id, 1.0)
            if column_total > cap:
                scale = cap / column_total
                column = {uid: weight * scale for uid, weight in column.items()}
            for uid, weight in column.items():
                weights[uid] += weight
        productive = sum(weight for uid, weight in weights.items() if uid != 0)
        if productive > 1:
            weights = {
                uid: (weight / productive if uid != 0 else 0.0) for uid, weight in weights.items()
            }
            productive = 1.0
        weights[0] = 1.0 - productive
        return weights

    @staticmethod
    def apply_referrals(
        weights: dict[int, float],
        referrals: tuple[Connection, ...],
        account_to_uid: dict[str, int],
        *,
        daily_usd: float,
    ) -> dict[int, float]:
        """Add v2's locked due-referral USD, then normalize the resulting vector."""

        if daily_usd <= 0:
            raise ValueError("daily miner USD must be positive")
        output = dict(weights)
        for referral in referrals:
            referee_uid = account_to_uid.get(referral.account_username)
            if referee_uid is not None:
                output[referee_uid] = output.get(referee_uid, 0.0) + (
                    referral.referee_amount / daily_usd
                )
            if referral.referred_by is not None:
                referrer_uid = account_to_uid.get(referral.referred_by)
                if referrer_uid is not None:
                    output[referrer_uid] = output.get(referrer_uid, 0.0) + (
                        referral.referrer_amount / daily_usd
                    )
        total = sum(output.values())
        return {uid: weight / total for uid, weight in output.items()} if total > 0 else output

    @staticmethod
    def _reward_tweet(item: ScoredAttribution) -> RewardTweet:
        return RewardTweet(
            campaign_id=item.attribution.campaign_id,
            tweet_id=item.tweet.tweet_id,
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

    @staticmethod
    def _snapshot_reward(
        floor: TweetReward,
        scored: dict[tuple[str, str], ScoredAttribution],
        hotkey_to_uid: dict[str, int],
    ) -> LegacyTweetReward:
        evidence = scored[(floor.campaign_id, floor.tweet_id)]
        return LegacyTweetReward(
            tweet_id=floor.tweet_id,
            author=evidence.tweet.author,
            uid=hotkey_to_uid[floor.miner_hotkey],
            total_usd=floor.daily_usd_floor * 7,
            miner_hotkey=floor.miner_hotkey,
            creator_x_id=evidence.tweet.author_x_id,
            score=floor.score,
            text=evidence.tweet.text,
            performance_bonus_pct=floor.performance_bonus_pct,
            performance_bonus_breakdown=floor.performance_bonus_breakdown,
            featured_tweet_bonus=floor.featured_tweet_bonus,
            favorite_count=evidence.tweet.favorite_count,
            retweet_count=evidence.tweet.retweet_count,
            reply_count=evidence.tweet.reply_count,
            quote_count=evidence.tweet.quote_count,
            bookmark_count=evidence.tweet.bookmark_count,
            views_count=evidence.tweet.views_count,
            retweets=tuple(
                item.username for item in evidence.details if item.engagement_type == "retweet"
            ),
            quotes=tuple(
                item.username for item in evidence.details if item.engagement_type == "quote"
            ),
            created_at=evidence.tweet.created_at.strftime("%a %b %d %H:%M:%S %z %Y"),
            lang=evidence.tweet.lang,
            author_influence=evidence.author_influence,
            baseline_score=evidence.baseline_score,
            score_breakdown=tuple(
                {
                    "u": item.username,
                    "t": "rt" if item.engagement_type == "retweet" else "qt",
                    "i": item.influence_score,
                    "s": item.scale_factor,
                    "c": item.weighted_contribution,
                }
                for item in evidence.details
            ),
        )

    @staticmethod
    def _replayed_floors(
        campaign: CampaignRecord,
        snapshot: LegacyRewardSnapshot,
        hotkey_to_uid: dict[str, int],
    ) -> list[TweetReward]:
        uid_to_hotkey = {uid: hotkey for hotkey, uid in hotkey_to_uid.items()}
        output: list[TweetReward] = []
        for item in snapshot.tweet_rewards:
            hotkey = str(item.miner_hotkey or uid_to_hotkey.get(item.uid) or "")
            if not hotkey:
                continue
            output.append(
                TweetReward(
                    campaign_id=campaign.access.campaign_id,
                    tweet_id=item.tweet_id,
                    creator_x_id=item.creator_x_id or "0",
                    miner_hotkey=hotkey,
                    score=item.score,
                    daily_usd_floor=item.total_usd / 7,
                    performance_bonus_pct=item.performance_bonus_pct,
                    performance_bonus_breakdown=item.performance_bonus_breakdown,
                    featured_tweet_bonus=item.featured_tweet_bonus,
                )
            )
        return output
