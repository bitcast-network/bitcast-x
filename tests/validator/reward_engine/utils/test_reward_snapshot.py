"""Tests for reward snapshot utilities."""

import json
import pytest
from pathlib import Path
from datetime import datetime
from bitcast.validator.reward_engine.utils.reward_snapshot import (
    save_reward_snapshot,
    load_reward_snapshot
)


@pytest.fixture
def sample_snapshot_data():
    """Sample snapshot data for testing."""
    return {
        'brief_id': 'test_brief_001',
        'pool_name': 'test_pool',
        'created_at': '2025-11-09T12:00:00+00:00',
        'tweet_rewards': [
            {
                'tweet_id': '123', 'author': 'user1', 'uid': 1, 'score': 0.85, 'total_usd': 50.25,
                'favorite_count': 10, 'retweet_count': 5, 'reply_count': 3, 'quote_count': 2, 'bookmark_count': 1,
                'retweets': ['acc1'], 'quotes': [], 'created_at': '2025-11-01T10:00:00Z', 'lang': 'en'
            },
            {
                'tweet_id': '456', 'author': 'user2', 'uid': 1, 'score': 0.75, 'total_usd': 50.25,
                'favorite_count': 0, 'retweet_count': 0, 'reply_count': 0, 'quote_count': 0, 'bookmark_count': 0,
                'retweets': [], 'quotes': [], 'created_at': '2025-11-02T11:00:00Z', 'lang': 'en'
            },
            {
                'tweet_id': '789', 'author': 'user3', 'uid': 2, 'score': 1.20, 'total_usd': 200.75,
                'favorite_count': 20, 'retweet_count': 10, 'reply_count': 5, 'quote_count': 3, 'bookmark_count': 2,
                'retweets': ['acc1', 'acc2'], 'quotes': ['acc3'], 'created_at': '2025-11-03T12:00:00Z', 'lang': 'en'
            },
            {
                'tweet_id': '101', 'author': 'user4', 'uid': 3, 'score': 0.60, 'total_usd': 50.25,
                'favorite_count': 5, 'retweet_count': 2, 'reply_count': 1, 'quote_count': 0, 'bookmark_count': 0,
                'retweets': ['acc2'], 'quotes': [], 'created_at': '2025-11-04T13:00:00Z', 'lang': 'und'
            }
        ]
    }


def test_save_reward_snapshot(sample_snapshot_data):
    """Test saving a reward snapshot."""
    import shutil
    
    # Save snapshot (uses real filesystem)
    brief_id = sample_snapshot_data['brief_id']
    pool_name = sample_snapshot_data['pool_name']
    
    snapshot_file = save_reward_snapshot(brief_id, pool_name, sample_snapshot_data)
    
    try:
        # Verify file was created
        assert Path(snapshot_file).exists()
        
        # Verify contents
        with open(snapshot_file, 'r') as f:
            loaded_data = json.load(f)
        
        assert loaded_data['brief_id'] == brief_id
        assert loaded_data['pool_name'] == pool_name
        assert loaded_data['tweet_rewards'] == sample_snapshot_data['tweet_rewards']
        assert len(loaded_data['tweet_rewards']) == 4
        
        # Verify score field is present in each tweet
        for tweet in loaded_data['tweet_rewards']:
            assert 'score' in tweet
    
    finally:
        # Cleanup
        snapshot_dir = Path(snapshot_file).parent
        shutil.rmtree(snapshot_dir, ignore_errors=True)


def test_load_reward_snapshot(sample_snapshot_data):
    """Test loading a reward snapshot."""
    import shutil
    
    # Create snapshot using save function
    brief_id = sample_snapshot_data['brief_id']
    pool_name = sample_snapshot_data['pool_name']
    
    snapshot_file = save_reward_snapshot(brief_id, pool_name, sample_snapshot_data)
    
    try:
        # Load snapshot
        data, file_path = load_reward_snapshot(brief_id, pool_name)
        
        # Verify data
        assert data['brief_id'] == sample_snapshot_data['brief_id']
        assert data['tweet_rewards'] == sample_snapshot_data['tweet_rewards']
        assert len(data['tweet_rewards']) == 4
        assert Path(file_path).exists()
        
        # Verify score field is present
        for tweet in data['tweet_rewards']:
            assert 'score' in tweet
    
    finally:
        # Cleanup
        snapshot_dir = Path(snapshot_file).parent
        shutil.rmtree(snapshot_dir, ignore_errors=True)


def test_load_nonexistent_snapshot():
    """Test that loading a nonexistent snapshot raises FileNotFoundError."""
    # Try to load nonexistent snapshot (uses real filesystem)
    with pytest.raises(FileNotFoundError):
        load_reward_snapshot('nonexistent_brief_xyz_test', 'nonexistent_pool_xyz')


def test_snapshot_data_structure(sample_snapshot_data):
    """Test that snapshot data has correct structure."""
    # Verify required keys
    required_keys = {'brief_id', 'pool_name', 'created_at', 'tweet_rewards'}
    assert required_keys.issubset(sample_snapshot_data.keys())
    assert isinstance(sample_snapshot_data['tweet_rewards'], list)
    
    # Verify each tweet has required fields with correct types
    required_tweet_fields = {
        'tweet_id': str, 'author': str, 'uid': int, 'score': (int, float), 'total_usd': (int, float),
        'favorite_count': int, 'retweet_count': int, 'reply_count': int, 'quote_count': int,
        'bookmark_count': int, 'retweets': list, 'quotes': list, 'created_at': str, 'lang': str
    }
    
    for tweet in sample_snapshot_data['tweet_rewards']:
        for field, expected_type in required_tweet_fields.items():
            assert field in tweet, f"Missing field: {field}"
            assert isinstance(tweet[field], expected_type), f"{field} has wrong type"


def test_load_snapshot_deterministic_when_multiple_exist(sample_snapshot_data):
    """Test that loading is deterministic when multiple snapshots exist."""
    import shutil
    import time
    import copy
    
    brief_id = sample_snapshot_data['brief_id'] + "_multi"  # Unique ID for this test
    pool_name = sample_snapshot_data['pool_name']
    
    # Create multiple snapshots
    files = []
    for i in range(3):
        data = copy.deepcopy(sample_snapshot_data)
        data['brief_id'] = brief_id
        snapshot_file = save_reward_snapshot(brief_id, pool_name, data)
        files.append(snapshot_file)
        time.sleep(0.05)  # Small delay between creates
    
    try:
        # Load snapshot multiple times
        data1, file_path1 = load_reward_snapshot(brief_id, pool_name)
        data2, file_path2 = load_reward_snapshot(brief_id, pool_name)
        data3, file_path3 = load_reward_snapshot(brief_id, pool_name)
        
        # Should get same file every time (deterministic/stable)
        assert file_path1 == file_path2 == file_path3
        assert data1 == data2 == data3
        
        # Verify it has the score field
        for tweet in data1['tweet_rewards']:
            assert 'score' in tweet
    
    finally:
        # Cleanup all files
        snapshot_dir = Path(files[0]).parent
        shutil.rmtree(snapshot_dir, ignore_errors=True)

