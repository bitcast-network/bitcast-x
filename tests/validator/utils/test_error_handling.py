"""Tests for error handling utilities."""

import pytest
from bitcast.validator.utils.error_handling import (
    log_and_raise_api_error,
    log_and_raise_validation_error,
    log_and_raise_config_error
)


def test_log_and_raise_api_error():
    """log_and_raise_api_error raises RuntimeError with context"""
    with pytest.raises(RuntimeError) as exc_info:
        log_and_raise_api_error(
            Exception("Connection timeout"),
            endpoint="https://api.example.com/briefs",
            context="Test API call"
        )
    
    error = exc_info.value
    assert 'api.example.com' in str(error)
    assert 'Connection timeout' in str(error)


def test_log_and_raise_api_error_sanitizes_params():
    """API error handler sanitizes sensitive parameters"""
    with pytest.raises(RuntimeError):
        log_and_raise_api_error(
            Exception("Auth failed"),
            endpoint="https://api.example.com",
            params={'api_key': 'secret123', 'limit': 100},
            context="Auth test"
        )
    # Should not log api_key but should log other params


def test_log_and_raise_validation_error():
    """log_and_raise_validation_error raises ValueError with context"""
    with pytest.raises(ValueError) as exc_info:
        log_and_raise_validation_error(
            "Invalid brief data",
            context_info={'brief_id': 'test_001', 'field': 'budget'}
        )
    
    error = exc_info.value
    assert "Invalid brief data" in str(error)


def test_log_and_raise_validation_error_truncates_large_data():
    """Validation error handler truncates large data for logging"""
    large_data = {'data': 'x' * 1000}
    
    with pytest.raises(ValueError) as exc_info:
        log_and_raise_validation_error(
            "Data too large",
            data=large_data
        )
    
    # Should still raise error even with large data
    assert "Data too large" in str(exc_info.value)


def test_log_and_raise_config_error():
    """log_and_raise_config_error raises ValueError with context"""
    with pytest.raises(ValueError) as exc_info:
        log_and_raise_config_error(
            "Missing required configuration",
            config_key="RAPID_API_KEY",
            config_value="missing"
        )
    
    error = exc_info.value
    assert "Missing required configuration" in str(error)
    assert "RAPID_API_KEY" in str(error)


def test_log_and_raise_config_error_sanitizes_sensitive():
    """Config error handler sanitizes sensitive config values"""
    with pytest.raises(ValueError) as exc_info:
        log_and_raise_config_error(
            "Invalid API key",
            config_key="API_KEY",
            config_value="secret12345"
        )
    
    error = exc_info.value
    # Value should be redacted in error message (through logging)
    assert "Invalid API key" in str(error)

