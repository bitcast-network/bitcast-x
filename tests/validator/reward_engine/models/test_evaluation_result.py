"""Tests for platform-agnostic evaluation result models."""

import pytest
from bitcast.validator.reward_engine.models.evaluation_result import (
    AccountResult,
    EvaluationResult,
    EvaluationResultCollection
)


class TestAccountResult:
    """Test AccountResult platform-agnostic model."""
    
    def test_content_field_twitter(self):
        """Test AccountResult with Twitter content."""
        result = AccountResult(
            account_id="test_user",
            platform_data={'username': 'test_user'},
            content={'tweet1': {'text': 'Test tweet'}},
            scores={'brief_002': 5.3},
            performance_stats={'platform': 'twitter'},
            success=True
        )
        
        assert result.account_id == "test_user"
        assert 'tweet1' in result.content
        assert result.scores['brief_002'] == 5.3
    
    def test_create_error_result(self):
        """Test create_error_result class method."""
        briefs = [{'id': 'brief_001'}, {'id': 'brief_002'}]
        
        result = AccountResult.create_error_result(
            account_id="error_account",
            error_message="Test error",
            briefs=briefs
        )
        
        assert result.account_id == "error_account"
        assert result.success is False
        assert result.error_message == "Test error"
        assert result.content == {}
        assert result.scores['brief_001'] == 0.0


class TestEvaluationResult:
    """Test EvaluationResult model."""
    
    def test_create_and_add_accounts(self):
        """Test creating EvaluationResult and adding accounts."""
        result = EvaluationResult(uid=42, platform="twitter")
        
        account = AccountResult(
            account_id="test_account",
            platform_data={},
            content={},
            scores={'brief_001': 5.0},
            performance_stats={},
            success=True
        )
        
        result.add_account_result("test_account", account)
        result.aggregated_scores = {'brief_001': 100.0}
        
        assert result.uid == 42
        assert result.platform == "twitter"
        assert 'test_account' in result.account_results
        assert result.aggregated_scores['brief_001'] == 100.0


class TestEvaluationResultCollection:
    """Test EvaluationResultCollection."""
    
    def test_collection_operations(self):
        """Test adding results to collection."""
        collection = EvaluationResultCollection()
        
        result1 = EvaluationResult(uid=10, platform="twitter")
        result2 = EvaluationResult(uid=20, platform="twitter")
        
        collection.add_result(10, result1)
        collection.add_result(20, result2)
        
        assert len(collection.results) == 2
        assert collection.get_result(10).uid == 10
        assert collection.get_result(20).uid == 20

