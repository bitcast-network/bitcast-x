"""
Grid search runner for stability analysis.

Sweeps parameter combinations, runs the stability pipeline for each,
and collects results.  Output is written to the stability output
directory — never to ``social_maps/`` and never published.
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
    GRID_DEFINITIONS,
    SINGLE_STAGE_GRID,
    OUTPUT_DIR,
    TOP_N_ACCOUNTS,
)


class GridSearchRunner:
    """
    Runs parameter grid search for a single pool.

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

    def run_two_stage(
        self,
        *,
        recursive: bool = False,
        core_grid: Optional[Dict[str, List]] = None,
        extended_grid: Optional[Dict[str, List]] = None,
    ) -> List[Dict]:
        """
        Run a two-stage grid search.

        If *core_grid* / *extended_grid* are not supplied they are loaded
        from ``config.GRID_DEFINITIONS[pool_name]``.

        Returns:
            List of result dicts, one per combination.
        """
        defaults = GRID_DEFINITIONS.get(self.pool_name, GRID_DEFINITIONS.get("bittensor", {}))
        core_grid = core_grid or defaults.get("core", {})
        ext_key = "extended_recursive" if recursive else "extended"
        extended_grid = extended_grid or defaults.get(ext_key, {})

        combinations = self._build_combinations(core_grid, extended_grid)
        total = len(combinations)

        bt.logging.info("=" * 80)
        bt.logging.info(f"TWO-STAGE GRID SEARCH — {self.pool_name}")
        bt.logging.info(f"Combinations: {total}  (recursive={recursive})")
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

            result = self._run_two_stage_combination(core_params, ext_params)
            results.append(result)

        self._log_summary(results)
        return results

    def run_single_stage(
        self,
        *,
        param_grid: Optional[Dict[str, List]] = None,
    ) -> List[Dict]:
        """
        Run a single-stage grid search.

        Returns:
            List of result dicts, one per combination.
        """
        param_grid = param_grid or SINGLE_STAGE_GRID
        combinations = self._build_flat_combinations(param_grid)
        total = len(combinations)

        bt.logging.info("=" * 80)
        bt.logging.info(f"SINGLE-STAGE GRID SEARCH — {self.pool_name}")
        bt.logging.info(f"Combinations: {total}")
        bt.logging.info(f"Grid: {param_grid}")
        bt.logging.info("=" * 80)

        results: List[Dict] = []
        for i, params in enumerate(combinations):
            bt.logging.info("-" * 80)
            bt.logging.info(f"Combination {i+1}/{total}: {params}")
            bt.logging.info("-" * 80)

            result = self._run_single_combination(params)
            results.append(result)

        self._log_summary(results)
        return results

    # ------------------------------------------------------------------
    # Result persistence
    # ------------------------------------------------------------------

    def save_results(
        self,
        results: List[Dict],
        grid_type: str = "two_stage",
    ) -> Path:
        """
        Save grid search results to the output directory.

        Returns the path to the saved file.
        """
        grid_dir = self.output_dir / "grid_searches" / grid_type
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

    def _run_two_stage_combination(
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

    def _run_single_combination(self, params: Dict) -> Dict:
        try:
            result = self.analyzer.run_analysis(
                params=params,
                top_n=self.top_n,
            )
            result["parameters"] = params
            return result
        except Exception as e:
            bt.logging.error(f"Combination failed: {e}")
            traceback.print_exc()
            return {"parameters": params, "error": str(e)}

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

    @staticmethod
    def _build_flat_combinations(
        param_grid: Dict[str, List],
    ) -> List[Dict]:
        names = list(param_grid.keys())
        combos = list(product(*param_grid.values()))
        return [dict(zip(names, c)) for c in combos]

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

            # Two-stage extras
            meta = r.get("metadata", {})
            if meta.get("two_stage") or meta.get("core_accounts_count"):
                entry["core_accounts"] = meta.get("core_accounts_count", 0)
                entry["extended_accounts"] = (
                    len(r["social_map"]["accounts"]) - meta.get("core_accounts_count", 0)
                )
                ext = r.get("parameters", {}).get("extended", {})
                if ext.get("recursive"):
                    entry["recursive"] = True
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
            params = r.get("parameters", {})
            core_ct = r.get("metadata", {}).get("core_accounts_count", "")
            core_str = f", core={core_ct}" if core_ct else ""
            bt.logging.info(
                f"  #{rank}: stability={stab:.3f}, accounts={accts}{core_str}"
            )
            bt.logging.info(f"         params={params}")

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
