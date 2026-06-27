"""Integration tests for social discovery v2: relevance gradient + AI out-link sink."""

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


def _make_client(mock_tweets, counts=None):
    """Build a mock TwitterClient. counts maps username -> (relevant, total)."""
    client = mock.Mock()

    def fetch(username, fetch_days=30, skip_if_cache_fresh=False):
        return {"tweets": mock_tweets.get(username, []), "user_info": {"followers_count": 1000}}

    client.fetch_user_tweets.side_effect = fetch
    client.check_user_relevance.return_value = True

    def relevance_counts(username, keywords, lang=None, skip_if_cache_fresh=False):
        return counts.get(username, (0, 0)) if counts else (1, 1)

    client.compute_user_relevance_counts.side_effect = relevance_counts
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


class TestRelevanceGradient:
    def test_gate_excludes_low_ratio_account(self):
        # c is high-volume but only 0.1% on-topic -> excluded at 5% gate.
        counts = {"a": (10, 10), "b": (10, 10), "c": (1, 1000)}
        client = _make_client(GRAPH_TWEETS, counts=counts)
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)

        scores, *_ = analyzer.analyze_network(
            ["a", "b", "c"], ["hi"], relevance_gradient=True, min_relevance_ratio=0.05,
        )

        assert "c" not in scores
        assert "a" in scores and "b" in scores
        # Relevance scores recorded for every checked account, including excluded c.
        assert set(analyzer.relevance_scores) >= {"a", "b", "c"}
        assert analyzer.relevance_scores["c"] < 0.05

    def test_low_min_ratio_includes_everyone(self):
        counts = {"a": (10, 10), "b": (10, 10), "c": (1, 1000)}
        client = _make_client(GRAPH_TWEETS, counts=counts)
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)
        scores, *_ = analyzer.analyze_network(
            ["a", "b", "c"], ["hi"], relevance_gradient=True, min_relevance_ratio=0.0,
        )
        assert {"a", "b", "c"} <= set(scores)

    def test_uses_counts_not_bool_relevance(self):
        counts = {"a": (10, 10), "b": (10, 10), "c": (10, 10)}
        client = _make_client(GRAPH_TWEETS, counts=counts)
        analyzer = TwitterNetworkAnalyzer(client, max_workers=1)
        analyzer.analyze_network(
            ["a", "b", "c"], ["hi"], relevance_gradient=True, min_relevance_ratio=0.0,
        )
        client.compute_user_relevance_counts.assert_called()
        client.check_user_relevance.assert_not_called()
