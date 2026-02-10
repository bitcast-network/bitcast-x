"""
Grid search runner for stability analysis.

Sweeps parameter combinations for the two-stage recursive discovery
pipeline, runs the stability analysis for each, and collects results.
Output is written to the stability output directory — never to
``social_maps/`` and never published.
"""

import json
import traceback
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Optional

import bittensor as bt

from .analyzer import StabilityAnalyzer
from .config import (
    GRID,
    OUTPUT_DIR,
    TOP_N_ACCOUNTS,
)


class GridSearchRunner:
    """
    Runs a parameter grid search for a single pool.

    Shares one ``StabilityAnalyzer`` (and therefore one ``TwitterClient``)
    across all combinations so that the tweet cache is reused.
    """

    def __init__(
        self,
        pool_name: str,
        *,
        max_workers: int = 10,
        top_n: int = TOP_N_ACCOUNTS,
        output_dir: Optional[Path] = None,
    ):
        self.pool_name = pool_name
        self.top_n = top_n
        self.output_dir = output_dir or OUTPUT_DIR
        self.analyzer = StabilityAnalyzer(
            pool_name=pool_name,
            max_workers=max_workers,
            output_dir=self.output_dir,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        core_grid: Optional[Dict[str, List]] = None,
        extended_grid: Optional[Dict[str, List]] = None,
    ) -> List[Dict]:
        """
        Run the two-stage recursive grid search.

        If *core_grid* / *extended_grid* are not supplied they are loaded
        from ``config.GRID``.

        Returns:
            List of result dicts, one per combination.
        """
        core_grid = core_grid or GRID["core"]
        extended_grid = extended_grid or GRID["extended"]

        combinations = self._build_combinations(core_grid, extended_grid)
        total = len(combinations)

        bt.logging.info("=" * 80)
        bt.logging.info(f"GRID SEARCH — {self.pool_name}")
        bt.logging.info(f"Combinations: {total}")
        bt.logging.info(f"Core grid:     {core_grid}")
        bt.logging.info(f"Extended grid: {extended_grid}")
        bt.logging.info("=" * 80)

        results: List[Dict] = []
        for i, (core_params, ext_params) in enumerate(combinations):
            bt.logging.info("-" * 80)
            bt.logging.info(f"Combination {i+1}/{total}")
            bt.logging.info(f"  Core:     {core_params}")
            bt.logging.info(f"  Extended: {ext_params}")
            bt.logging.info("-" * 80)

            result = self._run_combination(core_params, ext_params)
            results.append(result)

        self._log_summary(results)
        return results

    # ------------------------------------------------------------------
    # Result persistence
    # ------------------------------------------------------------------

    def save_results(self, results: List[Dict]) -> Path:
        """
        Save grid search results to the output directory.

        Returns the path to the saved file.
        """
        grid_dir = self.output_dir / "grid_searches"
        grid_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.pool_name}_{timestamp}.json"
        filepath = grid_dir / filename

        summary = self._build_summary(results)

        with open(filepath, "w") as f:
            json.dump(summary, f, indent=2, default=str)

        bt.logging.info(f"Grid results saved to {filepath}")
        return filepath

    # ------------------------------------------------------------------
    # Private: running combinations
    # ------------------------------------------------------------------

    def _run_combination(
        self,
        core_params: Dict,
        ext_params: Dict,
    ) -> Dict:
        try:
            result = self.analyzer.run_two_stage_analysis(
                core_params=core_params,
                extended_params=ext_params,
                top_n=self.top_n,
            )
            result["parameters"] = {"core": core_params, "extended": ext_params}
            return result
        except Exception as e:
            bt.logging.error(f"Combination failed: {e}")
            traceback.print_exc()
            return {
                "parameters": {"core": core_params, "extended": ext_params},
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Private: combination generation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_combinations(
        core_grid: Dict[str, List],
        extended_grid: Dict[str, List],
    ) -> List[tuple]:
        """
        Cartesian product of core x extended parameter combinations.

        Returns list of (core_params_dict, extended_params_dict) tuples.
        """
        core_names = list(core_grid.keys())
        core_combos = list(product(*core_grid.values()))

        ext_names = list(extended_grid.keys())
        ext_combos = list(product(*extended_grid.values()))

        return [
            (
                dict(zip(core_names, cc)),
                dict(zip(ext_names, ec)),
            )
            for cc, ec in product(core_combos, ext_combos)
        ]

    # ------------------------------------------------------------------
    # Private: summary / logging
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary(results: List[Dict]) -> List[Dict]:
        """Distil full results into a compact JSON-friendly list."""
        summary: List[Dict] = []
        for r in results:
            if "error" in r:
                summary.append({
                    "parameters": r["parameters"],
                    "error": r["error"],
                })
                continue

            entry: Dict[str, Any] = {
                "parameters": r["parameters"],
                "stability_score": r["stability"]["overall"],
                "accounts_discovered": len(r["social_map"]["accounts"]),
                "components": r["stability"]["components"],
            }

            meta = r.get("metadata", {})
            entry["core_accounts"] = meta.get("core_accounts_count", 0)
            entry["extended_accounts"] = (
                len(r["social_map"]["accounts"]) - meta.get("core_accounts_count", 0)
            )
            ext = r.get("parameters", {}).get("extended", {})
            entry["max_iterations"] = ext.get("max_iterations")

            summary.append(entry)
        return summary

    @staticmethod
    def _log_summary(results: List[Dict]) -> None:
        """Print a ranked summary table to the log."""
        bt.logging.info("")
        bt.logging.info("=" * 80)
        bt.logging.info("GRID SEARCH SUMMARY")
        bt.logging.info("=" * 80)

        valid = [r for r in results if "error" not in r]
        errors = [r for r in results if "error" in r]

        # Sort valid results by stability descending
        valid.sort(
            key=lambda r: r.get("stability", {}).get("overall", 0),
            reverse=True,
        )

        for rank, r in enumerate(valid, 1):
            stab = r["stability"]["overall"]
            accts = len(r["social_map"]["accounts"])
            core_ct = r.get("metadata", {}).get("core_accounts_count", "")
            core_str = f", core={core_ct}" if core_ct else ""
            bt.logging.info(
                f"  #{rank}: stability={stab:.3f}, accounts={accts}{core_str}"
            )
            bt.logging.info(f"         params={r.get('parameters', {})}")

        for r in errors:
            bt.logging.info(f"  ERROR: {r['parameters']} — {r['error']}")

        if valid:
            best = valid[0]
            bt.logging.info("")
            bt.logging.info(
                f"Best: stability={best['stability']['overall']:.3f} | "
                f"params={best.get('parameters', {})}"
            )
        bt.logging.info("=" * 80)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        self.analyzer.close()
