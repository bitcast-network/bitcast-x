"""Tests for time-pinned influence lookup (get_influence_at_time)."""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import bitcast.validator.tweet_scoring.social_map_loader as sml
from bitcast.validator.tweet_scoring.social_map_loader import (
    get_influence_at_time,
    clear_influence_cache,
    _find_map_for_timestamp,
    parse_social_map_filename,
    _accounts_dict_from_map,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_map(path: Path, accounts: dict):
    """Write a minimal social map JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'accounts': accounts}))
    return path


def _make_patched_find(maps_dir: Path):
    """Return a replacement for _find_map_for_timestamp that uses *maps_dir*."""

    def _patched(pool_name, timestamp):
        all_maps = [
            f for f in maps_dir.glob("*.json")
            if not f.name.endswith(('_adjacency.json', '_metadata.json'))
            and not f.name.startswith('recursive_summary_')
        ]
        if not all_maps:
            return None

        maps_with_times = sorted(
            [(f, ts) for f in all_maps if (ts := parse_social_map_filename(f.name))],
            key=lambda x: x[1]
        )
        if not maps_with_times:
            return None

        for i, (map_file, map_time) in enumerate(maps_with_times):
            next_map_time = maps_with_times[i + 1][1] if i + 1 < len(maps_with_times) else datetime.max.replace(tzinfo=timezone.utc)
            if map_time <= timestamp < next_map_time:
                return (map_file, map_time)

        return maps_with_times[0]

    return _patched


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure cache is clean before and after each test."""
    clear_influence_cache()
    yield
    clear_influence_cache()


@pytest.fixture
def two_maps(tmp_path, monkeypatch):
    """Create two social maps with different scores and patch _find_map_for_timestamp."""
    old_accounts = {
        'alice': {'score': 0.50},
        'bob': {'score': 0.30},
        'carol': {'score': 0.20},
    }
    new_accounts = {
        'alice': {'score': 0.80},   # score changed
        'bob': {'score': 0.10},      # score changed
        'dave': {'score': 0.40},     # carol dropped, dave added
    }

    _write_map(tmp_path / "2025.11.01_00.00.00.json", old_accounts)
    _write_map(tmp_path / "2025.11.15_00.00.00.json", new_accounts)

    # Patch _find_map_for_timestamp to use our tmp_path
    monkeypatch.setattr(sml, "_find_map_for_timestamp", _make_patched_find(tmp_path))

    return {
        'dir': tmp_path,
        'old_accounts': old_accounts,
        'new_accounts': new_accounts,
        'old_time': datetime(2025, 11, 1, tzinfo=timezone.utc),
        'new_time': datetime(2025, 11, 15, tzinfo=timezone.utc),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFindMapForTimestamp:
    """Test _find_map_for_timestamp resolution logic."""

    def test_finds_map_containing_timestamp(self, two_maps):
        """Should return the map whose active period contains the timestamp."""
        ts = datetime(2025, 11, 10, tzinfo=timezone.utc)
        result = sml._find_map_for_timestamp('test', ts)
        assert result is not None
        map_file, _ = result
        assert '2025.11.01' in map_file.name

    def test_finds_latest_map_for_recent_timestamp(self, two_maps):
        """Should return the latest map for timestamps after all maps."""
        ts = datetime(2025, 12, 1, tzinfo=timezone.utc)
        result = sml._find_map_for_timestamp('test', ts)
        assert result is not None
        map_file, _ = result
        assert '2025.11.15' in map_file.name

    def test_finds_earliest_map_for_old_timestamp(self, two_maps):
        """Should return the earliest map for timestamps before all maps."""
        ts = datetime(2025, 10, 1, tzinfo=timezone.utc)
        result = sml._find_map_for_timestamp('test', ts)
        assert result is not None
        map_file, _ = result
        assert '2025.11.01' in map_file.name

    def test_returns_none_for_nonexistent_pool(self):
        """Should return None when no maps directory exists."""
        result = _find_map_for_timestamp('nonexistent_pool_xyz_123', datetime.now(timezone.utc))
        assert result is None

    def test_exact_map_creation_timestamp(self, two_maps):
        """At exactly the new map's creation time, the new map should be active."""
        ts = two_maps['new_time']
        result = sml._find_map_for_timestamp('test', ts)
        assert result is not None
        map_file, _ = result
        assert '2025.11.15' in map_file.name


class TestGetInfluenceAtTime:
    """Test get_influence_at_time lookup."""

    def test_returns_old_score_before_map_refresh(self, two_maps):
        """Author influence should use the old map's score before the refresh."""
        score = get_influence_at_time(
            pool_name='test',
            username='alice',
            timestamp=datetime(2025, 11, 10, tzinfo=timezone.utc),
        )
        assert score == 0.50  # old map's score for alice

    def test_returns_new_score_after_map_refresh(self, two_maps):
        """Author influence should use the new map's score after the refresh."""
        score = get_influence_at_time(
            pool_name='test',
            username='alice',
            timestamp=datetime(2025, 11, 20, tzinfo=timezone.utc),
        )
        assert score == 0.80  # new map's score for alice

    def test_returns_none_for_user_not_in_map(self, two_maps):
        """Should return fallback (None by default) for user not in the resolved map."""
        # carol is only in the old map; after the refresh she's gone
        score = get_influence_at_time(
            pool_name='test',
            username='carol',
            timestamp=datetime(2025, 11, 20, tzinfo=timezone.utc),
        )
        assert score is None

    def test_returns_fallback_when_specified(self, two_maps):
        """Should return the provided fallback when user not found."""
        score = get_influence_at_time(
            pool_name='test',
            username='carol',
            timestamp=datetime(2025, 11, 20, tzinfo=timezone.utc),
            fallback_score=0.001,
        )
        assert score == 0.001

    def test_case_insensitive_username(self, two_maps):
        """Username lookup should be case-insensitive."""
        score = get_influence_at_time(
            pool_name='test',
            username='Alice',
            timestamp=datetime(2025, 11, 10, tzinfo=timezone.utc),
        )
        assert score == 0.50

    def test_returns_fallback_when_no_maps_exist(self):
        """Should return fallback when no maps exist for the pool."""
        score = get_influence_at_time(
            pool_name='nonexistent_pool_xyz_123',
            username='alice',
            timestamp=datetime.now(timezone.utc),
            fallback_score=0.01,
        )
        assert score == 0.01

    def test_handles_naive_datetime(self, two_maps):
        """Should treat naive datetimes as UTC."""
        score = get_influence_at_time(
            pool_name='test',
            username='alice',
            timestamp=datetime(2025, 11, 10),  # no tzinfo
        )
        assert score == 0.50

    def test_user_only_in_new_map(self, two_maps):
        """Dave is only in the new map — should not be found in old map period."""
        # Before the new map, dave doesn't exist
        score_old = get_influence_at_time(
            pool_name='test',
            username='dave',
            timestamp=datetime(2025, 11, 10, tzinfo=timezone.utc),
            fallback_score=0.0,
        )
        assert score_old == 0.0  # dave not in old map

        # After the new map, dave exists
        score_new = get_influence_at_time(
            pool_name='test',
            username='dave',
            timestamp=datetime(2025, 11, 20, tzinfo=timezone.utc),
        )
        assert score_new == 0.40


class TestInfluenceCache:
    """Test caching behavior of get_influence_at_time."""

    def test_cache_avoids_repeated_file_reads(self, two_maps, monkeypatch):
        """Calls resolving to the same map should read its file only once,
        even for different tweet timestamps."""
        import builtins
        original_open = builtins.open
        call_count = 0

        def counting_open(*args, **kwargs):
            nonlocal call_count
            path_str = str(args[0]) if args else str(kwargs.get('file', ''))
            if '.json' in path_str and 'tmp' in path_str:
                call_count += 1
            return original_open(*args, **kwargs)

        monkeypatch.setattr(builtins, 'open', counting_open)

        # First call — loads from file
        score1 = get_influence_at_time('test', 'alice', datetime(2025, 11, 10, tzinfo=timezone.utc))
        first_count = call_count

        # Different timestamps within the same map's active period — must hit cache
        score2 = get_influence_at_time('test', 'bob', datetime(2025, 11, 12, 8, 30, tzinfo=timezone.utc))
        score3 = get_influence_at_time('test', 'alice', datetime(2025, 11, 14, 23, 59, tzinfo=timezone.utc))
        second_count = call_count

        assert score1 == 0.50
        assert score2 == 0.30
        assert score3 == 0.50
        assert second_count == first_count  # no additional file reads

    def test_clear_influence_cache_for_specific_pool(self, two_maps):
        """clear_influence_cache(pool_name) should clear only that pool's cache."""
        ts = datetime(2025, 11, 10, tzinfo=timezone.utc)

        # Prime the cache
        get_influence_at_time('test', 'alice', ts)

        # Clear only 'test' pool
        clear_influence_cache('test')

        # Cache should be empty — next call reads from file again
        # This should still work correctly
        score = get_influence_at_time('test', 'alice', ts)
        assert score == 0.50

    def test_clear_influence_cache_all(self, two_maps):
        """clear_influence_cache() with no args should clear everything."""
        ts = datetime(2025, 11, 10, tzinfo=timezone.utc)
        get_influence_at_time('test', 'alice', ts)

        clear_influence_cache()

        score = get_influence_at_time('test', 'alice', ts)
        assert score == 0.50


class TestScoreConsistencyAcrossMapRefresh:
    """Integration-style test: author influence is stable across map refreshes."""

    def test_author_score_does_not_change_when_map_refreshes(self, two_maps):
        """A tweet posted before the map refresh should keep its original influence
        score even after the social map regenerates."""
        # Tweet posted on Nov 10 (old map active)
        tweet_time = datetime(2025, 11, 10, tzinfo=timezone.utc)

        # Look up influence at tweet time
        influence_at_posting = get_influence_at_time('test', 'alice', tweet_time)

        # Now "regenerate" the map — look up influence at a later time
        # But the tweet's score should still use the influence at posting time
        later_time = datetime(2025, 11, 20, tzinfo=timezone.utc)
        influence_after_refresh = get_influence_at_time('test', 'alice', later_time)

        # The scores are different (old=0.50, new=0.80)
        assert influence_at_posting == 0.50
        assert influence_after_refresh == 0.80

        # But if we re-query at the original tweet time, we get the original score
        rechecked = get_influence_at_time('test', 'alice', tweet_time)
        assert rechecked == 0.50  # still the old score, pinned to tweet time
