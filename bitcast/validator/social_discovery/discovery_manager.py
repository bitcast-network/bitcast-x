"""
Background discovery manager.

Runs social discovery in a dedicated thread so scoring cycles are never blocked.
Uses a simple lock-based guard to prevent overlapping runs.
"""

import asyncio
import threading
import time
from typing import Dict, Optional

import bittensor as bt


class DiscoveryManager:
    """
    Manages social discovery as a background task.

    Discovery writes social-map JSON files to disk; scoring reads them.
    There is no shared in-memory state, so concurrent execution is safe.
    """

    _instance: Optional["DiscoveryManager"] = None

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_results: Dict[str, str] = {}
        self._last_error: Optional[str] = None
        self._last_run_time: Optional[float] = None

    @classmethod
    def get_instance(cls) -> "DiscoveryManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_results(self) -> Dict[str, str]:
        return self._last_results

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def maybe_start(self) -> bool:
        """
        Start discovery in the background if not already running.

        Returns True if a new run was launched, False if one is already active.
        """
        with self._lock:
            if self._running:
                elapsed = ""
                if self._last_run_time is not None:
                    mins = (time.time() - self._last_run_time) / 60
                    elapsed = f" (running for {mins:.0f}m)"
                bt.logging.info(
                    f"Social discovery already running{elapsed}, skipping — scoring will proceed"
                )
                return False

            self._running = True
            self._last_error = None
            self._last_run_time = time.time()

        self._thread = threading.Thread(
            target=self._run_discovery,
            name="social-discovery",
            daemon=True,
        )
        self._thread.start()
        bt.logging.info("Social discovery started in background thread")
        return True

    def _run_discovery(self):
        """Thread target — runs discovery in its own event loop."""
        try:
            from bitcast.validator.social_discovery.social_discovery import (
                run_discovery_for_stale_pools,
            )

            results = asyncio.run(run_discovery_for_stale_pools())
            self._last_results = results or {}

            if results:
                bt.logging.info(
                    f"Background discovery complete for {len(results)} pool(s): "
                    f"{list(results.keys())}"
                )
            else:
                bt.logging.info("Background discovery complete — no pools needed updating")

        except Exception as e:
            self._last_error = str(e)
            bt.logging.error(f"Background discovery failed: {e}")

        finally:
            elapsed = ""
            if self._last_run_time is not None:
                mins = (time.time() - self._last_run_time) / 60
                elapsed = f" after {mins:.1f}m"
            bt.logging.info(f"Social discovery thread finished{elapsed}")

            with self._lock:
                self._running = False
