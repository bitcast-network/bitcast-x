"""
Recursive social discovery that runs discovery iterations until convergence.

This module repeatedly runs the social discovery process, using the results
of each iteration as seeds for the next, until the pool membership stabilizes.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import bittensor as bt
from dotenv import load_dotenv

from .social_discovery import discover_social_network
from .pool_manager import PoolManager


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
    Calculate stability metric between two member sets.
    
    Stability = (intersection) / (union)
    
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
        top_n: Number of top accounts to use as seeds (default: 150)
        
    Returns:
        Tuple of (top_accounts_set, metadata_dict)
    """
    with open(social_map_path, 'r') as f:
        social_map = json.load(f)
    
    # Get all accounts with scores
    account_scores = [
        (username, data.get('score', 0.0))
        for username, data in social_map['accounts'].items()
    ]
    
    # Sort by score descending and take top N
    account_scores.sort(key=lambda x: x[1], reverse=True)
    top_accounts = {username for username, _ in account_scores[:top_n]}
    
    metadata = social_map.get('metadata', {})
    
    return top_accounts, metadata


async def recursive_social_discovery(
    pool_name: str = "tao",
    max_iterations: int = 10,
    convergence_threshold: float = 0.95,
    run_id_prefix: Optional[str] = None,
    save_summary: bool = True,
    posts_only: bool = True
) -> Tuple[str, int, bool, ConvergenceMetrics]:
    """
    Recursively run social discovery until convergence or max iterations.
    
    Each iteration uses the active members from the previous iteration as seeds,
    potentially discovering new accounts and re-ranking existing ones.
    
    Args:
        pool_name: Name of pool to discover (from pools_config.json)
        max_iterations: Maximum number of iterations before stopping
        convergence_threshold: Stability threshold for convergence (0.0 to 1.0)
        run_id_prefix: Optional prefix for run IDs
        save_summary: Whether to save convergence summary to file
        posts_only: If True, use only /user/tweets endpoint (faster, saves quota). Default: True
        
    Returns:
        Tuple of (final_social_map_path, iterations_run, converged, metrics)
        
    Example:
        >>> path, iters, converged, metrics = recursive_social_discovery(
        ...     pool_name="tao",
        ...     max_iterations=5,
        ...     convergence_threshold=0.95
        ... )
    """
    bt.logging.info("=" * 80)
    bt.logging.info("🔄 STARTING RECURSIVE SOCIAL DISCOVERY")
    bt.logging.info("=" * 80)
    bt.logging.info(f"Pool: {pool_name}")
    bt.logging.info(f"Max iterations: {max_iterations}")
    bt.logging.info(f"Convergence threshold: {convergence_threshold:.1%}")
    bt.logging.info("=" * 80)
    
    # Verify pool exists
    pool_manager = PoolManager()
    pool_config = pool_manager.get_pool(pool_name)
    if not pool_config:
        raise ValueError(f"Pool '{pool_name}' not found in configuration")
    
    metrics = ConvergenceMetrics()
    prev_active_members = set()
    converged = False
    final_social_map_path = None
    
    for iteration in range(max_iterations):
        bt.logging.info("")
        bt.logging.info("=" * 80)
        bt.logging.info(f"📍 ITERATION {iteration + 1}/{max_iterations}")
        bt.logging.info("=" * 80)
        
        # Generate unique run_id for this iteration
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if run_id_prefix:
            run_id = f"{run_id_prefix}_iter{iteration + 1:02d}_{timestamp}"
        else:
            run_id = f"recursive_iter{iteration + 1:02d}_{timestamp}"
        
        bt.logging.info(f"Run ID: {run_id}")
        
        # Run discovery for this iteration
        try:
            social_map_path = await discover_social_network(pool_name, run_id, posts_only=posts_only)
            final_social_map_path = social_map_path
        except Exception as e:
            bt.logging.error(f"❌ Discovery failed at iteration {iteration + 1}: {e}")
            raise
        
        # Load results to check convergence
        max_seed_accounts = pool_config.get('max_seed_accounts', 150)
        current_active_members, metadata = load_social_map_members(social_map_path, top_n=max_seed_accounts)
        
        # Calculate statistics
        total_accounts = metadata.get('total_accounts', len(current_active_members))
        
        # Calculate stability if not first iteration
        stability = None
        if prev_active_members:
            stability = calculate_stability(prev_active_members, current_active_members)
            
            # Detailed change analysis
            new_members = current_active_members - prev_active_members
            lost_members = prev_active_members - current_active_members
            unchanged_members = prev_active_members & current_active_members
            
            bt.logging.info("")
            bt.logging.info("-" * 80)
            bt.logging.info("📊 ITERATION STATISTICS")
            bt.logging.info("-" * 80)
            bt.logging.info(f"Total accounts discovered: {total_accounts}")
            bt.logging.info(f"Seed accounts for next iteration: {len(current_active_members)}/{pool_config.get('max_seed_accounts', 150)}")
            bt.logging.info("")
            bt.logging.info("Member Set Changes:")
            bt.logging.info(f"  Unchanged from previous: {len(unchanged_members)}")
            bt.logging.info(f"  New members: {len(new_members)}")
            bt.logging.info(f"  Lost members: {len(lost_members)}")
            bt.logging.info("")
            bt.logging.info(f"🎯 STABILITY: {stability:.2%}")
            
            if new_members:
                bt.logging.info(f"  ➕ New: {sorted(list(new_members))[:10]}")
                if len(new_members) > 10:
                    bt.logging.info(f"     ... and {len(new_members) - 10} more")
            
            if lost_members:
                bt.logging.info(f"  ➖ Lost: {sorted(list(lost_members))[:10]}")
                if len(lost_members) > 10:
                    bt.logging.info(f"     ... and {len(lost_members) - 10} more")
            
            bt.logging.info("-" * 80)
        else:
            # First iteration
            bt.logging.info("")
            bt.logging.info("-" * 80)
            bt.logging.info("📊 ITERATION STATISTICS (BASELINE)")
            bt.logging.info("-" * 80)
            bt.logging.info(f"Total accounts discovered: {total_accounts}")
            bt.logging.info(f"Seed accounts for next iteration: {len(current_active_members)}/{pool_config.get('max_seed_accounts', 150)}")
            bt.logging.info("-" * 80)
        
        # Record metrics
        # Calculate new and lost members for metrics
        if prev_active_members:
            new_count = len(current_active_members - prev_active_members)
            lost_count = len(prev_active_members - current_active_members)
        else:
            new_count = len(current_active_members)
            lost_count = 0
        
        metrics.add_iteration(
            iteration=iteration + 1,
            active_members=current_active_members,
            total_accounts=total_accounts,
            promoted_count=new_count,
            relegated_count=lost_count,
            stability=stability
        )
        
        # Check convergence (skip first iteration)
        if stability is not None and stability >= convergence_threshold:
            bt.logging.info("")
            bt.logging.info("=" * 80)
            bt.logging.info(f"✅ CONVERGED after {iteration + 1} iterations!")
            bt.logging.info(f"   Final stability: {stability:.2%} >= {convergence_threshold:.2%}")
            bt.logging.info("=" * 80)
            converged = True
            break
        
        # Update for next iteration
        prev_active_members = current_active_members
    
    # Final summary
    bt.logging.info("")
    bt.logging.info("=" * 80)
    if not converged:
        bt.logging.warning(f"⚠️  Did NOT converge after {max_iterations} iterations")
        if stability is not None:
            bt.logging.warning(f"   Final stability: {stability:.2%} < {convergence_threshold:.2%}")
    else:
        bt.logging.info("🎉 CONVERGENCE ACHIEVED")
    
    bt.logging.info("")
    bt.logging.info("RECURSIVE DISCOVERY SUMMARY:")
    bt.logging.info(f"  Total iterations run: {iteration + 1}")
    bt.logging.info(f"  Final active members: {len(current_active_members)}")
    bt.logging.info(f"  Convergence status: {'✅ Converged' if converged else '❌ Not converged'}")
    bt.logging.info(f"  Final social map: {final_social_map_path}")
    bt.logging.info("=" * 80)
    
    # Save convergence summary
    if save_summary and final_social_map_path:
        summary_path = Path(final_social_map_path).parent / f"recursive_summary_{timestamp}.json"
        summary_data = {
            'pool_name': pool_name,
            'converged': converged,
            'total_iterations': iteration + 1,
            'convergence_threshold': convergence_threshold,
            'final_social_map': str(final_social_map_path),
            'timestamp': datetime.now().isoformat(),
            'metrics': metrics.summary()
        }
        
        with open(summary_path, 'w') as f:
            json.dump(summary_data, f, indent=2)
        
        bt.logging.info(f"📝 Convergence summary saved to: {summary_path}")
    
    return final_social_map_path, iteration + 1, converged, metrics


# CLI interface for standalone execution
if __name__ == "__main__":
    import argparse
    from bitcast.validator.utils.config import WALLET_NAME, HOTKEY_NAME
    from bitcast.validator.utils.data_publisher import initialize_global_publisher
    
    # Load environment variables
    env_path = Path(__file__).parents[2] / '.env'  # bitcast/.env
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from {env_path}")
    
    try:
        # Create argument parser with bittensor options
        parser = argparse.ArgumentParser(
            description="Recursively discover social networks until convergence"
        )
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.subtensor.add_args(parser)
        
        parser.add_argument(
            "--pool-name",
            type=str,
            default="tao",
            help="Pool name to discover (default: tao)"
        )
        parser.add_argument(
            "--max-iterations",
            type=int,
            default=10,
            help="Maximum iterations before stopping (default: 10)"
        )
        parser.add_argument(
            "--convergence-threshold",
            type=float,
            default=0.95,
            help="Stability threshold for convergence, 0.0-1.0 (default: 0.95)"
        )
        parser.add_argument(
            "--run-id-prefix",
            type=str,
            default=None,
            help="Prefix for run IDs (default: None)"
        )
        parser.add_argument(
            "--no-summary",
            action="store_true",
            help="Don't save convergence summary file"
        )
        parser.add_argument(
            "--dual-endpoint",
            action="store_true",
            help="Use both /user/tweets and /user/tweetsandreplies endpoints (default: posts-only)"
        )
        
        # Build args list from environment variables for wallet config
        # Start with command-line args, then add environment-based defaults
        import sys
        args_list = sys.argv[1:]  # Get actual command-line arguments
        
        # Add wallet config from env if not already in CLI args
        if WALLET_NAME and '--wallet.name' not in args_list:
            args_list.extend(['--wallet.name', WALLET_NAME])
        if HOTKEY_NAME and '--wallet.hotkey' not in args_list:
            args_list.extend(['--wallet.hotkey', HOTKEY_NAME])
        
        # Add info logging if no logging level specified
        if not any(arg.startswith('--logging.') for arg in args_list):
            args_list.insert(0, '--logging.info')
        
        # Parse configuration with merged args
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Initialize global publisher with properly configured wallet
        wallet = bt.wallet(config=config)
        initialize_global_publisher(wallet)
        bt.logging.info("🌐 Global publisher initialized for standalone mode")
        
        # Determine posts_only mode
        posts_only = not config.dual_endpoint if hasattr(config, 'dual_endpoint') else True
        
        # Run recursive discovery (in standalone mode, asyncio.run is safe)
        path, iterations, converged, metrics = asyncio.run(recursive_social_discovery(
            pool_name=config.pool_name,
            max_iterations=config.max_iterations,
            convergence_threshold=config.convergence_threshold,
            run_id_prefix=config.run_id_prefix,
            save_summary=not config.no_summary,
            posts_only=posts_only
        ))
        
        print("\n" + "=" * 80)
        print("RECURSIVE DISCOVERY COMPLETE")
        print("=" * 80)
        print(f"Status: {'✅ Converged' if converged else '❌ Not converged'}")
        print(f"Iterations: {iterations}")
        print(f"Final map: {path}")
        print("=" * 80)
        
    except KeyboardInterrupt:
        print("\n\n❌ Cancelled by user")
        exit(1)
    except Exception as e:
        bt.logging.error(f"Recursive discovery failed: {e}", exc_info=True)
        print(f"❌ Error: {e}")
        exit(1)

