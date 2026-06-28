"""Integration tests for social discovery v2: AI out-link sink."""

import unittest.mock as mock

import pytest

from bitcast.validator.social_discovery.social_discovery import (
    TwitterNetworkAnalyzer,
    AI_SINK_NODE,
)


def _tweet(text, tagged=None):
    return {
        "text": text,
        "tagged_accounts": tagged or [],
        "retweeted_user": None,
        "quoted_user": None,
        "in_reply_to_status_id": None,
    }


def _make_client(mock_tweets, affiliates=None):
    """Build a mock TwitterClient.

    affiliates: optional {username: affiliate_username} mapping injected into user_info.
    """
    affiliates = affiliates or {}
    client = mock.Mock()

    def fetch(username, fetch_days=30, skip_if_cache_fresh=False):
        user_info = {"followers_count": 1000}
        if username in affiliates:
            user_info["affiliate_username"] = affiliates[username]
        return {"tweets": mock_tweets.get(username, []), "user_info": user_info}

    client.fetch_user_tweets.side_effect = fetch
    client.check_user_relevance.return_value = True
    return client


# a <-> b mutual, c -> b. b is the hub.
GRAPH_TWEETS = {
    "a": [_tweet("hi @b", ["b"])],
    "b": [_tweet("hi @a", ["a"])],
    "c": [_tweet("hi @b", ["b"])],
}


class TestAiSink:
    def test_sink_dampens_outgoing_influence_and_is_hidden(self):
        client = _make_client(GRAPH_TWEETS)
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)

        base, *_ = analyzer.analyze_network(["a", "b", "c"], ["hi"])
        damp, _m, _r, usernames, _ui, _f = analyzer.analyze_network(
            ["a", "b", "c"], ["hi"], ai_dampening=True, ai_scores={"a": 0.8},
        )

        # Sink node never leaks into outputs.
        assert AI_SINK_NODE not in damp
        assert AI_SINK_NODE not in usernames
        # b receives a's endorsement; dampening a's out-links should lower b.
        assert damp["b"] < base["b"]
        # Recorded ai_scores reflect the dampened account.
        assert analyzer.ai_scores.get("a") == 0.8

    def test_zero_ai_scores_is_noop(self):
        client = _make_client(GRAPH_TWEETS)
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)
        base, *_ = analyzer.analyze_network(["a", "b", "c"], ["hi"])
        same, *_ = analyzer.analyze_network(
            ["a", "b", "c"], ["hi"], ai_dampening=True, ai_scores={"a": 0.0},
        )
        assert same["b"] == pytest.approx(base["b"], rel=1e-9)


# Two structurally symmetric, disjoint mutual pairs:
#   aff <-> x   and   plain <-> y
# Without a boost the two pairs score identically. "aff" is a badged affiliate
# of the shortlisted handle "brand".
AFFILIATE_GRAPH_TWEETS = {
    "aff": [_tweet("hi @x", ["x"])],
    "x": [_tweet("hi @aff", ["aff"])],
    "plain": [_tweet("hi @y", ["y"])],
    "y": [_tweet("hi @plain", ["plain"])],
}
AFFILIATE_USERS = ["aff", "x", "plain", "y"]


class TestAffiliateBoost:
    def test_affiliate_of_shortlisted_outranks_symmetric_nonaffiliate(self):
        client = _make_client(AFFILIATE_GRAPH_TWEETS, affiliates={"aff": "brand"})
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)

        base, *_ = analyzer.analyze_network(AFFILIATE_USERS, ["hi"])
        # Baseline: the two mirror pairs are identical.
        assert base["aff"] == pytest.approx(base["plain"], rel=1e-9)
        assert base["x"] == pytest.approx(base["y"], rel=1e-9)

        boosted, *_ = analyzer.analyze_network(
            AFFILIATE_USERS, ["hi"], shortlisted_accounts={"brand"},
        )
        # The affiliate is lifted above its baseline and above the mirror account.
        assert boosted["aff"] > base["aff"]
        assert boosted["aff"] > boosted["plain"]
        # Propagation: the account the affiliate endorses is lifted above the
        # account the non-affiliate endorses.
        assert boosted["x"] > boosted["y"]

    def test_empty_shortlist_is_noop(self):
        client = _make_client(AFFILIATE_GRAPH_TWEETS, affiliates={"aff": "brand"})
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)
        base, *_ = analyzer.analyze_network(AFFILIATE_USERS, ["hi"])
        for shortlist in (None, set()):
            same, *_ = analyzer.analyze_network(
                AFFILIATE_USERS, ["hi"], shortlisted_accounts=shortlist,
            )
            for user in AFFILIATE_USERS:
                assert same[user] == pytest.approx(base[user], rel=1e-9)

    def test_shortlist_with_no_matching_affiliate_is_noop(self):
        client = _make_client(AFFILIATE_GRAPH_TWEETS, affiliates={"aff": "brand"})
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)
        base, *_ = analyzer.analyze_network(AFFILIATE_USERS, ["hi"])
        # "aff" is the handle, but no account is affiliated TO "aff"; matching is on
        # affiliate_username, so this shortlist boosts nobody.
        same, *_ = analyzer.analyze_network(
            AFFILIATE_USERS, ["hi"], shortlisted_accounts={"aff"},
        )
        for user in AFFILIATE_USERS:
            assert same[user] == pytest.approx(base[user], rel=1e-9)


class TestAiCheckCap:
    def test_selects_top_n_by_interaction_weight(self):
        analyzer = TwitterNetworkAnalyzer(mock.Mock(), max_workers=1)
        all_users = {"hub", "mid", "low"}
        # hub gets the most weight, low the least.
        iw = {("a", "hub"): 5.0, ("b", "hub"): 3.0, ("a", "mid"): 2.0, ("a", "low"): 1.0}
        # add 'a','b' to the user set so their out-weight counts too
        all_users |= {"a", "b"}
        selected = analyzer._select_ai_check_candidates(all_users, iw, max_checks=2)
        assert "hub" in selected and len(selected) == 2

    def test_deterministic_tiebreak_by_username(self):
        analyzer = TwitterNetworkAnalyzer(mock.Mock(), max_workers=1)
        all_users = {"charlie", "alice", "bob"}
        # Symmetric triangle: every account has identical total (in+out) weight.
        iw = {
            ("alice", "bob"): 1.0, ("bob", "alice"): 1.0,
            ("bob", "charlie"): 1.0, ("charlie", "bob"): 1.0,
            ("charlie", "alice"): 1.0, ("alice", "charlie"): 1.0,
        }
        a = analyzer._select_ai_check_candidates(all_users, iw, max_checks=2)
        b = analyzer._select_ai_check_candidates(all_users, iw, max_checks=2)
        assert a == b
        # All tied -> tie broken by username ascending: alice, bob win over charlie.
        assert a == {"alice", "bob"}

    def test_unlimited_when_zero_or_below_count(self):
        analyzer = TwitterNetworkAnalyzer(mock.Mock(), max_workers=1)
        all_users = {"a", "b", "c"}
        assert analyzer._select_ai_check_candidates(all_users, {}, 0) == all_users
        assert analyzer._select_ai_check_candidates(all_users, {}, 10) == all_users

    def test_cap_limits_accounts_actually_scored(self):
        # Only the most-connected account (b, the hub) should be AI-checked when cap=1.
        client = _make_client(GRAPH_TWEETS)
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)
        with mock.patch(
            "bitcast.validator.social_discovery.social_discovery.AI_MAX_ACCOUNTS_CHECKED", 1
        ):
            scored = {}

            def fake_compute(usernames):
                # Record which accounts were passed for scoring.
                scored["set"] = set(usernames)
                return {u: 0.0 for u in usernames}

            analyzer._compute_ai_scores_for = fake_compute
            analyzer.analyze_network(["a", "b", "c"], ["hi"], ai_dampening=True)

        assert len(scored["set"]) == 1
        assert "b" in scored["set"]  # b is the hub (a->b, c->b, b->a)
