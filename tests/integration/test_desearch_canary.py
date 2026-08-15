"""Opt-in read-only canary for the live Desearch normalization boundary."""

import os

import pytest

from bitcast_x.x_provider import DesearchProvider

pytestmark = pytest.mark.skipif(
    os.getenv("BITCAST_X_RUN_DESEARCH_CANARY") != "1",
    reason="set BITCAST_X_RUN_DESEARCH_CANARY=1 with a Desearch API key",
)

DEFAULT_TWEET_ID = "2070290556647883174"


@pytest.mark.asyncio
async def test_desearch_normalizes_stable_historical_tweet_evidence() -> None:
    api_key = os.getenv("DESEARCH_API_KEY", "").strip()
    if not api_key:
        pytest.fail("DESEARCH_API_KEY is required when the Desearch canary is enabled")
    tweet_id = os.getenv("BITCAST_X_CANARY_TWEET_ID", DEFAULT_TWEET_ID)
    provider = DesearchProvider(api_key, timeout=15, attempts=3, retry_delay=0.25)
    try:
        evidence = await provider.fetch_tweet_by_id(tweet_id)
        engagements = await provider.fetch_engagements(tweet_id)
    finally:
        await provider.close()

    assert evidence.provider_available is True
    assert evidence.tweet is not None
    assert evidence.tweet.tweet_id == tweet_id
    assert evidence.tweet.author_x_id.isdigit()
    assert evidence.tweet.author
    assert evidence.tweet.text.strip()
    assert evidence.tweet.created_at.utcoffset() is not None
    assert engagements.provider_available is True
    assert all(
        username and kind in {"quote", "retweet"}
        for username, kind in engagements.engagements.items()
    )
