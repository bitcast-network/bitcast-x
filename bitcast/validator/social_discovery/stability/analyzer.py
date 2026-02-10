"""
Stability analyzer for social network maps.

Uses the production TwitterNetworkAnalyzer and TwitterClient directly,
ensuring stability tests always reflect real production logic.

Pipeline:
  1. Two-stage (or single-stage) social map discovery
  2. Fetch extended tweet history for top accounts
  3. Build per-window networks
  4. Calculate cross-window stability metrics
"""

import numpy as np
import networkx as nx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

import bittensor as bt

from ..social_discovery import TwitterNetworkAnalyzer
from ..pool_manager import PoolManager
from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.utils.config import (
    PAGERANK_MENTION_WEIGHT,
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    PAGERANK_ALPHA,
)

from .config import (
    EXTENDED_FETCH_DAYS,
    NUM_WINDOWS,
    WINDOW_DAYS,
    TOP_N_ACCOUNTS,
    MAX_CORE_ITERATIONS,
    CORE_CONVERGENCE_THRESHOLD,
    OUTPUT_DIR,
)
from .metrics import (
    calculate_window_metrics,
    calculate_cross_window_stability,
    calculate_per_window_summary,
    calculate_account_stability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_tweets_to_window(
    tweets: List[Dict],
    window_start: datetime,
    window_end: datetime,
) -> List[Dict]:
    """Filter tweets to a specific time window."""
    filtered = []
    for tweet in tweets:
        created_at = tweet.get("created_at")
        if not created_at:
            continue
        try:
            tweet_date = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            continue

        # Ensure window bounds are tz-aware
        ws = window_start if window_start.tzinfo else window_start.replace(tzinfo=tweet_date.tzinfo)
        we = window_end if window_end.tzinfo else window_end.replace(tzinfo=tweet_date.tzinfo)

        if ws <= tweet_date < we:
            filtered.append(tweet)
    return filtered


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ---------------------------------------------------------------------------
# StabilityAnalyzer
# ---------------------------------------------------------------------------

class StabilityAnalyzer:
    """
    Analyses temporal stability of social network maps.

    All network analysis is delegated to the production
    ``TwitterNetworkAnalyzer.analyze_network()`` — no reimplementation.
    """

    def __init__(
        self,
        pool_name: str,
        *,
        max_workers: int = 10,
        output_dir: Optional[Path] = None,
    ):
        self.pool_name = pool_name

        # Load pool config from the production PoolManager
        pm = PoolManager()
        self.pool_config = pm.get_pool(pool_name)
        if not self.pool_config:
            raise ValueError(f"Pool '{pool_name}' not found in PoolManager configuration")

        # Production objects — shared across all grid-search combinations
        self.twitter_client = TwitterClient(posts_only=True)
        self.network_analyzer = TwitterNetworkAnalyzer(
            twitter_client=self.twitter_client,
            max_workers=max_workers,
        )
        self.max_workers = max_workers
        self.output_dir = output_dir or OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public: two-stage analysis
    # ------------------------------------------------------------------

    def run_two_stage_analysis(
        self,
        *,
        core_params: Dict,
        extended_params: Dict,
        top_n: int = TOP_N_ACCOUNTS,
        force_refresh: bool = False,
    ) -> Dict:
        """
        Run the full stability pipeline using two-stage discovery.

        Args:
            core_params: Overrides for core stage
                         (min_interaction_weight, min_tweets, max_seed_accounts)
            extended_params: Overrides for extended stage
                            (+ recursive, max_iterations, convergence_threshold)
            top_n: Top accounts to track
            force_refresh: Force-refresh tweet cache

        Returns:
            Result dict with social_map, window_metrics, stability, etc.
        """
        now = datetime.now(timezone.utc)

        # --- Resolve parameters (override > pool config > defaults) ----
        core_min_weight = core_params.get(
            "min_interaction_weight",
            self.pool_config.get("core_min_interaction_weight", 2),
        )
        core_min_tweets = core_params.get(
            "min_tweets",
            self.pool_config.get("core_min_tweets", 5),
        )
        core_max_seeds = core_params.get(
            "max_seed_accounts",
            self.pool_config.get("core_max_seed_accounts", 100),
        )

        ext_min_weight = extended_params.get(
            "min_interaction_weight",
            self.pool_config.get("extended_min_interaction_weight", 1),
        )
        ext_min_tweets = extended_params.get(
            "min_tweets",
            self.pool_config.get("extended_min_tweets", 1),
        )
        ext_max_seeds = extended_params.get(
            "max_seed_accounts",
            self.pool_config.get("extended_max_seed_accounts", 300),
        )
        recursive = extended_params.get("recursive", False)
        ext_max_iter = extended_params.get("max_iterations", 3)
        ext_convergence = extended_params.get("convergence_threshold", 0.90)

        bt.logging.info("=" * 80)
        bt.logging.info("STABILITY: TWO-STAGE ANALYSIS")
        bt.logging.info("=" * 80)
        bt.logging.info(
            f"Core: min_weight={core_min_weight}, min_tweets={core_min_tweets}, "
            f"max_seeds={core_max_seeds}"
        )
        bt.logging.info(
            f"Extended: min_weight={ext_min_weight}, min_tweets={ext_min_tweets}, "
            f"max_seeds={ext_max_seeds}, recursive={recursive}"
        )

        # ====== STAGE 1: Core discovery (strict, recursive) ============
        bt.logging.info("-" * 80)
        bt.logging.info("STAGE 1: Core discovery (strict)")
        bt.logging.info("-" * 80)

        seed_accounts = list(self.pool_config["initial_accounts"])
        current_core_seeds = seed_accounts[:core_max_seeds]
        prev_core_top: Set[str] = set()
        core_accounts: Set[str] = set()
        core_scores: Dict[str, float] = {}

        for core_iter in range(MAX_CORE_ITERATIONS):
            bt.logging.info(
                f"  Core iter {core_iter + 1}/{MAX_CORE_ITERATIONS} | "
                f"seeds={len(current_core_seeds)}"
            )

            core_scores, _, _, core_usernames, _, _ = self.network_analyzer.analyze_network(
                seed_accounts=current_core_seeds,
                keywords=self.pool_config["keywords"],
                min_followers=0,
                lang=self.pool_config.get("lang"),
                min_tweets=core_min_tweets,
                min_interaction_weight=core_min_weight,
                skip_if_cache_fresh=True,
            )

            core_accounts = set(core_usernames)
            sorted_core = sorted(core_scores.items(), key=lambda x: x[1], reverse=True)
            current_core_top = {acc for acc, _ in sorted_core[:core_max_seeds]}

            if prev_core_top:
                stability = _jaccard(prev_core_top, current_core_top)
                bt.logging.info(f"  Stability: {stability:.1%}")
                if stability >= CORE_CONVERGENCE_THRESHOLD:
                    bt.logging.info(f"  Core converged at iter {core_iter + 1}")
                    break
            prev_core_top = current_core_top
            current_core_seeds = list(current_core_top)

        bt.logging.info(f"Core discovery complete: {len(core_accounts)} accounts")

        # ====== STAGE 2: Extended discovery (relaxed, PPR) =============
        bt.logging.info("-" * 80)
        bt.logging.info("STAGE 2: Extended discovery (relaxed, personalized PageRank)")
        bt.logging.info("-" * 80)

        sorted_core_by_score = sorted(core_scores.items(), key=lambda x: x[1], reverse=True)
        current_seeds = [acc for acc, _ in sorted_core_by_score[:ext_max_seeds]]
        all_discovered: Set[str] = set(core_accounts)
        prev_top: Set[str] = set()

        ext_iterations = ext_max_iter if recursive else 1
        scores: Dict[str, float] = {}
        adj_matrix = None
        usernames: List[str] = []
        user_info_map: Dict[str, Dict] = {}
        total_pool_followers = 0

        for iteration in range(ext_iterations):
            bt.logging.info(
                f"  Extended iter {iteration + 1}/{ext_iterations} | "
                f"seeds={len(current_seeds)}"
            )

            scores, adj_matrix, _, usernames, user_info_map, total_pool_followers = (
                self.network_analyzer.analyze_network(
                    seed_accounts=current_seeds,
                    keywords=self.pool_config["keywords"],
                    min_followers=0,
                    lang=self.pool_config.get("lang"),
                    min_tweets=ext_min_tweets,
                    min_interaction_weight=ext_min_weight,
                    core_accounts=core_accounts,
                    use_personalized_pagerank=True,
                    skip_if_cache_fresh=True,
                )
            )

            iteration_accounts = set(usernames)
            newly_discovered = iteration_accounts - all_discovered
            all_discovered.update(iteration_accounts)
            bt.logging.info(f"  New={len(newly_discovered)}, total={len(all_discovered)}")

            if not recursive:
                break

            sorted_ext = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            current_top = {acc for acc, _ in sorted_ext[:ext_max_seeds]}

            if prev_top:
                stab = _jaccard(prev_top, current_top)
                bt.logging.info(f"  Stability: {stab:.1%}")
                if stab >= ext_convergence:
                    bt.logging.info(f"  Extended converged at iter {iteration + 1}")
                    break
                if not newly_discovered:
                    bt.logging.info("  No new accounts; stopping")
                    break
            prev_top = current_top
            current_seeds = list(current_top)

        bt.logging.info(f"Extended discovery complete: {len(scores)} accounts")

        # ====== Build social map from final iteration =================
        sorted_accounts = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        core_in_final = core_accounts & set(usernames)

        social_map = {
            "metadata": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "pool_name": self.pool_name,
                "total_accounts": len(scores),
                "core_accounts": len(core_in_final),
                "extended_accounts": len(set(usernames) - core_accounts),
                "total_followers": total_pool_followers,
                "two_stage": True,
            },
            "accounts": {
                username: {
                    "score": score,
                    "followers_count": user_info_map.get(username, {}).get("followers_count", 0),
                    "is_core": username in core_accounts,
                }
                for username, score in sorted_accounts
            },
        }

        # ====== Windowed stability analysis ============================
        top_accounts = [acc for acc, _ in sorted_accounts[:top_n]]

        bt.logging.info("-" * 80)
        bt.logging.info(
            f"Fetching {EXTENDED_FETCH_DAYS}d history for top {len(top_accounts)} accounts"
        )

        extended_tweets = self._fetch_pool_tweets(top_accounts, days=EXTENDED_FETCH_DAYS)

        windows = self._define_windows(now)
        window_results = self._analyze_windows(
            windows=windows,
            extended_tweets=extended_tweets,
            top_accounts=top_accounts,
            keywords=self.pool_config["keywords"],
            core_accounts=core_accounts,
            use_personalized_pagerank=True,
        )

        stability = calculate_cross_window_stability(window_results, top_n=top_n)
        account_stab = calculate_account_stability(window_results, top_accounts)

        bt.logging.info(f"Overall stability: {stability['overall']:.3f}")
        bt.logging.info("=" * 80)

        return {
            "metadata": {
                "analysis_time": datetime.now(timezone.utc).isoformat(),
                "pool_name": self.pool_name,
                "top_n": top_n,
                "two_stage": True,
                "core_accounts_count": len(core_in_final),
                "core_params": core_params,
                "extended_params": extended_params,
            },
            "social_map": social_map,
            "core_accounts": list(core_accounts),
            "window_metrics": window_results,
            "window_summary": calculate_per_window_summary(window_results),
            "stability": stability,
            "account_stability": account_stab,
        }

    # ------------------------------------------------------------------
    # Public: single-stage analysis
    # ------------------------------------------------------------------

    def run_analysis(
        self,
        *,
        params: Optional[Dict] = None,
        top_n: int = TOP_N_ACCOUNTS,
        force_refresh: bool = False,
    ) -> Dict:
        """
        Run single-stage (recursive discovery) stability analysis.

        Args:
            params: Optional overrides (min_interaction_weight, min_tweets,
                    max_seed_accounts)
            top_n: Top accounts to track
            force_refresh: Force-refresh cache

        Returns:
            Result dict
        """
        params = params or {}
        now = datetime.now(timezone.utc)

        min_weight = params.get(
            "min_interaction_weight",
            self.pool_config.get("min_interaction_weight", 2),
        )
        min_tweets = params.get(
            "min_tweets",
            self.pool_config.get("min_tweets", 1),
        )
        max_seeds = params.get(
            "max_seed_accounts",
            self.pool_config.get("max_seed_accounts", 150),
        )

        bt.logging.info("=" * 80)
        bt.logging.info("STABILITY: SINGLE-STAGE ANALYSIS")
        bt.logging.info("=" * 80)

        # Recursive discovery
        seed_accounts = list(self.pool_config["initial_accounts"])
        max_iterations = 10
        convergence_threshold = 0.95
        prev_top: Set[str] = set()
        scores: Dict[str, float] = {}
        usernames: List[str] = []
        user_info_map: Dict[str, Dict] = {}
        total_pool_followers = 0

        for iteration in range(max_iterations):
            seeds = seed_accounts if iteration == 0 else list(prev_top)
            bt.logging.info(f"  Iter {iteration + 1}/{max_iterations} | seeds={len(seeds)}")

            scores, adj_matrix, _, usernames, user_info_map, total_pool_followers = (
                self.network_analyzer.analyze_network(
                    seed_accounts=seeds,
                    keywords=self.pool_config["keywords"],
                    min_followers=0,
                    lang=self.pool_config.get("lang"),
                    min_tweets=min_tweets,
                    min_interaction_weight=min_weight,
                    skip_if_cache_fresh=True,
                )
            )

            sorted_accs = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            current_top = {acc for acc, _ in sorted_accs[:max_seeds]}

            if prev_top:
                stab = _jaccard(prev_top, current_top)
                bt.logging.info(f"  Stability: {stab:.1%}")
                if stab >= convergence_threshold:
                    bt.logging.info(f"  Converged at iter {iteration + 1}")
                    break
            prev_top = current_top

        sorted_accounts = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        social_map = {
            "metadata": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "pool_name": self.pool_name,
                "total_accounts": len(scores),
                "total_followers": total_pool_followers,
            },
            "accounts": {
                username: {
                    "score": score,
                    "followers_count": user_info_map.get(username, {}).get("followers_count", 0),
                }
                for username, score in sorted_accounts
            },
        }

        # Windowed stability analysis
        top_accounts = [acc for acc, _ in sorted_accounts[:top_n]]
        extended_tweets = self._fetch_pool_tweets(top_accounts, days=EXTENDED_FETCH_DAYS)

        windows = self._define_windows(now)
        window_results = self._analyze_windows(
            windows=windows,
            extended_tweets=extended_tweets,
            top_accounts=top_accounts,
            keywords=self.pool_config["keywords"],
        )

        stability = calculate_cross_window_stability(window_results, top_n=top_n)
        account_stab = calculate_account_stability(window_results, top_accounts)

        bt.logging.info(f"Overall stability: {stability['overall']:.3f}")
        bt.logging.info("=" * 80)

        return {
            "metadata": {
                "analysis_time": datetime.now(timezone.utc).isoformat(),
                "pool_name": self.pool_name,
                "top_n": top_n,
                "params": params,
            },
            "social_map": social_map,
            "window_metrics": window_results,
            "window_summary": calculate_per_window_summary(window_results),
            "stability": stability,
            "account_stability": account_stab,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _define_windows(
        self, now: datetime
    ) -> List[Tuple[datetime, datetime, str]]:
        """Non-overlapping time windows, most recent first."""
        windows = []
        for i in range(NUM_WINDOWS):
            end = now - timedelta(days=i * WINDOW_DAYS)
            start = end - timedelta(days=WINDOW_DAYS)
            label = f"Window {i+1}: Days -{(i+1)*WINDOW_DAYS} to -{i*WINDOW_DAYS}"
            windows.append((start, end, label))
        return windows

    @staticmethod
    def _build_window_network(
        window_tweets: Dict[str, List[Dict]],
        restrict_to_accounts: Set[str],
        core_accounts: Optional[Set[str]] = None,
        use_personalized_pagerank: bool = False,
    ) -> Tuple[Dict[str, float], np.ndarray, List[str], Dict[str, Dict], int]:
        """
        Build an interaction network from pre-filtered window tweets.

        This is used for per-window stability analysis where tweets have
        already been filtered to a specific time window.  It uses the
        same PageRank weights as the production ``analyze_network()``
        but skips tweet fetching and keyword relevance checking (those
        were already handled during discovery).

        Args:
            window_tweets: {username: [tweet_dicts]} — already filtered
                           to the target time window.
            restrict_to_accounts: Only include these accounts in the
                                  network (the top-N from discovery).
            core_accounts: Optional core set for personalized PageRank.
            use_personalized_pagerank: Bias PageRank toward core.

        Returns:
            (scores, adjacency_matrix, usernames, user_info_map,
             total_pool_followers)
        """
        restrict_lower = {a.lower() for a in restrict_to_accounts}

        # --- Step 1: Build interaction edges from tweets ---------------
        interaction_weights: Dict[tuple, float] = {}   # max weight
        relationship_scores: Dict[tuple, float] = {}   # cumulative
        user_info_map: Dict[str, Dict] = {}

        for from_user, tweets in window_tweets.items():
            from_user = from_user.lower()
            if from_user not in restrict_lower:
                continue
            for tweet in tweets:
                # Skip reply tweets (matches production behaviour)
                if tweet.get("in_reply_to_status_id"):
                    continue

                targets: List[Tuple[str, float]] = []

                # Mentions
                for tagged in tweet.get("tagged_accounts", []):
                    tagged = tagged.lower()
                    if tagged != from_user and tagged in restrict_lower:
                        targets.append((tagged, PAGERANK_MENTION_WEIGHT))

                # Retweet
                rt_user = (tweet.get("retweeted_user") or "").lower()
                if rt_user and rt_user != from_user and rt_user in restrict_lower:
                    targets.append((rt_user, PAGERANK_RETWEET_WEIGHT))

                # Quote
                qt_user = (tweet.get("quoted_user") or "").lower()
                if qt_user and qt_user != from_user and qt_user in restrict_lower:
                    targets.append((qt_user, PAGERANK_QUOTE_WEIGHT))

                for to_user, weight in targets:
                    key = (from_user, to_user)
                    interaction_weights[key] = max(
                        interaction_weights.get(key, 0), weight
                    )
                    relationship_scores[key] = (
                        relationship_scores.get(key, 0) + weight
                    )

        # Collect all users that appear in at least one edge
        all_users = set()
        for f, t in interaction_weights:
            all_users.add(f)
            all_users.add(t)

        if not interaction_weights:
            empty = np.array([])
            return {}, empty, [], {}, 0

        # --- Step 2: PageRank ------------------------------------------
        G = nx.DiGraph()
        for (f, t), w in interaction_weights.items():
            G.add_edge(f, t, weight=w)

        try:
            if use_personalized_pagerank and core_accounts:
                personalization = {
                    n: (1.0 if n in core_accounts else 0.0)
                    for n in G.nodes()
                }
                if sum(personalization.values()) > 0:
                    pr = nx.pagerank(
                        G, weight="weight", alpha=PAGERANK_ALPHA,
                        personalization=personalization, max_iter=1000,
                    )
                else:
                    pr = nx.pagerank(
                        G, weight="weight", alpha=PAGERANK_ALPHA,
                        max_iter=1000,
                    )
            else:
                pr = nx.pagerank(
                    G, weight="weight", alpha=PAGERANK_ALPHA,
                    max_iter=1000,
                )
        except nx.PowerIterationFailedConvergence:
            pr = {n: 1.0 / len(G.nodes()) for n in G.nodes()}

        # Normalise + scale by pool difficulty
        total_pool_followers = sum(
            user_info_map.get(u, {}).get("followers_count", 0)
            for u in all_users
        )
        total_score = sum(pr.values())
        if total_score > 0:
            normed = {u: s / total_score for u, s in pr.items()}
        else:
            normed = pr
        scores = {
            u: round(s * (total_pool_followers / 1000), 2)
            for u, s in normed.items()
        }

        # --- Step 3: Adjacency matrix ----------------------------------
        usernames_sorted = sorted(all_users)
        n = len(usernames_sorted)
        adj = np.zeros((n, n))
        idx = {u: i for i, u in enumerate(usernames_sorted)}
        for (f, t), w in interaction_weights.items():
            adj[idx[f], idx[t]] = w

        return scores, adj, usernames_sorted, user_info_map, total_pool_followers

    def _fetch_pool_tweets(
        self,
        accounts: List[str],
        days: int = EXTENDED_FETCH_DAYS,
    ) -> Dict[str, Dict]:
        """
        Fetch tweet history for a list of accounts using the production
        TwitterClient (concurrent).
        """
        bt.logging.info(
            f"Fetching {days}d of tweets for {len(accounts)} accounts..."
        )
        results: Dict[str, Dict] = {}
        failed: List[str] = []

        def _fetch_one(username: str) -> Tuple[str, Optional[Dict]]:
            try:
                data = self.twitter_client.fetch_user_tweets(
                    username.lower(),
                    fetch_days=days,
                    skip_if_cache_fresh=True,
                )
                return username, data
            except Exception as e:
                bt.logging.warning(f"Failed to fetch @{username}: {e}")
                return username, None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(_fetch_one, u): u for u in accounts}
            for i, future in enumerate(as_completed(futures)):
                username, data = future.result()
                if data:
                    results[username] = data
                else:
                    failed.append(username)
                if (i + 1) % 50 == 0:
                    bt.logging.info(f"  Fetched {i + 1}/{len(accounts)}")

        bt.logging.info(
            f"Fetch complete: {len(results)} ok, {len(failed)} failed"
        )
        return results

    def _analyze_windows(
        self,
        windows: List[Tuple[datetime, datetime, str]],
        extended_tweets: Dict[str, Dict],
        top_accounts: List[str],
        keywords: List[str],
        core_accounts: Optional[Set[str]] = None,
        use_personalized_pagerank: bool = False,
    ) -> List[Dict]:
        """Build per-window networks and compute metrics."""
        bt.logging.info(
            f"Analysing {len(windows)} windows ({WINDOW_DAYS}d each)"
        )
        window_results: List[Dict] = []

        empty_window = {
            "node_count": 0,
            "edge_count": 0,
            "density": 0,
            "avg_k_core": 0,
            "max_k_core": 0,
            "clustering_coefficient": 0,
            "avg_weighted_degree": 0,
            "total_edge_weight": 0,
            "pagerank_scores": {},
            "k_cores": {},
        }

        for window_start, window_end, label in windows:
            bt.logging.info(f"  {label}")

            # Filter tweets to this window
            window_tweets: Dict[str, Any] = {}
            for username, data in extended_tweets.items():
                tweets = data.get("tweets", [])
                filtered = _filter_tweets_to_window(tweets, window_start, window_end)
                if filtered:
                    window_tweets[username] = filtered

            total_tweets = sum(len(t) for t in window_tweets.values())
            bt.logging.info(
                f"    {len(window_tweets)} accounts, {total_tweets} tweets"
            )

            if not window_tweets:
                metrics = dict(empty_window)
                metrics.update(
                    window_label=label,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                )
                window_results.append(metrics)
                continue

            # Build the interaction network from *only* this window's
            # tweets, restricted to the top-N accounts discovered
            # earlier.  This avoids the production analyze_network()
            # which would re-fetch / use full cached tweet histories
            # and ignore the time-window boundaries.
            restrict = set(top_accounts)

            w_scores, w_adj, w_usernames, w_info, _ = (
                self._build_window_network(
                    window_tweets=window_tweets,
                    restrict_to_accounts=restrict,
                    core_accounts=core_accounts,
                    use_personalized_pagerank=use_personalized_pagerank,
                )
            )

            if not w_scores:
                metrics = dict(empty_window)
                metrics.update(
                    window_label=label,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                )
                window_results.append(metrics)
                continue

            metrics = calculate_window_metrics(
                adjacency_matrix=w_adj,
                usernames=w_usernames,
                scores=w_scores,
                user_info_map=w_info,
            )
            metrics["window_label"] = label
            metrics["window_start"] = window_start.isoformat()
            metrics["window_end"] = window_end.isoformat()
            metrics["accounts_with_tweets"] = len(window_tweets)
            metrics["total_tweets"] = total_tweets

            window_results.append(metrics)

            bt.logging.info(
                f"    Nodes={metrics['node_count']}, "
                f"Edges={metrics['edge_count']}, "
                f"Density={metrics['density']:.4f}"
            )

        return window_results

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        """Release resources (no-op kept for API compat)."""
        pass
