"""Deterministic normalized X evidence for tests that must not call a provider."""

from collections.abc import Mapping

from bitcast_x.x_provider import EngagementFetch, TweetFetch, TweetSearchFetch


class FixtureXProvider:
    """Serve explicit normalized evidence and fail when a scenario omits a fixture."""

    def __init__(
        self,
        *,
        tweets: Mapping[str, TweetFetch],
        engagements: Mapping[str, EngagementFetch] | None = None,
        searches: Mapping[str, TweetSearchFetch] | None = None,
        replies: Mapping[str, TweetSearchFetch] | None = None,
    ) -> None:
        self._tweets = dict(tweets)
        self._engagements = dict(engagements or {})
        self._searches = dict(searches or {})
        self._replies = dict(replies or {})

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        return self._tweets[tweet_id]

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        return self._engagements[tweet_id]

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        del count
        return self._searches[query]

    async def fetch_replies(self, tweet_id: str, *, count: int = 100) -> TweetSearchFetch:
        del count
        return self._replies[tweet_id]

    async def close(self) -> None:
        return None
