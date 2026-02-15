"""
Utility functions for adjacency matrix serialization.

Provides compact edge format for efficient storage and transmission,
reducing file sizes by ~700-1000x for sparse social network matrices.

Format:
{
    "usernames": [...],
    "format_version": "2.0",
    "adjacency_edges": {"sources": [...], "targets": [...], "weights": [...]},
    "adjacency_shape": [n, n],
    "relationship_edges": {"sources": [...], "targets": [...], "weights": [...]},
    "relationship_shape": [n, n]
}
"""

import numpy as np
from typing import Dict, Any, Tuple, List, Optional
from scipy import sparse


def dense_to_compact_edges(matrix: np.ndarray) -> Dict[str, List]:
    """
    Convert a dense adjacency matrix to compact edge format.
    
    Stores only non-zero edges as parallel arrays for efficient storage.
    Uses numpy for 30x faster performance on large matrices.
    """
    # Use numpy for efficient sparse extraction
    rows, cols = np.nonzero(matrix)
    vals = matrix[rows, cols]
    
    return {
        'sources': rows.tolist(),
        'targets': cols.tolist(),
        'weights': vals.tolist()
    }


def compact_edges_to_dense(
    sources: List[int],
    targets: List[int],
    weights: List[float],
    shape: Tuple[int, int]
) -> np.ndarray:
    """Convert compact edge format back to dense matrix."""
    matrix = np.zeros(shape, dtype=np.float64)
    for s, t, w in zip(sources, targets, weights):
        matrix[s, t] = w
    return matrix


def compact_edges_to_sparse_csr(
    sources: List[int],
    targets: List[int],
    weights: List[float],
    shape: Tuple[int, int]
) -> sparse.csr_matrix:
    """Convert compact edges to scipy CSR sparse matrix for efficient random access."""
    return sparse.csr_matrix(
        (weights, (sources, targets)),
        shape=shape,
        dtype=np.float64
    )


def serialize_adjacency_matrix(
    adjacency_matrix: np.ndarray,
    relationship_matrix: Optional[np.ndarray] = None,
    usernames: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Serialize adjacency matrix(es) to compact JSON format."""
    result = {}
    
    if usernames is not None:
        result['usernames'] = usernames
    
    result['adjacency_edges'] = dense_to_compact_edges(adjacency_matrix)
    result['adjacency_shape'] = list(adjacency_matrix.shape)
    
    if relationship_matrix is not None:
        result['relationship_edges'] = dense_to_compact_edges(relationship_matrix)
        result['relationship_shape'] = list(relationship_matrix.shape)
    
    if metadata:
        result.update(metadata)
    
    result['format_version'] = '2.0'
    
    return result


def deserialize_adjacency_matrix(data: Dict[str, Any]) -> Tuple[np.ndarray, Optional[np.ndarray], List[str]]:
    """Deserialize compact adjacency matrix format (v2.0 only)."""
    if data.get('format_version') != '2.0':
        raise ValueError(f"Unsupported format version: {data.get('format_version')}")
    
    usernames = data.get('usernames', [])
    
    # Reconstruct adjacency matrix
    adj_edges = data['adjacency_edges']
    adj_shape = tuple(data['adjacency_shape'])
    adjacency_matrix = compact_edges_to_dense(
        adj_edges['sources'],
        adj_edges['targets'],
        adj_edges['weights'],
        adj_shape
    )
    
    # Reconstruct relationship matrix if present
    relationship_matrix = None
    if 'relationship_edges' in data:
        rel_edges = data['relationship_edges']
        rel_shape = tuple(data['relationship_shape'])
        relationship_matrix = compact_edges_to_dense(
            rel_edges['sources'],
            rel_edges['targets'],
            rel_edges['weights'],
            rel_shape
        )
    
    return adjacency_matrix, relationship_matrix, usernames


def load_relationship_scores_sparse(data: Dict[str, Any]) -> Tuple[Optional[sparse.csr_matrix], List[str]]:
    """
    Load relationship scores as sparse matrix for efficient memory and random access.
    
    Returns a scipy CSR matrix instead of dense, saving memory for large sparse networks.
    Random access (matrix[i, j]) works efficiently on CSR format.
    """
    if data.get('format_version') != '2.0':
        raise ValueError(f"Unsupported format version: {data.get('format_version')}")
    
    usernames = data.get('usernames', [])
    
    if 'relationship_edges' not in data:
        return None, usernames
    
    rel_edges = data['relationship_edges']
    rel_shape = tuple(data['relationship_shape'])
    
    relationship_matrix = compact_edges_to_sparse_csr(
        rel_edges['sources'],
        rel_edges['targets'],
        rel_edges['weights'],
        rel_shape
    )
    
    return relationship_matrix, usernames
