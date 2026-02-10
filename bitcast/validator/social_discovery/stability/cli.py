#!/usr/bin/env python3
"""
CLI for running stability analysis and grid searches.

Usage (from the bitcast-x root):

    # Two-stage grid search (recursive) for bittensor pool
    python -m bitcast.validator.social_discovery.stability.cli \
        --pool tao --grid --two-stage --recursive

    # Single analysis run
    python -m bitcast.validator.social_discovery.stability.cli \
        --pool tao --two-stage

    # Single-stage grid search
    python -m bitcast.validator.social_discovery.stability.cli \
        --pool tao --grid
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import bittensor as bt
from dotenv import load_dotenv

from .analyzer import StabilityAnalyzer
from .grid_search import GridSearchRunner
from .config import OUTPUT_DIR, TOP_N_ACCOUNTS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stability analysis for social discovery maps",
    )

    # Pool selection
    parser.add_argument(
        "--pool",
        type=str,
        default="tao",
        help="Pool name to analyse (default: tao)",
    )

    # Analysis mode
    parser.add_argument(
        "--two-stage",
        action="store_true",
        help="Use two-stage discovery (core + extended)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Enable recursive expansion in Stage 2 (requires --two-stage)",
    )

    # Grid search
    parser.add_argument(
        "--grid",
        action="store_true",
        help="Run parameter grid search instead of a single analysis",
    )

    # Tuning
    parser.add_argument(
        "--top-n",
        type=int,
        default=TOP_N_ACCOUNTS,
        help=f"Top N accounts to track (default: {TOP_N_ACCOUNTS})",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Concurrent workers for tweet fetching (default: 10)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Custom output directory (default: stability/output/)",
    )

    # Parameter overrides for single-run mode
    parser.add_argument("--min-interaction-weight", type=float, default=None)
    parser.add_argument("--min-tweets", type=int, default=None)
    parser.add_argument("--max-seed-accounts", type=int, default=None)

    # Save control
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Skip saving results to disk",
    )

    # Bittensor logging
    bt.logging.add_args(parser)

    return parser


def main(argv=None):
    # Load .env from bitcast root
    env_path = Path(__file__).parents[3] / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)

    parser = build_parser()

    # Default to info logging
    args_list = argv or sys.argv[1:]
    if not any(arg.startswith("--logging.") for arg in args_list):
        args_list = ["--logging.info"] + list(args_list)

    config = bt.config(parser, args=args_list)
    bt.logging.set_config(config=config.logging)

    pool_name = config.pool
    output_dir = Path(config.output_dir) if config.output_dir else OUTPUT_DIR
    top_n = config.top_n

    # ---- Grid search mode ----
    if config.grid:
        runner = GridSearchRunner(
            pool_name=pool_name,
            max_workers=config.max_workers,
            top_n=top_n,
            output_dir=output_dir,
        )

        try:
            if config.two_stage:
                results = runner.run_two_stage(recursive=config.recursive)
                grid_type = "two_stage_recursive" if config.recursive else "two_stage"
            else:
                results = runner.run_single_stage()
                grid_type = "single_stage"

            if not config.no_save:
                saved = runner.save_results(results, grid_type=grid_type)
                bt.logging.info(f"Results saved to {saved}")
        finally:
            runner.close()

        return

    # ---- Single analysis mode ----
    analyzer = StabilityAnalyzer(
        pool_name=pool_name,
        max_workers=config.max_workers,
        output_dir=output_dir,
    )

    try:
        if config.two_stage:
            # Build param dicts from CLI overrides
            core_params = {}
            ext_params = {}
            if config.min_interaction_weight is not None:
                core_params["min_interaction_weight"] = config.min_interaction_weight
            if config.min_tweets is not None:
                core_params["min_tweets"] = config.min_tweets
            if config.max_seed_accounts is not None:
                core_params["max_seed_accounts"] = config.max_seed_accounts
            if config.recursive:
                ext_params["recursive"] = True

            result = analyzer.run_two_stage_analysis(
                core_params=core_params,
                extended_params=ext_params,
                top_n=top_n,
            )
        else:
            params = {}
            if config.min_interaction_weight is not None:
                params["min_interaction_weight"] = config.min_interaction_weight
            if config.min_tweets is not None:
                params["min_tweets"] = config.min_tweets
            if config.max_seed_accounts is not None:
                params["max_seed_accounts"] = config.max_seed_accounts

            result = analyzer.run_analysis(params=params, top_n=top_n)

        # Save
        if not config.no_save:
            individual_dir = output_dir / "individual"
            individual_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = individual_dir / f"{pool_name}_{timestamp}.json"

            # Slim down for saving (drop large per-window data)
            save_data = dict(result)
            save_data["window_metrics"] = [
                {k: v for k, v in w.items() if k not in ("pagerank_scores", "k_cores")}
                for w in result.get("window_metrics", [])
            ]
            save_data["account_stability"] = {
                acc: {k: v for k, v in m.items() if k not in ("scores_by_window", "k_cores_by_window")}
                for acc, m in result.get("account_stability", {}).items()
            }

            with open(filepath, "w") as f:
                json.dump(save_data, f, indent=2, default=str)
            bt.logging.info(f"Results saved to {filepath}")

        # Print headline
        stability = result.get("stability", {}).get("overall", 0)
        accounts = len(result.get("social_map", {}).get("accounts", {}))
        bt.logging.info(f"Stability={stability:.3f}, accounts={accounts}")

    finally:
        analyzer.close()


if __name__ == "__main__":
    main()
