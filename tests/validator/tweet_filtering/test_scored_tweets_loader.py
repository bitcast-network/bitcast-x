"""Tests for scored tweets loader."""

import json
import pytest
from pathlib import Path
from bitcast.validator.tweet_filtering.scored_tweets_loader import (
    load_latest_scored_tweets,
    load_existing_scored_tweets,
    validate_scored_tweets_structure
)


@pytest.fixture
def sample_scored_data():
    """Sample scored tweets data for testing."""
    return {
        'metadata': {
            'run_id': 'test_run_123',
            'brief_id': 'test_brief',
            'created_at': '2025-10-30T12:00:00',
            'pool_name': 'tao',
            'total_tweets_scored': 3
        },
        'scored_tweets': [
            {
                'tweet_id': '123',
                'author': 'user1',
                'text': 'Test tweet 1',
                'score': 0.5
            },
            {
                'tweet_id': '456',
                'author': 'user2',
                'text': 'Test tweet 2',
                'score': 0.3
            }
        ]
    }


@pytest.fixture
def temp_scored_tweets_dir(tmp_path, sample_scored_data):
    """Create temporary scored tweets directory with test data."""
    # Create directory structure
    scoring_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "tao"
    scoring_dir.mkdir(parents=True)
    
    # Create test file
    test_file = scoring_dir / "test_brief_2025.10.30_12.00.00.json"
    with open(test_file, 'w') as f:
        json.dump(sample_scored_data, f)
    
    return tmp_path


class TestValidateScoredTweetsStructure:
    """Test scored tweets structure validation."""
    
    def test_valid_structure_passes(self, sample_scored_data):
        """Should pass validation for valid structure."""
        # Should not raise
        validate_scored_tweets_structure(sample_scored_data)
    
    def test_missing_metadata_fails(self):
        """Should fail if metadata is missing."""
        data = {'scored_tweets': []}
        with pytest.raises(ValueError, match="missing 'metadata'"):
            validate_scored_tweets_structure(data)
    
    def test_missing_scored_tweets_fails(self):
        """Should fail if scored_tweets is missing."""
        data = {'metadata': {'run_id': 'test', 'brief_id': 'test', 'created_at': 'test'}}
        with pytest.raises(ValueError, match="missing 'scored_tweets'"):
            validate_scored_tweets_structure(data)
    
    def test_missing_required_metadata_field_fails(self):
        """Should fail if required metadata field is missing."""
        data = {
            'metadata': {'run_id': 'test'},  # missing brief_id and created_at
            'scored_tweets': []
        }
        with pytest.raises(ValueError, match="Metadata missing required field"):
            validate_scored_tweets_structure(data)
    
    def test_scored_tweets_not_list_fails(self):
        """Should fail if scored_tweets is not a list."""
        data = {
            'metadata': {'run_id': 'test', 'brief_id': 'test', 'created_at': 'test'},
            'scored_tweets': 'not a list'
        }
        with pytest.raises(ValueError, match="'scored_tweets' must be a list"):
            validate_scored_tweets_structure(data)
    
    def test_non_dict_data_fails(self):
        """Should fail if data is not a dict."""
        with pytest.raises(ValueError, match="must be a dictionary"):
            validate_scored_tweets_structure([])


class TestLoadLatestScoredTweets:
    """Test loading scored tweets from disk."""
    
    def test_loads_existing_file(self, temp_scored_tweets_dir, sample_scored_data, monkeypatch):
        """Should load existing scored tweets file."""
        # Mock the parent path to point to our temp directory
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        original_file = Path(scored_tweets_loader.__file__)
        mock_file = temp_scored_tweets_dir / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        data, file_path = load_latest_scored_tweets('test_brief')
        
        assert data['metadata']['brief_id'] == 'test_brief'
        assert len(data['scored_tweets']) == 2
        assert 'test_brief_2025.10.30_12.00.00.json' in file_path
    
    def test_raises_if_directory_missing(self, tmp_path, monkeypatch):
        """Should raise FileNotFoundError if scored_tweets directory doesn't exist."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        with pytest.raises(FileNotFoundError, match="Scored tweets directory does not exist"):
            load_latest_scored_tweets('nonexistent_brief')
    
    def test_raises_if_no_files_match(self, temp_scored_tweets_dir, monkeypatch):
        """Should raise FileNotFoundError if no files match brief_id."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        mock_file = temp_scored_tweets_dir / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        with pytest.raises(FileNotFoundError, match="No scored tweets found for brief_id"):
            load_latest_scored_tweets('nonexistent_brief')
    
    def test_selects_most_recent_file(self, tmp_path, sample_scored_data, monkeypatch):
        """Should select the most recent file if multiple exist."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        import os
        import time
        
        # Create directory structure
        scoring_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "tao"
        scoring_dir.mkdir(parents=True)
        
        # Create older file
        old_file = scoring_dir / "test_brief_2025.10.29_12.00.00.json"
        with open(old_file, 'w') as f:
            json.dump(sample_scored_data, f)
        
        # Ensure files have different modification times
        old_time = time.time() - 60  # 1 minute ago
        os.utime(old_file, (old_time, old_time))
        
        time.sleep(0.1)  # Small delay
        
        # Create newer file
        newer_data = sample_scored_data.copy()
        newer_data['metadata'] = sample_scored_data['metadata'].copy()
        newer_data['metadata']['run_id'] = 'newer_run'
        
        new_file = scoring_dir / "test_brief_2025.10.30_12.00.00.json"
        with open(new_file, 'w') as f:
            json.dump(newer_data, f)
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        data, file_path = load_latest_scored_tweets('test_brief')
        
        # Should load the newer file
        assert data['metadata']['run_id'] == 'newer_run'
        assert 'test_brief_2025.10.30_12.00.00.json' in file_path


class TestLoadExistingScoredTweets:
    """Test loading existing scoring snapshots for specific pool."""
    
    def test_loads_snapshot_from_specific_pool(self, tmp_path, sample_scored_data, monkeypatch):
        """Should load snapshot from specified pool directory."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        # Create directory structure with multiple pools
        tao_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "tao"
        tao_dir.mkdir(parents=True)
        
        bittensor_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "bittensor"
        bittensor_dir.mkdir(parents=True)
        
        # Create snapshot in tao pool
        tao_file = tao_dir / "test_brief_2025.10.30_12.00.00.json"
        with open(tao_file, 'w') as f:
            json.dump(sample_scored_data, f)
        
        # Create different snapshot in bittensor pool
        bittensor_data = sample_scored_data.copy()
        bittensor_data['metadata'] = sample_scored_data['metadata'].copy()
        bittensor_data['metadata']['run_id'] = 'bittensor_run'
        bittensor_file = bittensor_dir / "test_brief_2025.10.30_13.00.00.json"
        with open(bittensor_file, 'w') as f:
            json.dump(bittensor_data, f)
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        # Should load from tao pool only
        data, file_path = load_existing_scored_tweets('test_brief', 'tao')
        
        assert data['metadata']['run_id'] == 'test_run_123'
        assert data['metadata']['pool_name'] == 'tao'
        assert 'tao/test_brief_2025.10.30_12.00.00.json' in file_path
    
    def test_raises_if_pool_directory_missing(self, tmp_path, monkeypatch):
        """Should raise FileNotFoundError if pool directory doesn't exist."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        # Create scored_tweets dir but not the pool subdirectory
        scoring_dir = tmp_path / "tweet_scoring" / "scored_tweets"
        scoring_dir.mkdir(parents=True)
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        with pytest.raises(FileNotFoundError, match="No scored tweets directory found for pool 'nonexistent'"):
            load_existing_scored_tweets('test_brief', 'nonexistent')
    
    def test_raises_if_no_snapshot_for_brief(self, tmp_path, sample_scored_data, monkeypatch):
        """Should raise FileNotFoundError if no snapshot exists for brief in pool."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        # Create pool directory with different brief
        tao_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "tao"
        tao_dir.mkdir(parents=True)
        
        other_file = tao_dir / "other_brief_2025.10.30_12.00.00.json"
        with open(other_file, 'w') as f:
            json.dump(sample_scored_data, f)
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        with pytest.raises(FileNotFoundError, match="No scoring snapshot found for brief_id 'test_brief' in pool 'tao'"):
            load_existing_scored_tweets('test_brief', 'tao')
    
    def test_selects_oldest_file_as_canonical_snapshot(self, tmp_path, sample_scored_data, monkeypatch):
        """Should select the oldest file as the canonical snapshot."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        import os
        import time
        
        # Create pool directory
        tao_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "tao"
        tao_dir.mkdir(parents=True)
        
        # Create older file (canonical snapshot)
        old_data = sample_scored_data.copy()
        old_data['metadata'] = sample_scored_data['metadata'].copy()
        old_data['metadata']['run_id'] = 'canonical_snapshot'
        
        old_file = tao_dir / "test_brief_2025.10.29_12.00.00.json"
        with open(old_file, 'w') as f:
            json.dump(old_data, f)
        
        # Set older modification time
        old_time = time.time() - 120  # 2 minutes ago
        os.utime(old_file, (old_time, old_time))
        
        time.sleep(0.1)
        
        # Create newer file (should be ignored)
        newer_data = sample_scored_data.copy()
        newer_data['metadata'] = sample_scored_data['metadata'].copy()
        newer_data['metadata']['run_id'] = 'newer_snapshot'
        
        new_file = tao_dir / "test_brief_2025.10.30_12.00.00.json"
        with open(new_file, 'w') as f:
            json.dump(newer_data, f)
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        data, file_path = load_existing_scored_tweets('test_brief', 'tao')
        
        # Should load the OLDEST file (canonical snapshot)
        assert data['metadata']['run_id'] == 'canonical_snapshot'
        assert 'test_brief_2025.10.29_12.00.00.json' in file_path
    
    def test_validates_snapshot_structure(self, tmp_path, monkeypatch):
        """Should validate snapshot structure and raise if invalid."""
        from bitcast.validator.tweet_filtering import scored_tweets_loader
        
        # Create pool directory with invalid data
        tao_dir = tmp_path / "tweet_scoring" / "scored_tweets" / "tao"
        tao_dir.mkdir(parents=True)
        
        invalid_file = tao_dir / "test_brief_2025.10.30_12.00.00.json"
        with open(invalid_file, 'w') as f:
            json.dump({'invalid': 'data'}, f)
        
        mock_file = tmp_path / "tweet_filtering" / "mock.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        monkeypatch.setattr(scored_tweets_loader, '__file__', str(mock_file))
        
        with pytest.raises(ValueError, match="missing 'metadata'"):
            load_existing_scored_tweets('test_brief', 'tao')

