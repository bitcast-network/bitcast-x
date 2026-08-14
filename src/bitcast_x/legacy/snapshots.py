"""Frozen v2 legacy reward-snapshot import and deterministic replay."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from bitcast_x.errors import ProtocolError

SNAPSHOT_TIMESTAMP_FORMAT = "%Y.%m.%d_%H.%M.%S"


class LegacyTweetReward(BaseModel):
    """Required frozen fields plus the original tweet evidence."""

    model_config = ConfigDict(extra="allow", frozen=True)

    tweet_id: str = Field(pattern=r"^[0-9]+$")
    author: str = Field(min_length=1)
    uid: int = Field(ge=0)
    total_usd: float = Field(ge=0)
    miner_hotkey: str | None = None
    creator_x_id: str | None = None
    score: float = 0.0
    text: str = ""
    performance_bonus_pct: float = 0.0
    performance_bonus_breakdown: dict[str, Any] | None = None
    featured_tweet_bonus: bool = False
    favorite_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    bookmark_count: int = 0
    views_count: int = 0
    retweets: tuple[str, ...] = ()
    quotes: tuple[str, ...] = ()
    created_at: str = ""
    lang: str = "und"
    author_influence: float | None = None
    baseline_score: float | None = None
    score_breakdown: tuple[dict[str, Any], ...] = ()


class LegacyRewardSnapshot(BaseModel):
    """Canonical first-emission snapshot produced by v2."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    brief_id: str = Field(min_length=1)
    pool_name: str = Field(min_length=1)
    created_at: datetime
    tweet_rewards: tuple[LegacyTweetReward, ...]


@dataclass(frozen=True)
class SnapshotReplay:
    """One snapshot's stable daily payout and committed tweet IDs."""

    snapshot: LegacyRewardSnapshot
    daily_usd_by_uid: dict[int, float]
    committed_tweet_ids: frozenset[str]


class LegacySnapshotStore:
    """Load v2's oldest filename-timestamped snapshot without changing it."""

    def __init__(self, root: Path, *, emissions_period_days: int = 7) -> None:
        if emissions_period_days <= 0:
            raise ValueError("emissions_period_days must be positive")
        self.root = root
        self.emissions_period_days = emissions_period_days

    def load(self, pool_name: str, brief_id: str) -> LegacyRewardSnapshot | None:
        """Return the canonical snapshot, or null when the campaign has never frozen."""

        directory = self.root / pool_name
        candidates = list(directory.glob(f"{brief_id}_*.json")) if directory.is_dir() else []
        if not candidates:
            return None
        canonical = min(candidates, key=lambda path: self._sort_key(brief_id, path))
        try:
            payload = TypeAdapter(dict[str, Any]).validate_json(canonical.read_bytes())
            snapshot = LegacyRewardSnapshot.model_validate(payload)
        except (OSError, ValueError) as exc:
            raise ProtocolError(f"invalid legacy reward snapshot: {canonical}") from exc
        if snapshot.brief_id != brief_id or snapshot.pool_name != pool_name:
            raise ProtocolError(f"legacy reward snapshot identity mismatch: {canonical}")
        return snapshot

    def replay(self, pool_name: str, brief_id: str) -> SnapshotReplay | None:
        """Reproduce v2's `total_usd / 7` per-UID daily replay."""

        snapshot = self.load(pool_name, brief_id)
        if snapshot is None:
            return None
        totals: dict[int, float] = {}
        for reward in snapshot.tweet_rewards:
            totals[reward.uid] = totals.get(reward.uid, 0.0) + reward.total_usd
        return SnapshotReplay(
            snapshot=snapshot,
            daily_usd_by_uid={
                uid: total / self.emissions_period_days for uid, total in totals.items()
            },
            committed_tweet_ids=frozenset(item.tweet_id for item in snapshot.tweet_rewards),
        )

    @staticmethod
    def _sort_key(brief_id: str, path: Path) -> tuple[int, float]:
        suffix = path.stem[len(brief_id) + 1 :] if path.stem.startswith(f"{brief_id}_") else ""
        try:
            parsed = datetime.strptime(suffix, SNAPSHOT_TIMESTAMP_FORMAT).replace(tzinfo=UTC)
            return (0, parsed.timestamp())
        except ValueError:
            return (1, path.stat().st_mtime)

    def write_new(self, snapshot: LegacyRewardSnapshot) -> Path:
        """Atomically persist a new first-emission snapshot in the frozen format."""

        directory = self.root / snapshot.pool_name
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = snapshot.created_at.astimezone(UTC).strftime(SNAPSHOT_TIMESTAMP_FORMAT)
        target = directory / f"{snapshot.brief_id}_{timestamp}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        temporary.replace(target)
        return target
