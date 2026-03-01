"""
Stability metrics for social network analysis.

Provides comprehensive metrics for analysing network stability
across time windows.
"""

import numpy as np
import networkx as nx
from typing import Dict, List, Optional
from scipy.stats import spearmanr


def calculate_window_metrics(
    adjacency_matrix: np.ndarray,
    usernames: List[str],
    scores: Dict[str, float],
    user_info_map: Optional[Dict[str, Dict]] = None,
) -> Dict:
    """
    Calculate comprehensive metrics for a single window's network.

    Args:
        adjacency_matrix: Weighted adjacency matrix
        usernames: List of usernames (order matches matrix)
        scores: PageRank scores by username
        user_info_map: Optional user info with followers counts

    Returns:
        Dict of metrics
    """
    n = len(usernames)

    if n == 0 or adjacency_matrix.size == 0:
        return {
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

    # Build directed graph
    G = nx.DiGraph()
    for i, from_user in enumerate(usernames):
        G.add_node(from_user)
        for j, to_user in enumerate(usernames):
            if adjacency_matrix[i, j] > 0:
                G.add_edge(from_user, to_user, weight=adjacency_matrix[i, j])

    # Basic counts
    edge_count = G.number_of_edges()
    max_edges = n * (n - 1) if n > 1 else 1
    density = edge_count / max_edges

    # Total edge weight
    total_edge_weight = float(np.sum(adjacency_matrix))

    # Average weighted degree
    weighted_degrees = dict(G.degree(weight="weight"))
    avg_weighted_degree = float(np.mean(list(weighted_degrees.values()))) if weighted_degrees else 0.0

    # K-core analysis (on undirected version)
    G_undirected = G.to_undirected()
    try:
        k_cores = nx.core_number(G_undirected)
        k_values = list(k_cores.values())
        avg_k_core = float(np.mean(k_values)) if k_values else 0.0
        max_k_core = max(k_values) if k_values else 0
    except Exception:
        k_cores = {}
        avg_k_core = 0.0
        max_k_core = 0

    # Clustering coefficient
    try:
        clustering = nx.average_clustering(G_undirected)
    except Exception:
        clustering = 0.0

    # Score distribution stats
    score_values = list(scores.values()) if scores else [0]

    return {
        "node_count": n,
        "edge_count": edge_count,
        "density": round(density, 4),
        "avg_k_core": round(avg_k_core, 2),
        "max_k_core": max_k_core,
        "clustering_coefficient": round(clustering, 4),
        "avg_weighted_degree": round(avg_weighted_degree, 2),
        "total_edge_weight": round(total_edge_weight, 2),
        "score_sum": round(sum(score_values), 2),
        "score_max": round(max(score_values), 2),
        "score_median": round(float(np.median(score_values)), 2),
        "pagerank_scores": scores,
        "k_cores": k_cores,
    }


# -----------------------------------------------------------------------
# Pairwise stability between adjacent windows
# -----------------------------------------------------------------------

def _calculate_pairwise_stability(
    window_a: Dict,
    window_b: Dict,
    top_n: int,
) -> Dict[str, float]:
    """
    Calculate stability metrics between two adjacent windows.

    Uses symmetric measures (min/max ratio) so direction doesn't matter.
    """
    # 1. Edge count stability
    edge_a, edge_b = window_a["edge_count"], window_b["edge_count"]
    edge_stability = min(edge_a, edge_b) / max(edge_a, edge_b) if max(edge_a, edge_b) > 0 else 0.0

    # 2. K-core stability
    kcore_a, kcore_b = window_a["avg_k_core"], window_b["avg_k_core"]
    kcore_stability = min(kcore_a, kcore_b) / max(kcore_a, kcore_b) if max(kcore_a, kcore_b) > 0 else 0.0

    # 3. Density stability
    density_a, density_b = window_a["density"], window_b["density"]
    density_stability = min(density_a, density_b) / max(density_a, density_b) if max(density_a, density_b) > 0 else 0.0

    # 4. Rank correlation of PageRank scores
    pr_a = window_a.get("pagerank_scores", {})
    pr_b = window_b.get("pagerank_scores", {})
    common_accounts = set(pr_a.keys()) & set(pr_b.keys())

    if len(common_accounts) > 10:
        scores_a = [pr_a[acc] for acc in common_accounts]
        scores_b = [pr_b[acc] for acc in common_accounts]
        try:
            rank_correlation, _ = spearmanr(scores_a, scores_b)
            if np.isnan(rank_correlation):
                rank_correlation = 0.0
        except Exception:
            rank_correlation = 0.0
    else:
        rank_correlation = 0.0

    # 5. Top-N overlap (Jaccard similarity)
    top_a = set(sorted(pr_a, key=pr_a.get, reverse=True)[:top_n])
    top_b = set(sorted(pr_b, key=pr_b.get, reverse=True)[:top_n])
    jaccard = len(top_a & top_b) / len(top_a | top_b) if (top_a and top_b) else 0.0

    # 6. Edge weight stability
    weight_a = window_a.get("total_edge_weight", 0)
    weight_b = window_b.get("total_edge_weight", 0)
    weight_stability = min(weight_a, weight_b) / max(weight_a, weight_b) if max(weight_a, weight_b) > 0 else 0.0

    return {
        "edge_stability": edge_stability,
        "kcore_stability": kcore_stability,
        "density_stability": density_stability,
        "rank_correlation": max(rank_correlation, 0.0),
        "top_n_jaccard": jaccard,
        "weight_stability": weight_stability,
        "top_n_overlap_count": len(top_a & top_b) if (top_a and top_b) else 0,
    }


# -----------------------------------------------------------------------
# Cross-window stability (aggregated)
# -----------------------------------------------------------------------

# Component weights (sum to 1.0)
WEIGHTS = {
    "edge_stability": 0.15,
    "kcore_stability": 0.20,
    "density_stability": 0.10,
    "rank_correlation": 0.25,
    "top_n_jaccard": 0.20,
    "weight_stability": 0.10,
}


def _weighted_overall(components: Dict[str, float]) -> float:
    """Compute weighted overall score from component values."""
    return sum(WEIGHTS[k] * components.get(k, 0.0) for k in WEIGHTS)


def calculate_cross_window_stability(
    window_metrics: List[Dict],
    top_n: int = 150,
) -> Dict:
    """
    Calculate stability by comparing all adjacent window pairs.

    Windows should be ordered most-recent-first.
    """
    if len(window_metrics) < 2:
        return {
            "overall": 0,
            "components": {},
            "interpretation": "Need at least 2 windows for stability analysis",
        }

    pairwise_results = [
        _calculate_pairwise_stability(window_metrics[i], window_metrics[i + 1], top_n)
        for i in range(len(window_metrics) - 1)
    ]

    def aggregate(key: str) -> float:
        return float(np.mean([p[key] for p in pairwise_results]))

    components = {k: round(aggregate(k), 3) for k in WEIGHTS}
    overall = round(_weighted_overall(components), 3)

    min_pair_scores = [round(_weighted_overall(p), 3) for p in pairwise_results]

    interpretation = _interpret_stability(overall, components)

    # Top-N overlap between newest and oldest windows
    recent_pr = window_metrics[0].get("pagerank_scores", {})
    oldest_pr = window_metrics[-1].get("pagerank_scores", {})
    recent_top = set(sorted(recent_pr, key=recent_pr.get, reverse=True)[:top_n])
    oldest_top = set(sorted(oldest_pr, key=oldest_pr.get, reverse=True)[:top_n])

    return {
        "overall": overall,
        "components": components,
        "interpretation": interpretation,
        "top_n_overlap": len(recent_top & oldest_top),
        "windows_analyzed": len(window_metrics),
        "pairs_compared": len(pairwise_results),
        "min_pair_stability": min(min_pair_scores),
        "max_pair_stability": max(min_pair_scores),
        "pairwise_details": [
            {f"pair_{i}_{i+1}": {k: round(v, 3) for k, v in p.items()}}
            for i, p in enumerate(pairwise_results)
        ],
    }


def _interpret_stability(score: float, components: Dict) -> str:
    """Generate human-readable interpretation of stability."""
    issues = []

    if components.get("edge_stability", 0) < 0.5:
        issues.append(f"Low edge stability ({components['edge_stability']:.0%})")
    if components.get("kcore_stability", 0) < 0.6:
        issues.append(f"Low k-core stability ({components['kcore_stability']:.0%})")
    if components.get("rank_correlation", 0) < 0.5:
        issues.append(f"Low rank correlation ({components['rank_correlation']:.2f})")
    if components.get("top_n_jaccard", 0) < 0.5:
        issues.append(f"Low top-N overlap ({components['top_n_jaccard']:.0%})")
    if components.get("weight_stability", 0) < 0.5:
        issues.append(f"Low weight stability ({components['weight_stability']:.0%})")

    if score >= 0.75:
        summary = "STABLE: Current selection represents a persistent structural core."
    elif score >= 0.55:
        summary = "MODERATE: Some stability, but window-to-window variance detected."
    else:
        summary = "UNSTABLE: Current selection may not persist."

    if issues:
        summary += "\n  Issues:\n    - " + "\n    - ".join(issues)
    return summary


# -----------------------------------------------------------------------
# Per-window summary table
# -----------------------------------------------------------------------

def calculate_per_window_summary(window_metrics: List[Dict]) -> Dict:
    """Create a summary table of metrics across all windows."""
    if not window_metrics:
        return {}
    return {
        "window_labels": [w.get("window_label", f"Window {i}") for i, w in enumerate(window_metrics)],
        "node_counts": [w["node_count"] for w in window_metrics],
        "edge_counts": [w["edge_count"] for w in window_metrics],
        "densities": [w["density"] for w in window_metrics],
        "avg_k_cores": [w["avg_k_core"] for w in window_metrics],
        "max_k_cores": [w["max_k_core"] for w in window_metrics],
        "clustering_coefficients": [w["clustering_coefficient"] for w in window_metrics],
        "total_edge_weights": [w["total_edge_weight"] for w in window_metrics],
    }


# -----------------------------------------------------------------------
# Per-account stability
# -----------------------------------------------------------------------

def calculate_account_stability(
    window_metrics: List[Dict],
    accounts: List[str],
) -> Dict[str, Dict]:
    """
    Calculate per-account stability metrics across windows.

    Returns:
        Dict mapping username -> stability metrics
    """
    account_stability = {}

    for account in accounts:
        scores = []
        k_cores = []
        present_count = 0

        for window in window_metrics:
            pr_scores = window.get("pagerank_scores", {})
            k_core_vals = window.get("k_cores", {})

            scores.append(pr_scores.get(account, 0.0))
            if account in pr_scores:
                present_count += 1
            k_cores.append(k_core_vals.get(account, 0))

        presence_ratio = present_count / len(window_metrics) if window_metrics else 0.0

        nonzero_scores = [s for s in scores if s > 0]
        score_cv = (
            float(np.std(nonzero_scores) / np.mean(nonzero_scores))
            if len(nonzero_scores) > 1
            else 0.0
        )

        avg_k_core = float(np.mean(k_cores)) if k_cores else 0.0
        min_k_core = min(k_cores) if k_cores else 0
        max_k_core_val = max(k_cores) if k_cores else 1

        account_stability[account] = {
            "presence_ratio": round(presence_ratio, 2),
            "score_cv": round(score_cv, 3),
            "avg_k_core": round(avg_k_core, 2),
            "min_k_core": min_k_core,
            "scores_by_window": scores,
            "k_cores_by_window": k_cores,
            "stability_score": round(
                presence_ratio * (1 - min(score_cv, 1)) * (min_k_core / max(max_k_core_val, 1)),
                3,
            ),
        }

    return account_stability
