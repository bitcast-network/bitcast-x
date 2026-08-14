"""Legacy reward snapshot cutover and replay tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.legacy import LegacyRewardSnapshot, LegacySnapshotStore, LegacyTweetReward


def _snapshot(created: datetime, total: float = 70) -> LegacyRewardSnapshot:
    return LegacyRewardSnapshot(
        brief_id="legacy-1",
        pool_name="tao",
        created_at=created,
        tweet_rewards=(
            LegacyTweetReward(tweet_id="123", author="alice", uid=114, total_usd=total),
            LegacyTweetReward(tweet_id="456", author="bob", uid=114, total_usd=total),
        ),
    )


def test_oldest_filename_snapshot_is_canonical_and_replayed(tmp_path: Path) -> None:
    store = LegacySnapshotStore(tmp_path)
    newer = _snapshot(datetime(2026, 8, 8, 10, tzinfo=UTC), 140)
    older = _snapshot(datetime(2026, 8, 7, 10, tzinfo=UTC), 70)
    store.write_new(newer)
    store.write_new(older)

    replay = store.replay("tao", "legacy-1")

    assert replay is not None
    assert replay.snapshot.created_at == older.created_at
    assert replay.daily_usd_by_uid == {114: 20.0}
    assert replay.committed_tweet_ids == {"123", "456"}


def test_snapshot_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "tao"
    directory.mkdir()
    payload = _snapshot(datetime(2026, 8, 7, tzinfo=UTC)).model_dump(mode="json")
    payload["brief_id"] = "other"
    (directory / "legacy-1_2026.08.07_00.00.00.json").write_text(json.dumps(payload))

    with pytest.raises(ProtocolError, match="identity mismatch"):
        LegacySnapshotStore(tmp_path).load("tao", "legacy-1")


def test_missing_snapshot_is_not_an_error(tmp_path: Path) -> None:
    assert LegacySnapshotStore(tmp_path).replay("tao", "missing") is None
