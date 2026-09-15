"""Bit-identical v2 scoring math and approved residual-emission economics."""

import numpy as np
import pytest

from bitcast_x.rewards import (
    RewardCampaign,
    RewardTweet,
    TweetReward,
    aggregate_productive_weights,
    apply_v2_bonuses,
    assign_tweets,
    calculate_rewards,
    calculate_tweet_floors,
)
from bitcast_x.scoring import calculate_tweet_score


def test_v2_engagement_score_fixture_is_exact() -> None:
    considered = {"alice": 10.0, "bob": 5.5, "carol": 2.0, "dave": 1.25}
    usernames = ["alice", "bob", "carol", "dave"]
    relationships = np.array(
        [
            [0.0, 7.0, 0.0, 0.0],
            [9.0, 0.0, 0.0, 0.0],
            [4.0, 0.0, 0.0, 0.0],
            [0.0, 0.5, 0.0, 0.0],
        ]
    )

    score, details = calculate_tweet_score(
        {"bob": "retweet", "carol": "quote"},
        author_influence=10.0,
        author="alice",
        considered_accounts=considered,
        relationship_scores=relationships,
        username_to_index={username: index for index, username in enumerate(usernames)},
    )

    assert score == 23.05
    assert [item.model_dump() for item in details] == [
        {
            "username": "bob",
            "influence_score": 5.5,
            "engagement_type": "retweet",
            "relationship_score": 9.0,
            "scale_factor": 0.2,
            "weighted_contribution": 1.1,
        },
        {
            "username": "carol",
            "influence_score": 2.0,
            "engagement_type": "quote",
            "relationship_score": 4.0,
            "scale_factor": 0.325,
            "weighted_contribution": 1.95,
        },
    ]


def fixture_campaigns() -> list[RewardCampaign]:
    return [
        RewardCampaign(
            campaign_id="brief_a",
            reward_pool_usd=700.0,
            max_tweets_per_creator=2,
            tweets=tuple(
                RewardTweet("brief_a", tweet_id, author, miner, score)
                for tweet_id, author, miner, score in [
                    ("t1", "alice", "miner-a", 12.345678),
                    ("t2", "alice", "miner-a", 8.11),
                    ("t3", "bob", "miner-b", 5.0),
                    ("t4", "alice", "miner-a", 2.25),
                    ("t5", "ghost", "miner-c", 4.0),
                ]
            ),
        ),
        RewardCampaign(
            campaign_id="brief_b",
            reward_pool_usd=350.0,
            max_tweets_per_creator=None,
            tweets=tuple(
                RewardTweet("brief_b", tweet_id, author, miner, score)
                for tweet_id, author, miner, score in [
                    ("t2", "alice", "miner-a", 8.11),
                    ("t6", "carol", "miner-c", 9.5),
                    ("t7", "dave", "miner-d", 0.75),
                ]
            ),
        ),
    ]


def test_v2_assignment_and_floor_fixture_remain_exact() -> None:
    campaigns = fixture_campaigns()

    assignments = assign_tweets(campaigns, committed_tweet_ids={"t9"})
    floors = calculate_tweet_floors(campaigns, assignments)

    assert {key: sorted(value) for key, value in assignments.items()} == {
        "brief_a": ["t1", "t2", "t3", "t5"],
        "brief_b": ["t6", "t7"],
    }
    by_miner: dict[str, float] = {}
    for reward in floors:
        by_miner[reward.miner_hotkey] = (
            by_miner.get(reward.miner_hotkey, 0.0) + reward.daily_usd_floor
        )
    assert by_miner == {
        "miner-a": 62.95151594004601,
        "miner-b": 19.865300478352125,
        # v2 dropped the unmapped ghost author; v3 correctly routes that same
        # frozen floor through the attributed miner hotkey instead.
        "miner-c": 59.129986253251715,
        "miner-d": 8.053197328350144,
    }


def test_productive_miners_receive_all_emissions_in_floor_proportions() -> None:
    campaigns = fixture_campaigns()

    weights, floors = calculate_rewards(
        campaigns,
        {"miner-a": 3, "miner-b": 4, "miner-c": 5, "miner-d": 7},
        [0, 3, 4, 5, 7, 9],
    )
    totals: dict[int, float] = {3: 0.0, 4: 0.0, 5: 0.0, 7: 0.0}
    hotkey_uid = {"miner-a": 3, "miner-b": 4, "miner-c": 5, "miner-d": 7}
    for reward in floors:
        totals[hotkey_uid[reward.miner_hotkey]] += reward.daily_usd_floor
    expected_floors = np.array(
        [
            0.0,
            totals[3],
            totals[4],
            totals[5],
            totals[7],
            0.0,
        ],
        dtype=np.float64,
    )
    expected = expected_floors / expected_floors.sum()

    assert np.array_equal(weights, expected)
    assert np.isclose(weights.sum(), 1.0)
    assert weights[0] == 0.0


def test_no_productive_content_preserves_all_to_burn_fallback() -> None:
    weights, floors = calculate_rewards([], {}, [0, 1, 2])

    assert floors == []
    assert np.array_equal(weights, np.array([1.0, 0.0, 0.0], dtype=np.float64))


def test_v2_performance_then_featured_bonus_fixture_is_exact() -> None:
    campaign = RewardCampaign(
        campaign_id="campaign",
        reward_pool_usd=700,
        max_tweets_per_creator=None,
        tweets=(
            RewardTweet(
                "campaign",
                "t1",
                "1",
                "miner-a",
                2.0,
                author_username="alice",
                followers_count=500,
                views_count=1000,
                favorite_count=100,
            ),
            RewardTweet(
                "campaign",
                "t2",
                "2",
                "miner-b",
                1.0,
                author_username="bob",
                followers_count=500,
                views_count=500,
                favorite_count=50,
                engagement_usernames=("alice",),
            ),
        ),
    )

    adjusted = apply_v2_bonuses(campaign, {"t1", "t2"})

    first, second = adjusted.tweets
    assert first.performance_bonus_pct == 20.0
    assert first.performance_bonus_breakdown == {
        "views": 5.0,
        "views_per_follower": 5.0,
        "total_engagements": 5.0,
        "engagement_per_view": 5.0,
    }
    assert second.performance_bonus_pct == 12.5
    assert first.featured_tweet_id == second.featured_tweet_id == "t2"
    assert first.featured_tweet_bonus is second.featured_tweet_bonus is True
    assert first.score == 2.52
    assert second.score == 1.1812500000000001


def _tweet_reward(
    campaign_id: str,
    tweet_id: str,
    miner_hotkey: str,
    *,
    score: float,
    daily_usd_floor: float,
) -> TweetReward:
    return TweetReward(
        campaign_id=campaign_id,
        tweet_id=tweet_id,
        creator_x_id=tweet_id,
        miner_hotkey=miner_hotkey,
        score=score,
        daily_usd_floor=daily_usd_floor,
    )


def test_score_blend_zero_is_bit_identical_to_floor_proportions() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=10.0, daily_usd_floor=60.0),
        _tweet_reward("c1", "t2", "miner-a", score=5.0, daily_usd_floor=40.0),
        _tweet_reward("c2", "t3", "miner-b", score=1.0, daily_usd_floor=100.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    blended = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=0.0)
    proportional = aggregate_productive_weights(rewards, hotkey_to_uid, uids)

    assert np.array_equal(blended, proportional)
    assert np.isclose(blended.sum(), 1.0)


def test_score_blend_one_allocates_by_unique_tweet_scores() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=10.0, daily_usd_floor=90.0),
        _tweet_reward("c1", "t2", "miner-a", score=10.0, daily_usd_floor=90.0),
        _tweet_reward("c2", "t3", "miner-b", score=80.0, daily_usd_floor=20.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    weights = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=1.0)

    assert np.allclose(weights, np.array([0.0, 0.2, 0.8], dtype=np.float64))


def test_duplicate_matches_count_once_toward_score_weights() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=30.0, daily_usd_floor=50.0),
        # Attribution storage re-rows the same match once per validator run;
        # the duplicate must not double either the floor or the score term.
        _tweet_reward("c1", "t1", "miner-a", score=30.0, daily_usd_floor=50.0),
        _tweet_reward("c2", "t2", "miner-b", score=30.0, daily_usd_floor=50.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    weights = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=1.0)

    assert np.allclose(weights, np.array([0.0, 0.5, 0.5], dtype=np.float64))


def test_score_blend_interpolates_between_floor_and_score_vectors() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=10.0, daily_usd_floor=90.0),
        _tweet_reward("c1", "t2", "miner-a", score=10.0, daily_usd_floor=90.0),
        _tweet_reward("c2", "t3", "miner-b", score=80.0, daily_usd_floor=20.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    weights = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=0.5)

    assert np.allclose(weights, np.array([0.0, 0.55, 0.45], dtype=np.float64))


def test_blend_falls_back_to_floors_when_no_positive_scores_exist() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=0.0, daily_usd_floor=90.0),
        _tweet_reward("c2", "t2", "miner-b", score=0.0, daily_usd_floor=10.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    weights = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=0.5)

    assert np.allclose(weights, np.array([0.0, 0.9, 0.1], dtype=np.float64))


def test_score_shares_fall_back_to_floor_shares_when_floors_absent() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=30.0, daily_usd_floor=0.0),
        _tweet_reward("c2", "t2", "miner-b", score=10.0, daily_usd_floor=0.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    weights = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=0.5)

    assert np.allclose(weights, np.array([0.0, 0.75, 0.25], dtype=np.float64))


def test_negative_scores_are_ignored_in_score_vector() -> None:
    rewards = [
        _tweet_reward("c1", "t1", "miner-a", score=-5.0, daily_usd_floor=50.0),
        _tweet_reward("c2", "t2", "miner-b", score=25.0, daily_usd_floor=50.0),
    ]
    hotkey_to_uid = {"miner-a": 1, "miner-b": 2}
    uids = [0, 1, 2]

    weights = aggregate_productive_weights(rewards, hotkey_to_uid, uids, score_blend=1.0)

    assert np.allclose(weights, np.array([0.0, 0.0, 1.0], dtype=np.float64))


def test_no_productive_content_still_burns_when_blended() -> None:
    weights = aggregate_productive_weights([], {}, [0, 1, 2], score_blend=0.5)

    assert np.array_equal(weights, np.array([1.0, 0.0, 0.0], dtype=np.float64))


def test_score_blend_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        aggregate_productive_weights([], {}, [0, 1], score_blend=1.5)
    with pytest.raises(ValueError):
        aggregate_productive_weights([], {}, [0, 1], score_blend=-0.1)
