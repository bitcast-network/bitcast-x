"""
Tests for DiscoveryManager background concurrency.

Verifies that:
- Discovery runs in a background thread
- Overlapping runs are skipped (lock guard)
- The running flag resets on completion and on failure
"""

import time
import threading
from unittest.mock import patch, MagicMock

import pytest

from bitcast.validator.social_discovery.discovery_manager import DiscoveryManager


@pytest.fixture(autouse=True)
def fresh_manager():
    """Reset the singleton before each test."""
    DiscoveryManager._instance = None
    yield
    DiscoveryManager._instance = None


# Patch target: the function is imported lazily inside _run_discovery,
# so we patch at the source module.
DISCOVERY_FN = "bitcast.validator.social_discovery.social_discovery.run_discovery_for_stale_pools"


class TestDiscoveryManager:

    def test_maybe_start_returns_true_on_first_call(self):
        mgr = DiscoveryManager.get_instance()
        with patch.object(mgr, "_run_discovery"):
            assert mgr.maybe_start() is True

    def test_maybe_start_returns_false_while_running(self):
        """Second call while first is still running should be skipped."""
        mgr = DiscoveryManager.get_instance()
        barrier = threading.Event()

        def slow_run():
            barrier.wait(timeout=5)

        with patch.object(mgr, "_run_discovery", side_effect=slow_run):
            assert mgr.maybe_start() is True
            time.sleep(0.1)

            assert mgr.is_running is True
            assert mgr.maybe_start() is False

            barrier.set()
            mgr._thread.join(timeout=5)

    def test_running_resets_after_completion(self):
        """After discovery finishes, a new run can start."""
        mgr = DiscoveryManager.get_instance()

        async def quick():
            return {"tao": "/tmp/fake.json"}

        with patch(DISCOVERY_FN, side_effect=quick):
            assert mgr.maybe_start() is True
            mgr._thread.join(timeout=5)

            assert mgr.is_running is False
            assert mgr.last_results == {"tao": "/tmp/fake.json"}
            assert mgr.last_error is None

    def test_running_resets_after_failure(self):
        """If discovery crashes, the flag still resets so future runs aren't blocked."""
        mgr = DiscoveryManager.get_instance()

        async def boom():
            raise RuntimeError("twitter API exploded")

        with patch(DISCOVERY_FN, side_effect=boom):
            assert mgr.maybe_start() is True
            mgr._thread.join(timeout=5)

            assert mgr.is_running is False
            assert mgr.last_error == "twitter API exploded"

    def test_can_restart_after_previous_completes(self):
        """Full lifecycle: start -> finish -> start again."""
        mgr = DiscoveryManager.get_instance()
        call_count = 0

        async def quick():
            nonlocal call_count
            call_count += 1
            return {}

        with patch(DISCOVERY_FN, side_effect=quick):
            # First run
            assert mgr.maybe_start() is True
            mgr._thread.join(timeout=5)
            assert mgr.is_running is False

        with patch(DISCOVERY_FN, side_effect=quick):
            # Second run
            assert mgr.maybe_start() is True
            mgr._thread.join(timeout=5)
            assert mgr.is_running is False

        assert call_count == 2
