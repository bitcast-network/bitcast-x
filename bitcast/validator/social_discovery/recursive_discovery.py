"""
Two-stage social discovery with personalized PageRank.

Implements a three-phase approach to network formation:
  Stage 1: Core discovery with strict parameters (standard PageRank)
  Stage 2: Extended discovery with relaxed parameters (recursive expansion)
  Stage 3: Final ranking with personalized PageRank biased toward core accounts

Usage:
    # CLI
    python -m bitcast.validator.social_discovery.recursive_discovery --pool-name tao

    # Programmatic
    path, metrics = await two_stage_discovery(pool_name="tao")
"""

import asyncio
import json
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import bittensor as bt
from dotenv import load_dotenv

from .social_discovery import TwitterNetworkAnalyzer
from .social_map_publisher import publish_social_map
from .pool_manager import PoolManager
from bitcast.validator.utils.config import ENABLE_DATA_PUBLISH
from bitcast.validator.utils.data_publisher import get_global_publisher
from bitcast.validator.tweet_scoring.social_map_loader import parse_social_map_filename


class ConvergenceMetrics:
    """Tracks convergence metrics across iterations."""
    
    def __init__(self):
        self.iterations = []
        
    def add_iteration(
        self,
        iteration: int,
        active_members: Set[str],
        total_accounts: int,
        promoted_count: int,
        relegated_count: int,
        stability: Optional[float] = None
    ):
        """Record metrics for an iteration."""
        self.iterations.append({
            'iteration': iteration,
            'active_members': active_members,
            'total_accounts': total_accounts,
            'promoted': promoted_count,
            'relegated': relegated_count,
            'stability': stability
        })
    
    def get_stability(self, iteration: int) -> Optional[float]:
        """Get stability score for a given iteration."""
        if iteration < len(self.iterations):
            return self.iterations[iteration]['stability']
        return None
    
    def summary(self) -> Dict:
        """Get summary of convergence metrics."""
        return {
            'total_iterations': len(self.iterations),
            'final_stability': self.iterations[-1]['stability'] if self.iterations else None,
            'final_active_count': len(self.iterations[-1]['active_members']) if self.iterations else 0,
            'iterations': [
                {
                    'iteration': it['iteration'],
                    'total_accounts': it['total_accounts'],
                    'active_count': len(it['active_members']),
                    'promoted': it['promoted'],
                    'relegated': it['relegated'],
                    'stability': it['stability']
                }
                for it in self.iterations
            ]
        }


def calculate_stability(
    prev_members: Set[str],
    current_members: Set[str]
) -> float:
    """
    Calculate stability metric between two member sets (Jaccard similarity).
    
    Returns:
        Float between 0.0 (no overlap) and 1.0 (identical)
    """
    if not prev_members and not current_members:
        return 1.0
    
    overlap = len(prev_members & current_members)
    total = len(prev_members | current_members)
    
    return overlap / total if total > 0 else 0.0


def load_social_map_members(
    social_map_path: str, 
    top_n: int = 150
) -> Tuple[Set[str], Dict]:
    """
    Load top accounts from a social map file sorted by score.
    
    Args:
        social_map_path: Path to social map JSON file
        top_n: Number of top accounts to return
        
    Returns:
        Tuple of (top_accounts_set, metadata_dict)
    """
    with open(social_map_path, 'r') as f:
        social_map = json.load(f)
    
    account_scores = [
        (username, data.get('score', 0.0))
        for username, data in social_map['accounts'].items()
    ]
    
    account_scores.sort(key=lambda x: x[1], reverse=True)
    top_accounts = {username for username, _ in account_scores[:top_n]}
    
    metadata = social_map.get('metadata', {})
    
    return top_accounts, metadata


def _get_seed_accounts(pool_name: str, pool_config: Dict) -> List[str]:
    """
    Get seed accounts from the latest social map or fall back to initial accounts.
    
    If a previous social map exists for this pool, uses the top accounts
    (sorted by score) as seeds. Otherwise, uses initial_accounts from pool config.
    """
    social_maps_dir = Path(__file__).parent / "social_maps"
    pool_dir = social_maps_dir / pool_name
    
    if pool_dir.exists():
        social_map_files = [
            f for f in pool_dir.glob("*.json")
            if not f.name.endswith('_adjacency.json')
            and not f.name.endswith('_metadata.json')
            and not f.name.startswith('recursive_summary_')
            and not f.name.startswith('two_stage_summary_')
        ]
        if social_map_files:
            latest_file = max(
                social_map_files,
                key=lambda f: parse_social_map_filename(f.name) or datetime.min.replace(tzinfo=timezone.utc)
            )
            with open(latest_file, 'r') as f:
                existing_data = json.load(f)
            
            max_seed_accounts = pool_config.get('max_seed_accounts', 150)
            all_accounts = [
                (acc, data.get('score', 0.0))
                for acc, data in existing_data['accounts'].items()
            ]
            all_accounts.sort(key=lambda x: x[1], reverse=True)
            seed_accounts = [acc for acc, _ in all_accounts[:max_seed_accounts]]
            
            bt.logging.info(f"Using top {len(seed_accounts)} accounts from previous map as seeds")
            return seed_accounts
    
    bt.logging.info(f"Using {len(pool_config['initial_accounts'])} initial accounts as seeds")
    return list(pool_config['initial_accounts'])


async def two_stage_discovery(
    pool_name: str = "tao",
    max_iterations: int = 3,
    convergence_threshold: float = 0.90,
    core_overrides: Optional[Dict] = None,
    extended_overrides: Optional[Dict] = None,
    run_id_prefix: Optional[str] = None,
    save_summary: bool = True,
    posts_only: bool = True,
    force_cache_refresh: bool = False,
) -> Tuple[str, ConvergenceMetrics]:
    """
    Two-stage social discovery with personalized PageRank.
    
    Discovers social networks through three phases:
      1. Core discovery with strict parameters to identify stable core accounts
      2. Extended discovery with relaxed parameters to grow the network
      3. Final ranking with personalized PageRank biased toward core accounts
    
    Seeds are sourced from the latest existing social map (top N by score)
    or from initial_accounts in the pool config if no prior map exists.
    
    Args:
        pool_name: Pool name from API configuration
        max_iterations: Maximum Stage 2 expansion iterations (default: 3)
        convergence_threshold: Jaccard stability threshold for Stage 2 convergence (default: 0.90)
        core_overrides: Optional dict to override core stage params
                       (min_interaction_weight, min_tweets, max_seed_accounts)
        extended_overrides: Optional dict to override extended stage params
                          (min_interaction_weight, min_tweets, max_seed_accounts,
                           max_iterations, convergence_threshold)
        run_id_prefix: Optional prefix for run IDs
        save_summary: Whether to save discovery summary to file
        posts_only: Use only /user/tweets endpoint (default: True)
        force_cache_refresh: Force fresh Twitter API fetches (default: False)
        
    Returns:
        Tuple of (social_map_path, convergence_metrics)
    """
    bt.logging.info("=" * 80)
    bt.logging.info("TWO-STAGE SOCIAL DISCOVERY")
    bt.logging.info("=" * 80)
    bt.logging.info(f"Pool: {pool_name}")
    
    # Load pool configuration
    pool_manager = PoolManager()
    pool_config = pool_manager.get_pool(pool_name)
    if not pool_config:
        raise ValueError(f"Pool '{pool_name}' not found in configuration")
    
    # Resolve parameters (CLI overrides > pool config > defaults)
    core_ov = core_overrides or {}
    ext_ov = extended_overrides or {}
    
    core_min_interaction_weight = core_ov.get('min_interaction_weight', pool_config.get('core_min_interaction_weight', 2))
    core_min_tweets = core_ov.get('min_tweets', pool_config.get('core_min_tweets', 5))
    core_max_seed_accounts = core_ov.get('max_seed_accounts', pool_config.get('core_max_seed_accounts', 100))
    
    ext_min_interaction_weight = ext_ov.get('min_interaction_weight', pool_config.get('extended_min_interaction_weight', 1))
    ext_min_tweets = ext_ov.get('min_tweets', pool_config.get('extended_min_tweets', 1))
    ext_max_seed_accounts = ext_ov.get('max_seed_accounts', pool_config.get('extended_max_seed_accounts', 300))
    ext_max_iterations = ext_ov.get('max_iterations', pool_config.get('max_discovery_iterations', max_iterations))
    ext_convergence = ext_ov.get('convergence_threshold', pool_config.get('convergence_threshold', convergence_threshold))
    
    bt.logging.info(f"Core params: min_weight={core_min_interaction_weight}, min_tweets={core_min_tweets}, max_seeds={core_max_seed_accounts}")
    bt.logging.info(f"Extended params: min_weight={ext_min_interaction_weight}, min_tweets={ext_min_tweets}, max_seeds={ext_max_seed_accounts}")
    bt.logging.info(f"Extended iterations: max={ext_max_iterations}, convergence={ext_convergence:.0%}")
    bt.logging.info("=" * 80)
    
    # Generate run ID
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if run_id_prefix:
        run_id = f"{run_id_prefix}_{timestamp}"
    else:
        try:
            publisher = get_global_publisher()
            vali_hotkey = publisher.wallet.hotkey.ss58_address
            run_id = f"vali_x_{vali_hotkey}_{timestamp}"
        except RuntimeError:
            run_id = f"two_stage_{timestamp}"
    
    # Get seed accounts (from previous map or initial config)
    seed_accounts = _get_seed_accounts(pool_name, pool_config)
    
    # Create analyzer
    analyzer = TwitterNetworkAnalyzer(
        force_cache_refresh=force_cache_refresh,
        posts_only=posts_only
    )
    
    metrics = ConvergenceMetrics()
    
    # ===== STAGE 1: CORE DISCOVERY (STRICT) =====
    bt.logging.info("")
    bt.logging.info("-" * 80)
    bt.logging.info("STAGE 1: CORE DISCOVERY (Strict Parameters)")
    bt.logging.info("-" * 80)
    
    core_seeds = seed_accounts[:core_max_seed_accounts]
    bt.logging.info(f"Seeds: {len(core_seeds)} accounts")
    
    core_scores, _, _, core_usernames, core_user_info, _ = analyzer.analyze_network(
        seed_accounts=core_seeds,
        keywords=pool_config['keywords'],
        min_followers=0,
        lang=pool_config.get('lang'),
        min_tweets=core_min_tweets,
        min_interaction_weight=core_min_interaction_weight,
    )
    
    core_accounts = set(core_usernames)
    bt.logging.info(f"Core discovery complete: {len(core_accounts)} accounts")
    
    # ===== STAGE 2: EXTENDED DISCOVERY (RELAXED, RECURSIVE) =====
    bt.logging.info("")
    bt.logging.info("-" * 80)
    bt.logging.info("STAGE 2: EXTENDED DISCOVERY (Relaxed Parameters)")
    bt.logging.info("-" * 80)
    bt.logging.info(f"Seeding from {len(core_accounts)} core accounts")
    bt.logging.info(f"Max iterations: {ext_max_iterations}, convergence: {ext_convergence:.0%}")
    
    all_discovered = set(core_accounts)
    prev_top_accounts = set()
    current_seeds = list(core_accounts)
    
    for iteration in range(ext_max_iterations):
        bt.logging.info("")
        bt.logging.info(f"  Extended iteration {iteration + 1}/{ext_max_iterations}")
        bt.logging.info(f"  Seeds: {len(current_seeds)}")
        
        # Discover with relaxed params (no keyword filter during expansion)
        iter_scores, _, _, iter_usernames, _, _ = analyzer.analyze_network(
            seed_accounts=current_seeds,
            keywords=[],  # No keyword filter during discovery
            min_followers=0,
            lang=pool_config.get('lang'),
            min_tweets=0,
            min_interaction_weight=0,
        )
        
        # Track discoveries
        iteration_accounts = set(iter_usernames)
        newly_discovered = iteration_accounts - all_discovered
        all_discovered.update(iteration_accounts)
        
        bt.logging.info(f"  New accounts: {len(newly_discovered)}")
        bt.logging.info(f"  Total discovered: {len(all_discovered)}")
        
        # Get top accounts for convergence check and next iteration seeds
        sorted_accounts = sorted(iter_scores.items(), key=lambda x: x[1], reverse=True)
        current_top = {acc for acc, _ in sorted_accounts[:ext_max_seed_accounts]}
        
        # Convergence check
        stability = None
        if prev_top_accounts:
            stability = calculate_stability(prev_top_accounts, current_top)
            new_count = len(current_top - prev_top_accounts)
            lost_count = len(prev_top_accounts - current_top)
            bt.logging.info(f"  Stability: {stability:.1%} (new: {new_count}, lost: {lost_count})")
        else:
            new_count = len(current_top)
            lost_count = 0
        
        metrics.add_iteration(
            iteration=iteration + 1,
            active_members=current_top,
            total_accounts=len(all_discovered),
            promoted_count=new_count,
            relegated_count=lost_count,
            stability=stability,
        )
        
        if stability is not None and stability >= ext_convergence:
            bt.logging.info(f"  Converged at iteration {iteration + 1} ({stability:.1%} >= {ext_convergence:.0%})")
            break
        
        if not newly_discovered:
            bt.logging.info("  No new accounts discovered, stopping")
            break
        
        prev_top_accounts = current_top
        current_seeds = list(current_top)
    
    bt.logging.info("")
    bt.logging.info(f"Extended discovery complete: {len(all_discovered)} total accounts")
    
    # ===== STAGE 3: FINAL RANKING (PERSONALIZED PAGERANK) =====
    bt.logging.info("")
    bt.logging.info("-" * 80)
    bt.logging.info("STAGE 3: FINAL RANKING (Personalized PageRank)")
    bt.logging.info("-" * 80)
    bt.logging.info(f"Ranking {len(all_discovered)} accounts with core bias ({len(core_accounts)} core)")
    
    scores, adj_matrix, rel_matrix, usernames, user_info_map, total_pool_followers = analyzer.analyze_network(
        seed_accounts=list(all_discovered),
        keywords=pool_config['keywords'],
        min_followers=0,
        lang=pool_config.get('lang'),
        min_tweets=ext_min_tweets,
        min_interaction_weight=ext_min_interaction_weight,
        core_accounts=core_accounts,
        use_personalized_pagerank=True,
    )
    
    # Build social map with is_core flag
    sorted_accounts = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    scaled_pool_difficulty = total_pool_followers / 1000
    core_in_final = core_accounts & set(usernames)
    
    social_map_data = {
        'metadata': {
            'created_at': datetime.now().isoformat(),
            'pool_name': pool_name,
            'total_accounts': len(scores),
            'core_accounts': len(core_in_final),
            'extended_accounts': len(set(usernames) - core_accounts),
            'pool_difficulty': round(scaled_pool_difficulty, 2),
            'total_followers': total_pool_followers,
            'two_stage': True,
        },
        'accounts': {
            username: {
                'score': score,
                'followers_count': user_info_map.get(username, {}).get('followers_count', 0),
                'is_core': username in core_accounts,
            }
            for username, score in sorted_accounts
        }
    }
    
    bt.logging.info(f"Final network: {len(scores)} accounts ({len(core_in_final)} core, {len(set(usernames) - core_accounts)} extended)")
    
    # ===== SAVE RESULTS =====
    social_maps_dir = Path(__file__).parent / "social_maps"
    pool_dir = social_maps_dir / pool_name
    pool_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp_str = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    
    # Social map
    social_map_file = pool_dir / f"{timestamp_str}.json"
    with open(social_map_file, 'w') as f:
        json.dump(social_map_data, f, indent=2)
    
    # Adjacency matrix with relationship scores
    matrix_file = pool_dir / f"{timestamp_str}_adjacency.json"
    matrix_data = {
        'usernames': usernames,
        'adjacency_matrix': adj_matrix.tolist(),
        'relationship_scores': rel_matrix.tolist(),
        'created_at': datetime.now().isoformat()
    }
    with open(matrix_file, 'w') as f:
        json.dump(matrix_data, f, indent=2)
    
    # Metadata
    validator_hotkey = None
    try:
        publisher = get_global_publisher()
        validator_hotkey = publisher.wallet.hotkey.ss58_address
    except (RuntimeError, Exception) as e:
        bt.logging.debug(f"Could not retrieve validator hotkey for metadata: {e}")
    
    metadata_file = pool_dir / f"{timestamp_str}_metadata.json"
    metadata = {
        'run_id': run_id,
        'validator_hotkey': validator_hotkey,
        'created_at': datetime.now().isoformat(),
        'pool_name': pool_name,
        'two_stage': True,
        'core_params': {
            'min_interaction_weight': core_min_interaction_weight,
            'min_tweets': core_min_tweets,
            'max_seed_accounts': core_max_seed_accounts,
        },
        'extended_params': {
            'min_interaction_weight': ext_min_interaction_weight,
            'min_tweets': ext_min_tweets,
            'max_seed_accounts': ext_max_seed_accounts,
            'max_iterations': ext_max_iterations,
            'convergence_threshold': ext_convergence,
        },
    }
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=2)
    
    bt.logging.info(f"Results saved to {pool_dir}")
    
    # Publish if enabled
    if ENABLE_DATA_PUBLISH:
        try:
            success = await publish_social_map(
                pool_name=pool_name,
                social_map_data=social_map_data,
                adjacency_matrix=adj_matrix,
                usernames=usernames,
                run_id=run_id
            )
            if success:
                bt.logging.info(f"Social map published for pool {pool_name}")
            else:
                bt.logging.warning(f"Social map publishing failed for pool {pool_name} (local results saved)")
        except RuntimeError as e:
            error_msg = str(e)
            if "running event loop" in error_msg.lower() or "asyncio.run" in error_msg.lower():
                bt.logging.warning(f"Social map publishing skipped - nested event loop conflict: {e}")
            else:
                bt.logging.warning(f"Social map publishing skipped: {e}")
        except Exception as e:
            bt.logging.warning(f"Social map publishing failed: {e} (local results saved)")
    else:
        bt.logging.debug("Social map publishing disabled by config")
    
    # Save discovery summary
    if save_summary:
        summary_path = pool_dir / f"two_stage_summary_{timestamp}.json"
        summary_data = {
            'pool_name': pool_name,
            'run_id': run_id,
            'total_accounts': len(scores),
            'core_accounts': len(core_in_final),
            'extended_accounts': len(set(usernames) - core_accounts),
            'social_map': str(social_map_file),
            'timestamp': datetime.now().isoformat(),
            'core_params': metadata['core_params'],
            'extended_params': metadata['extended_params'],
            'convergence': metrics.summary(),
        }
        with open(summary_path, 'w') as f:
            json.dump(summary_data, f, indent=2)
        bt.logging.info(f"Discovery summary saved to: {summary_path}")
    
    # Final summary
    bt.logging.info("")
    bt.logging.info("=" * 80)
    bt.logging.info("TWO-STAGE DISCOVERY COMPLETE")
    bt.logging.info("=" * 80)
    bt.logging.info(f"  Total accounts: {len(scores)}")
    bt.logging.info(f"  Core accounts: {len(core_in_final)}")
    bt.logging.info(f"  Extended accounts: {len(set(usernames) - core_accounts)}")
    bt.logging.info(f"  Stage 2 iterations: {len(metrics.iterations)}")
    final_stability = metrics.iterations[-1]['stability'] if metrics.iterations else None
    if final_stability is not None:
        bt.logging.info(f"  Final stability: {final_stability:.1%}")
    bt.logging.info(f"  Social map: {social_map_file}")
    bt.logging.info("=" * 80)
    
    return str(social_map_file), metrics


# CLI interface for standalone execution
if __name__ == "__main__":
    import argparse
    import sys
    from bitcast.validator.utils.config import WALLET_NAME, HOTKEY_NAME
    from bitcast.validator.utils.data_publisher import initialize_global_publisher
    
    # Load environment variables
    env_path = Path(__file__).parents[2] / '.env'  # bitcast/.env
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from {env_path}")
    
    try:
        parser = argparse.ArgumentParser(
            description="Two-stage social discovery with personalized PageRank"
        )
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.subtensor.add_args(parser)
        
        parser.add_argument(
            "--pool-name", type=str, default="tao",
            help="Pool name to discover (default: tao)"
        )
        parser.add_argument(
            "--max-iterations", type=int, default=3,
            help="Max Stage 2 expansion iterations (default: 3)"
        )
        parser.add_argument(
            "--convergence-threshold", type=float, default=0.90,
            help="Stability threshold for Stage 2 convergence (default: 0.90)"
        )
        parser.add_argument(
            "--core-min-weight", type=float, default=None,
            help="Override core stage min_interaction_weight"
        )
        parser.add_argument(
            "--core-min-tweets", type=int, default=None,
            help="Override core stage min_tweets"
        )
        parser.add_argument(
            "--core-max-seeds", type=int, default=None,
            help="Override core stage max_seed_accounts"
        )
        parser.add_argument(
            "--ext-min-weight", type=float, default=None,
            help="Override extended stage min_interaction_weight"
        )
        parser.add_argument(
            "--ext-min-tweets", type=int, default=None,
            help="Override extended stage min_tweets"
        )
        parser.add_argument(
            "--ext-max-seeds", type=int, default=None,
            help="Override extended stage max_seed_accounts"
        )
        parser.add_argument(
            "--run-id-prefix", type=str, default=None,
            help="Prefix for run IDs"
        )
        parser.add_argument(
            "--no-summary", action="store_true",
            help="Don't save discovery summary file"
        )
        parser.add_argument(
            "--dual-endpoint", action="store_true",
            help="Use both /user/tweets and /user/tweetsandreplies endpoints"
        )
        parser.add_argument(
            "--force-refresh", action="store_true",
            help="Force Twitter API cache refresh"
        )
        
        # Build args list with environment-based wallet defaults
        args_list = sys.argv[1:]
        if WALLET_NAME and '--wallet.name' not in args_list:
            args_list.extend(['--wallet.name', WALLET_NAME])
        if HOTKEY_NAME and '--wallet.hotkey' not in args_list:
            args_list.extend(['--wallet.hotkey', HOTKEY_NAME])
        if not any(arg.startswith('--logging.') for arg in args_list):
            args_list.insert(0, '--logging.info')
        
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Initialize global publisher
        wallet = bt.wallet(config=config)
        initialize_global_publisher(wallet)
        bt.logging.info("Global publisher initialized for standalone mode")
        
        # Build override dicts from CLI args
        core_overrides = {}
        if config.core_min_weight is not None:
            core_overrides['min_interaction_weight'] = config.core_min_weight
        if config.core_min_tweets is not None:
            core_overrides['min_tweets'] = config.core_min_tweets
        if config.core_max_seeds is not None:
            core_overrides['max_seed_accounts'] = config.core_max_seeds
        
        extended_overrides = {}
        if config.ext_min_weight is not None:
            extended_overrides['min_interaction_weight'] = config.ext_min_weight
        if config.ext_min_tweets is not None:
            extended_overrides['min_tweets'] = config.ext_min_tweets
        if config.ext_max_seeds is not None:
            extended_overrides['max_seed_accounts'] = config.ext_max_seeds
        
        posts_only = not config.dual_endpoint if hasattr(config, 'dual_endpoint') else True
        force_refresh = config.force_refresh if hasattr(config, 'force_refresh') else False
        
        path, metrics = asyncio.run(two_stage_discovery(
            pool_name=config.pool_name,
            max_iterations=config.max_iterations,
            convergence_threshold=config.convergence_threshold,
            core_overrides=core_overrides or None,
            extended_overrides=extended_overrides or None,
            run_id_prefix=config.run_id_prefix,
            save_summary=not config.no_summary,
            posts_only=posts_only,
            force_cache_refresh=force_refresh,
        ))
        
        print(f"\nTwo-stage discovery complete: {path}")
        
    except KeyboardInterrupt:
        print("\n\nCancelled by user")
        exit(1)
    except Exception as e:
        bt.logging.error(f"Two-stage discovery failed: {e}", exc_info=True)
        print(f"Error: {e}")
        exit(1)
