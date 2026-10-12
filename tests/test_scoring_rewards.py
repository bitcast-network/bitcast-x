"""Bit-identical v2 scoring math and floor-first emission economics."""

import numpy as np
import pytest
from pytest import approx

from bitcast_x.rewards import (
    RewardCampaign,
    RewardTweet,
    TweetReward,
    allocate_floor_first_weights,
    apply_v2_featured_bonus,
    apply_v2_performance_bonus,
    assign_tweets_with_reasons,
    calculate_tweet_floors,
    select_v2_featured_tweet,
)
from bitcast_x.scoring import calculate_tweet_score


def test_v2_engagement_score_fixture_is_exact() -> None:
    considered = {"alice": 10.0, "bob": 5.5, "carol": 2.0, "dave": 1.25}
    relationships = {
        ("alice", "bob"): 7.0,
        ("bob", "alice"): 9.0,
        ("carol", "alice"): 4.0,
        ("dave", "bob"): 0.5,
    }

    score, details = calculate_tweet_score(
        {"bob": "retweet", "carol": "quote"},
        author_influence=10.0,
        author="alice",
        considered_accounts=considered,
        relationships=relationships,
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

    assignments = assign_tweets_with_reasons(campaigns, committed_tweet_ids={"t9"}).assigned
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


def test_unpriced_weights_split_all_emissions_in_floor_proportions() -> None:
    campaigns = fixture_campaigns()

    hotkey_uid = {"miner-a": 3, "miner-b": 4, "miner-c": 5, "miner-d": 7}
    floors = calculate_tweet_floors(campaigns, assign_tweets_with_reasons(campaigns).assigned)
    weights = allocate_floor_first_weights(
        floors, hotkey_uid, [0, 3, 4, 5, 7, 9], emission_value_usd=None
    ).weights
    totals: dict[int, float] = {3: 0.0, 4: 0.0, 5: 0.0, 7: 0.0}
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

    assigned = {"t1", "t2"}
    selection = select_v2_featured_tweet(campaign, assigned)
    assert selection is not None
    adjusted = apply_v2_featured_bonus(
        apply_v2_performance_bonus(campaign, assigned), assigned, selection.tweet_id
    )

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


# Miner a carries most of the floor; miner b carries most of the score.
FLOOR_VS_SCORE = [
    _tweet_reward("c1", "t1", "miner-a", score=10.0, daily_usd_floor=90.0),
    _tweet_reward("c1", "t2", "miner-a", score=10.0, daily_usd_floor=90.0),
    _tweet_reward("c2", "t3", "miner-b", score=80.0, daily_usd_floor=20.0),
]
# Attribution storage re-rows the same match once per validator run; the
# duplicate must double neither its floor nor its score.
REPEATED_MATCH = [
    _tweet_reward("c1", "t1", "miner-a", score=30.0, daily_usd_floor=50.0),
    _tweet_reward("c1", "t1", "miner-a", score=30.0, daily_usd_floor=50.0),
    _tweet_reward("c2", "t2", "miner-b", score=30.0, daily_usd_floor=50.0),
]
MINERS = {"miner-a": 1, "miner-b": 2}


@pytest.mark.parametrize(
    ("rewards", "emission_value_usd", "expected", "funding_ratio"),
    [
        pytest.param(
            FLOOR_VS_SCORE,
            1000.0,
            # Floors $180/$200 and $20/$200 of $1000 emission, then the 0.8
            # surplus split 20/80 by score share.
            approx([0.0, 0.18 + 0.16, 0.02 + 0.64]),
            1.0,
            id="floors-first-then-surplus-by-score-share",
        ),
        pytest.param(
            FLOOR_VS_SCORE,
            200.0,
            approx([0.0, 0.9, 0.1]),
            1.0,
            id="exactly-funded-floors-leave-no-surplus",
        ),
        pytest.param(
            FLOOR_VS_SCORE,
            100.0,
            approx([0.0, 0.9, 0.1]),
            0.5,
            id="shortfall-pays-floors-pro-rata",
        ),
        pytest.param(
            FLOOR_VS_SCORE,
            None,
            approx([0.0, 0.9, 0.1]),
            None,
            id="unpriced-weights-follow-floor-proportions",
        ),
        pytest.param(
            REPEATED_MATCH,
            200.0,
            approx([0.0, 0.5, 0.5]),
            1.0,
            id="duplicate-match-counts-once",
        ),
        pytest.param(
            [
                _tweet_reward("c1", "t1", "miner-a", score=-5.0, daily_usd_floor=50.0),
                _tweet_reward("c2", "t2", "miner-b", score=25.0, daily_usd_floor=50.0),
            ],
            200.0,
            approx([0.0, 0.25, 0.75]),
            1.0,
            id="negative-scores-earn-no-surplus",
        ),
        pytest.param(
            [
                _tweet_reward("c1", "t1", "miner-a", score=0.0, daily_usd_floor=90.0),
                _tweet_reward("c2", "t2", "miner-b", score=0.0, daily_usd_floor=10.0),
            ],
            200.0,
            approx([0.0, 0.9, 0.1]),
            1.0,
            id="surplus-follows-floors-without-positive-scores",
        ),
    ],
)
def test_floor_first_weights(
    rewards: list[TweetReward],
    emission_value_usd: float | None,
    expected: object,
    funding_ratio: float | None,
) -> None:
    allocation = allocate_floor_first_weights(
        rewards, MINERS, [0, 1, 2], emission_value_usd=emission_value_usd
    )

    assert allocation.weights.tolist() == expected
    assert allocation.weights.sum() == approx(1.0)
    assert allocation.funding_ratio == (
        approx(funding_ratio) if funding_ratio is not None else None
    )
    assert not allocation.donated_weights.any()


def test_funded_floors_are_always_covered_in_usd() -> None:
    allocation = allocate_floor_first_weights(
        FLOOR_VS_SCORE, MINERS, [0, 1, 2], emission_value_usd=1000.0
    )

    # Miner a holds 90% of floors but only 20% of score share.
    assert allocation.weights[1] * 1000.0 >= 180.0
    assert allocation.weights[2] * 1000.0 >= 20.0
    assert allocation.total_floors_usd == 200.0
    assert allocation.floor_weights.tolist() == approx([0.0, 0.18, 0.02])
    assert allocation.surplus_weights.sum() == approx(0.8)


@pytest.mark.parametrize("emission_value_usd", [None, 1000.0])
def test_no_productive_content_burns(emission_value_usd: float | None) -> None:
    allocation = allocate_floor_first_weights(
        [], MINERS, [0, 1, 2], emission_value_usd=emission_value_usd
    )

    assert allocation.weights.tolist() == [1.0, 0.0, 0.0]
    assert allocation.funding_ratio is None


@pytest.mark.parametrize("emission_value_usd", [0.0, -1.0])
def test_non_positive_emission_value_is_rejected(emission_value_usd: float) -> None:
    with pytest.raises(ValueError):
        allocate_floor_first_weights(
            FLOOR_VS_SCORE, MINERS, [0, 1, 2], emission_value_usd=emission_value_usd
        )


@pytest.mark.parametrize(
    ("beneficiaries", "hotkey_to_uid", "expected", "donated"),
    [
        pytest.param(
            {"miner-b": "miner-a"},
            MINERS,
            # Miner b keeps its $20 floor weight and gives its 0.64 surplus away.
            approx([0.0, 0.34 + 0.64, 0.02]),
            approx([0.0, 0.0, 0.64]),
            id="donor-keeps-floor-weight-and-gives-surplus",
        ),
        pytest.param(
            {"miner-b": "owner"},
            {**MINERS, "owner": 0},
            approx([0.64, 0.34, 0.02]),
            approx([0.0, 0.0, 0.64]),
            id="burn-uid-can-receive-surplus",
        ),
        pytest.param(
            {"miner-a": "miner-b", "miner-b": "miner-a"},
            MINERS,
            # Each donation moves only the donor's own surplus, never one received.
            approx([0.0, 0.18 + 0.64, 0.02 + 0.16]),
            approx([0.0, 0.16, 0.64]),
            id="cycle-moves-each-surplus-once",
        ),
        pytest.param(
            {"miner-b": "unregistered"},
            MINERS,
            approx([0.0, 0.34, 0.66]),
            approx([0.0, 0.0, 0.0]),
            id="unregistered-beneficiary-leaves-surplus-with-donor",
        ),
        pytest.param(
            {"miner-a": "miner-a"},
            MINERS,
            approx([0.0, 0.34, 0.66]),
            approx([0.0, 0.0, 0.0]),
            id="self-designation-is-ignored",
        ),
    ],
)
def test_surplus_beneficiaries_receive_donor_surplus(
    beneficiaries: dict[str, str],
    hotkey_to_uid: dict[str, int],
    expected: object,
    donated: object,
) -> None:
    allocation = allocate_floor_first_weights(
        FLOOR_VS_SCORE,
        hotkey_to_uid,
        [0, 1, 2],
        emission_value_usd=1000.0,
        surplus_beneficiaries=beneficiaries,
    )

    assert allocation.weights.tolist() == expected
    assert allocation.donated_weights.tolist() == donated
    assert allocation.weights.sum() == approx(1.0)


@pytest.mark.parametrize("emission_value_usd", [None, 100.0])
def test_nothing_is_donated_without_a_surplus(emission_value_usd: float | None) -> None:
    allocation = allocate_floor_first_weights(
        FLOOR_VS_SCORE,
        MINERS,
        [0, 1, 2],
        emission_value_usd=emission_value_usd,
        surplus_beneficiaries={"miner-b": "miner-a"},
    )

    assert allocation.weights.tolist() == approx([0.0, 0.9, 0.1])
    assert not allocation.donated_weights.any()
