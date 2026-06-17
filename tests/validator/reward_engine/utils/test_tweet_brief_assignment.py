"""Tests for greedy one-to-one tweet→brief assignment."""

from bitcast.validator.reward_engine.utils import assign_tweets_to_briefs


def _tweet(tweet_id, author='u', score=0.5):
    return {'tweet_id': tweet_id, 'author': author, 'score': score}


class TestAssignTweetsToBriefs:
    """Behaviour of assign_tweets_to_briefs()."""

    def test_shared_tweet_assigned_to_exactly_one_brief(self):
        """A tweet passing two briefs is rewarded under only one of them."""
        high = {'brief_id': 'A', 'daily_budget': 1000, 'max_tweets': None,
                'tweets': [_tweet('t1')]}
        low = {'brief_id': 'B', 'daily_budget': 10, 'max_tweets': None,
               'tweets': [_tweet('t1')]}

        result = assign_tweets_to_briefs([high, low])

        assert result['A'] | result['B'] == {'t1'}
        assert not (result['A'] & result['B'])  # never in both
        assert result['A'] == {'t1'}  # higher-payout brief wins

    def test_two_tweets_split_across_two_capped_briefs(self):
        """The motivating case: one tweet lands in each max_tweets=1 brief.

        An account posts two tweets that both match two single-slot briefs. The
        best outcome fills both slots rather than stacking both tweets on the
        higher-budget brief (which would waste the other slot).
        """
        a = {'brief_id': 'A', 'daily_budget': 1000, 'max_tweets': 1,
             'tweets': [_tweet('t1', score=0.6), _tweet('t2', score=0.4)]}
        b = {'brief_id': 'B', 'daily_budget': 500, 'max_tweets': 1,
             'tweets': [_tweet('t1', score=0.6), _tweet('t2', score=0.4)]}

        result = assign_tweets_to_briefs([a, b])

        assert len(result['A']) == 1 and len(result['B']) == 1
        assert result['A'] | result['B'] == {'t1', 't2'}
        # Higher-score tweet routed to the higher-budget brief.
        assert result['A'] == {'t1'} and result['B'] == {'t2'}

    def test_max_tweets_cap_is_per_account(self):
        """The cap limits tweets per account within a brief, not in total."""
        brief = {'brief_id': 'C', 'daily_budget': 1000, 'max_tweets': 1,
                 'tweets': [_tweet('t1', author='u1', score=0.9),
                            _tweet('t2', author='u1', score=0.8),
                            _tweet('t3', author='u2', score=0.7)]}

        result = assign_tweets_to_briefs([brief])

        # u1 keeps only their top tweet; u2 keeps theirs.
        assert result['C'] == {'t1', 't3'}

    def test_no_cap_keeps_all_tweets(self):
        """max_tweets of None or 0 imposes no per-account limit."""
        for cap in (None, 0):
            brief = {'brief_id': 'C', 'daily_budget': 1000, 'max_tweets': cap,
                     'tweets': [_tweet('t1', score=0.9), _tweet('t2', score=0.8)]}
            assert assign_tweets_to_briefs([brief])['C'] == {'t1', 't2'}

    def test_committed_tweets_are_not_reassigned(self):
        """Tweets frozen in an existing snapshot are excluded from assignment."""
        brief = {'brief_id': 'A', 'daily_budget': 1000, 'max_tweets': None,
                 'tweets': [_tweet('t1'), _tweet('t2')]}

        result = assign_tweets_to_briefs([brief], committed_tweet_ids={'t1'})

        assert result['A'] == {'t2'}

    def test_every_brief_present_in_result(self):
        """Briefs that win no tweets still appear with an empty set."""
        a = {'brief_id': 'A', 'daily_budget': 1000, 'max_tweets': None,
             'tweets': [_tweet('t1')]}
        b = {'brief_id': 'B', 'daily_budget': 10, 'max_tweets': None,
             'tweets': [_tweet('t1')]}

        result = assign_tweets_to_briefs([a, b])

        assert set(result.keys()) == {'A', 'B'}
        assert result['B'] == set()

    def test_tweets_without_id_are_ignored(self):
        """Tweets missing a tweet_id can't be assigned and are skipped."""
        brief = {'brief_id': 'A', 'daily_budget': 1000, 'max_tweets': None,
                 'tweets': [{'author': 'u', 'score': 0.5}, _tweet('t2')]}

        result = assign_tweets_to_briefs([brief])

        assert result['A'] == {'t2'}

    def test_empty_input(self):
        assert assign_tweets_to_briefs([]) == {}
