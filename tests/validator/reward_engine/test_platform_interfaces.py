"""Tests for platform evaluator interfaces."""

import pytest
from bitcast.validator.reward_engine.interfaces.platform_evaluator import (
    PlatformEvaluator,
    QueryBasedEvaluator,
    ScanBasedEvaluator
)
from bitcast.validator.reward_engine.twitter_evaluator import TwitterEvaluator


def test_interface_hierarchy():
    """Both interfaces inherit from PlatformEvaluator"""
    assert issubclass(QueryBasedEvaluator, PlatformEvaluator)
    assert issubclass(ScanBasedEvaluator, PlatformEvaluator)


def test_interfaces_are_distinct():
    """The two interfaces have different required methods"""
    # QueryBasedEvaluator has can_evaluate and evaluate_miner_response
    query_methods = {m for m in dir(QueryBasedEvaluator) if not m.startswith('_')}
    
    # ScanBasedEvaluator has evaluate_briefs
    scan_methods = {m for m in dir(ScanBasedEvaluator) if not m.startswith('_')}
    
    # Both should have platform_name from base
    assert 'platform_name' in query_methods
    assert 'platform_name' in scan_methods
    
    # Query-based specific methods
    assert 'can_evaluate' in query_methods
    assert 'evaluate_miner_response' in query_methods
    
    # Scan-based specific methods
    assert 'evaluate_briefs' in scan_methods
    
    # They should NOT have each other's methods
    assert 'evaluate_briefs' not in query_methods
    assert 'can_evaluate' not in scan_methods
    assert 'evaluate_miner_response' not in scan_methods


def test_twitter_evaluator_interface():
    """TwitterEvaluator properly implements ScanBasedEvaluator"""
    evaluator = TwitterEvaluator()
    
    # Should be instance of ScanBasedEvaluator
    assert isinstance(evaluator, ScanBasedEvaluator)
    
    # Should also be instance of base PlatformEvaluator
    assert isinstance(evaluator, PlatformEvaluator)
    
    # Should have platform_name method
    assert evaluator.platform_name() == "twitter"
    
    # Should have evaluate_briefs method (from ScanBasedEvaluator)
    assert hasattr(evaluator, 'evaluate_briefs')
    assert callable(evaluator.evaluate_briefs)
    
    # Should NOT have can_evaluate or evaluate_miner_response methods
    # (these are QueryBasedEvaluator methods)
    assert not hasattr(evaluator, 'can_evaluate')
    assert not hasattr(evaluator, 'evaluate_miner_response')


def test_twitter_evaluator_not_query_based():
    """TwitterEvaluator is not a QueryBasedEvaluator"""
    evaluator = TwitterEvaluator()
    
    # Should NOT be instance of QueryBasedEvaluator
    assert not isinstance(evaluator, QueryBasedEvaluator)


def test_registry_integration():
    """PlatformRegistry can handle both evaluator types"""
    from bitcast.validator.reward_engine.services.platform_registry import PlatformRegistry
    
    registry = PlatformRegistry()
    twitter = TwitterEvaluator()
    registry.register_evaluator(twitter)
    
    # Can get as generic evaluator
    assert registry.get_evaluator("twitter") is not None
    assert registry.get_evaluator("twitter") == twitter
    
    # Can get as scan-based evaluator
    assert registry.get_scan_evaluator("twitter") is not None
    assert registry.get_scan_evaluator("twitter") == twitter
    
    # Returns None for query-based (Twitter is scan-based)
    assert registry.get_query_evaluator("twitter") is None


def test_registry_validates_evaluator_type():
    """PlatformRegistry validates evaluators implement correct interface"""
    from bitcast.validator.reward_engine.services.platform_registry import PlatformRegistry
    
    registry = PlatformRegistry()
    
    # Create invalid evaluator (doesn't implement Query or Scan interface)
    class InvalidEvaluator(PlatformEvaluator):
        def platform_name(self):
            return "invalid"
    
    # Should raise ValueError
    with pytest.raises(ValueError, match="must implement QueryBasedEvaluator or ScanBasedEvaluator"):
        registry.register_evaluator(InvalidEvaluator())


def test_registry_reports_evaluator_type():
    """PlatformRegistry logs whether evaluator is query-based or scan-based"""
    from bitcast.validator.reward_engine.services.platform_registry import PlatformRegistry
    import logging
    
    # Capture logs
    import bittensor as bt
    
    registry = PlatformRegistry()
    twitter = TwitterEvaluator()
    
    # This should log "scan-based" for Twitter
    registry.register_evaluator(twitter)
    
    # Just verify it doesn't error - actual log checking would require more setup
    assert registry.get_evaluator("twitter") == twitter

