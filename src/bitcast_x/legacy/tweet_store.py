"""Frozen cumulative legacy tweet-store compatibility."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from diskcache import Cache  # type: ignore[import-untyped]

from bitcast_x.campaigns import CampaignFeed, CampaignRecord, ecosystem_map_at
from bitcast_x.errors import ProtocolError
from bitcast_x.x_provider import EngagementFetch, Tweet

_METRICS = (
    "favorite_count",
    "retweet_count",
    "reply_count",
    "quote_count",
    "bookmark_count",
    "views_count",
)
_NEW_TWEET_REFRESH = 1
_RECENT_TWEET_REFRESH = 4
_OLD_TWEET_REFRESH = 24


class LegacyTweetStore:
    """Continue v1/v2's permanent diskcache store in place after cutover."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: Cache | None = None

    def validate(self) -> None:
        """Require an imported, internally healthy diskcache database."""

        database = self.path / "cache.db"
        if not database.is_file():
            raise ProtocolError(f"legacy tweet store is missing: {database}")
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                result = connection.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError as exc:
            raise ProtocolError(f"legacy tweet store is unreadable: {database}") from exc
        if result != ("ok",):
            raise ProtocolError(f"legacy tweet store integrity check failed: {result!r}")
        self._open()

    def close(self) -> None:
        """Flush and close the diskcache store."""

        if self._cache is not None:
            self._cache.close()
            self._cache = None

    def merge(self, tweets: tuple[Tweet, ...]) -> None:
        """Permanently merge tweets and any exact quote evidence they carry."""

        cache = self._open()
        now = datetime.now(UTC).isoformat()
        for tweet in tweets:
            key = f"tweet:{tweet.tweet_id}"
            existing = cache.get(key)
            payload = tweet.model_dump(mode="json")
            payload["created_at"] = tweet.created_at.strftime("%a %b %d %H:%M:%S %z %Y")
            if not isinstance(existing, dict):
                cache.set(key, {**payload, "first_seen": now, "last_updated": now})
            else:
                for metric in _METRICS:
                    existing[metric] = max(
                        self._integer(existing.get(metric)),
                        self._integer(payload.get(metric)),
                    )
                # Old stores do not contain immutable author IDs. Preserve every
                # frozen field, while filling fields that v3 newly knows.
                for field, value in payload.items():
                    existing.setdefault(field, value)
                existing["last_updated"] = now
                cache.set(key, existing)
            self._merge_observed_quote(
                cache,
                quoted_tweet_id=tweet.quoted_tweet_id,
                quote_tweet_id=tweet.tweet_id,
                author=tweet.author,
                first_seen=now,
            )

    def recover_observed_quote_engagements(self) -> int:
        """Backfill exact quote relationships already present in the cumulative store.

        Desearch can return a successful empty result for its ``quoted_tweet_id``
        search operator. Campaign search and fast-track intake still persist the
        quote post itself, including its immutable target ID. Recovering from that
        local evidence makes an upgrade self-healing without trusting aggregate
        quote counts or broad search matches.
        """

        cache = self._open()
        now = datetime.now(UTC).isoformat()
        recovered = 0
        for key in cache.iterkeys():
            if not str(key).startswith("tweet:"):
                continue
            record = cache.get(key)
            if not isinstance(record, dict):
                continue
            recovered += self._merge_observed_quote(
                cache,
                quoted_tweet_id=record.get("quoted_tweet_id"),
                quote_tweet_id=record.get("tweet_id"),
                author=record.get("author"),
                first_seen=record.get("first_seen") or now,
            )
        return recovered

    def campaign_tweets(
        self,
        feed: CampaignFeed,
        campaign: CampaignRecord,
    ) -> tuple[Tweet, ...]:
        """Return the complete cumulative v2 candidate set for one campaign."""

        output: list[Tweet] = []
        for key in self._open().iterkeys():
            if not str(key).startswith("tweet:"):
                continue
            record = self._open().get(key)
            if not isinstance(record, dict):
                continue
            tweet = self._to_tweet(record, feed, campaign)
            if tweet is None or not campaign.opens_at <= tweet.created_at <= campaign.closes_at:
                continue
            # Match v1's TweetFilter: replies and pure retweets are not
            # campaign posts, even when their text contains the campaign tag.
            if tweet.in_reply_to_status_id is not None or tweet.text.startswith("RT @"):
                continue
            if campaign.tag is not None and campaign.tag.casefold() not in tweet.text.casefold():
                continue
            if (
                campaign.quoted_tweet_id is not None
                and tweet.quoted_tweet_id != campaign.quoted_tweet_id
            ):
                continue
            if campaign.inclusion_keywords and not any(
                keyword.casefold() in tweet.text.casefold()
                for keyword in campaign.inclusion_keywords
            ):
                continue
            output.append(tweet)
        return tuple(sorted(output, key=lambda item: int(item.tweet_id)))

    def merge_engagements(self, tweet_id: str, fresh: EngagementFetch) -> EngagementFetch:
        """Preserve the cumulative retweeter/quoter set frozen by v1/v2."""

        cache = self._open()
        key = f"engagements:{tweet_id}"
        record = cache.get(key)
        if not isinstance(record, dict):
            record = {"tweet_id": tweet_id, "retweeters": {}, "quoters": {}}
        retweeters = record.get("retweeters", {})
        quoters = record.get("quoters", {})
        if not isinstance(retweeters, dict) or not isinstance(quoters, dict):
            return EngagementFetch(engagements={}, provider_available=False)
        cached_known = cache.get(key) is not None
        engagements = {
            str(username).casefold(): "retweet" for username in retweeters if str(username)
        }
        engagements.update(
            {str(username).casefold(): "quote" for username in quoters if str(username)}
        )
        if fresh.provider_available:
            now = datetime.now(UTC)
            for username, kind in fresh.engagements.items():
                target = quoters if kind == "quote" else retweeters
                target.setdefault(username.casefold(), {"first_seen": now.isoformat()})
            record["retweeters"] = retweeters
            record["quoters"] = quoters
            record["last_updated"] = now.isoformat()
            cache.set(key, record)
            cache.set(f"engagement_fetch:{tweet_id}", now.isoformat())
            engagements = {username.casefold(): "retweet" for username in retweeters}
            engagements.update({username.casefold(): "quote" for username in quoters})
        return EngagementFetch(
            engagements=engagements,
            provider_available=fresh.provider_available or cached_known,
        )

    def cached_engagements(self, tweet_id: str) -> EngagementFetch:
        """Return cumulative evidence without contacting the provider."""

        return self.merge_engagements(
            tweet_id,
            EngagementFetch(engagements={}, provider_available=False),
        )

    def engagement_refresh_due(self, tweet: Tweet, *, now: datetime | None = None) -> bool:
        """Apply v2's 1h/4h/24h engagement refresh schedule."""

        if not self.cached_engagements(tweet.tweet_id).provider_available:
            return True
        last_fetch = self._timestamp(self._open().get(f"engagement_fetch:{tweet.tweet_id}"))
        if last_fetch is None:
            return True
        current = now or datetime.now(UTC)
        age_hours = max((current - tweet.created_at).total_seconds() / 3600, 0)
        if age_hours < 1:
            interval_hours = _NEW_TWEET_REFRESH
        elif age_hours < 24:
            interval_hours = _RECENT_TWEET_REFRESH
        else:
            interval_hours = _OLD_TWEET_REFRESH
        return (current - last_fetch).total_seconds() >= interval_hours * 3600

    def _to_tweet(
        self,
        record: dict[str, Any],
        feed: CampaignFeed,
        campaign: CampaignRecord,
    ) -> Tweet | None:
        created_at = self._timestamp(record.get("created_at"))
        author = str(record.get("author") or "").casefold()
        if created_at is None or not author:
            return None
        ecosystems = tuple(
            ecosystem
            for pool in campaign.pools
            if (ecosystem := ecosystem_map_at(feed, pool, created_at)) is not None
        )
        account = next(
            (
                item
                for ecosystem in ecosystems
                for item in ecosystem.accounts
                if item.username.casefold() == author
            ),
            None,
        )
        author_x_id = str(record.get("author_x_id") or (account.x_id if account else ""))
        try:
            return Tweet.model_validate(
                {field: record.get(field) for field in Tweet.model_fields if field in record}
                | {
                    "tweet_id": str(record.get("tweet_id") or ""),
                    "author_x_id": author_x_id,
                    "created_at": created_at,
                    "author": author,
                    "text": str(record.get("text") or ""),
                }
            )
        except ValueError:
            return None

    def _open(self) -> Cache:
        if self._cache is None:
            self._cache = Cache(
                directory=str(self.path),
                sqlite_journal_mode="truncate",
                sqlite_mmap_size=0,
                disk_pickle_protocol=4,
            )
        return self._cache

    @staticmethod
    def _merge_observed_quote(
        cache: Cache,
        *,
        quoted_tweet_id: Any,
        quote_tweet_id: Any,
        author: Any,
        first_seen: Any,
    ) -> int:
        target_id = str(quoted_tweet_id or "")
        evidence_id = str(quote_tweet_id or "")
        username = str(author or "").casefold()
        if not target_id.isdigit() or not evidence_id.isdigit() or not username:
            return 0

        key = f"engagements:{target_id}"
        record = cache.get(key)
        if not isinstance(record, dict):
            record = {"tweet_id": target_id, "retweeters": {}, "quoters": {}}
        retweeters = record.get("retweeters", {})
        quoters = record.get("quoters", {})
        if not isinstance(retweeters, dict) or not isinstance(quoters, dict):
            return 0
        if any(str(existing).casefold() == username for existing in quoters):
            return 0

        quoters[username] = {
            "quote_tweet_id": evidence_id,
            "first_seen": str(first_seen),
        }
        record["retweeters"] = retweeters
        record["quoters"] = quoters
        record["last_updated"] = datetime.now(UTC).isoformat()
        cache.set(key, record)
        return 1

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (
                    parsed.astimezone(UTC)
                    if parsed.tzinfo is not None
                    else parsed.replace(tzinfo=UTC)
                )
            except ValueError:
                return None
