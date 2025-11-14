"""
Weight Copy Mode

This module provides lightweight validator operation by fetching weights
from a primary validator instead of running full validation logic.
"""

from bitcast.validator.weight_copy.wc_client import WeightCopyClient
from bitcast.validator.weight_copy.wc_forward import forward_weight_copy

__all__ = ['WeightCopyClient', 'forward_weight_copy']

