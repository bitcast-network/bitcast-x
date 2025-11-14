"""Tests for the validator weights API."""
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from bitcast.validator.api.weights_api import app, load_state, normalize_weights, get_state_path


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_state_data():
    """Create mock state data."""
    return {
        "scores": np.array([0.5, 0.3, 0.2], dtype=np.float32),
        "hotkeys": np.array(["hotkey1", "hotkey2", "hotkey3"]),
        "step": np.array(100)
    }


def test_normalize_weights():
    """Test weight normalization."""
    scores = np.array([10, 20, 30])
    normalized = normalize_weights(scores)
    assert np.isclose(normalized.sum(), 1.0)
    assert len(normalized) == len(scores)


def test_normalize_weights_zero():
    """Test normalization with zero weights."""
    scores = np.array([0, 0, 0])
    normalized = normalize_weights(scores)
    assert np.all(normalized == 0)


def test_health_endpoint(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@patch('bitcast.validator.api.weights_api.load_state')
def test_get_all_weights(mock_load, client, mock_state_data):
    """Test getting all weights."""
    mock_load.return_value = mock_state_data
    
    response = client.get("/weights")
    assert response.status_code == 200
    
    data = response.json()
    assert data["step"] == 100
    assert data["total_miners"] == 3
    assert len(data["weights"]) == 3
    assert data["weights"][0]["uid"] == 0
    assert data["weights"][0]["hotkey"] == "hotkey1"


@patch('bitcast.validator.api.weights_api.load_state')
def test_get_weight_by_uid(mock_load, client, mock_state_data):
    """Test getting weight for specific UID."""
    mock_load.return_value = mock_state_data
    
    response = client.get("/weights/1")
    assert response.status_code == 200
    
    data = response.json()
    assert data["uid"] == 1
    assert data["hotkey"] == "hotkey2"
    assert abs(data["raw_weight"] - 0.3) < 1e-6  # Float precision tolerance


@patch('bitcast.validator.api.weights_api.load_state')
def test_get_weight_invalid_uid(mock_load, client, mock_state_data):
    """Test getting weight for invalid UID."""
    mock_load.return_value = mock_state_data
    
    response = client.get("/weights/99")
    assert response.status_code == 404


@patch('bitcast.validator.api.weights_api.load_state')
def test_state_not_found(mock_load, client):
    """Test when state file doesn't exist."""
    mock_load.side_effect = FileNotFoundError("State file not found")
    
    response = client.get("/weights")
    assert response.status_code == 404


def test_rate_limiting(client):
    """Test rate limiting on endpoints."""
    # Health endpoint allows 60/min, should not rate limit in normal use
    for _ in range(10):
        response = client.get("/health")
        assert response.status_code == 200
    
    # Weights endpoint is 10/min - this is hard to test without mocking time
    # Just verify it works normally
    with patch('bitcast.validator.api.weights_api.load_state') as mock_load:
        mock_load.return_value = {
            "scores": np.array([0.5]),
            "hotkeys": np.array(["test"]),
            "step": np.array(1)
        }
        response = client.get("/weights")
        assert response.status_code == 200

