"""Tests for Brief dataclass."""

import pytest
from datetime import datetime, timezone, timedelta
from bitcast.validator.reward_engine.models import Brief


class TestBriefCreation:
    """Tests for creating Brief instances."""
    
    def test_brief_creation_with_all_fields(self):
        """Can create valid Brief with all fields."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        brief = Brief(
            id="test_001",
            pool="tao",
            budget=1000.0,
            start_date=start,
            end_date=end,
            brief_text="Test campaign",
            tag="#test",
            qrt="1234567890",
            prompt_version=2,
            boost=1.5
        )
        
        assert brief.id == "test_001"
        assert brief.pool == "tao"
        assert brief.budget == 1000.0
        assert brief.brief_text == "Test campaign"
        assert brief.tag == "#test"
        assert brief.qrt == "1234567890"
        assert brief.prompt_version == 2
        assert brief.boost == 1.5
    
    def test_brief_creation_with_required_fields_only(self):
        """Can create Brief with only required fields."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        brief = Brief(
            id="test_002",
            pool="tao",
            budget=500.0,
            start_date=start,
            end_date=end,
            brief_text="Minimal campaign"
        )
        
        assert brief.id == "test_002"
        assert brief.tag is None
        assert brief.qrt is None
        assert brief.prompt_version == 1  # Default
        assert brief.boost == 1.0  # Default


class TestBriefValidation:
    """Tests for Brief validation."""
    
    def test_negative_budget_raises_error(self):
        """Brief rejects negative budget."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        with pytest.raises(ValueError, match="Budget must be non-negative"):
            Brief(
                id="test", 
                pool="tao", 
                budget=-100,
                start_date=start,
                end_date=end,
                brief_text="Test"
            )
    
    def test_end_before_start_raises_error(self):
        """Brief rejects end_date before start_date."""
        start = datetime.now(timezone.utc)
        end = start - timedelta(days=1)  # End before start!
        
        with pytest.raises(ValueError, match="End date .* must be after start date"):
            Brief(
                id="test",
                pool="tao",
                budget=100,
                start_date=start,
                end_date=end,
                brief_text="Test"
            )
    
    def test_empty_id_raises_error(self):
        """Brief rejects empty ID."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        with pytest.raises(ValueError, match="Brief ID cannot be empty"):
            Brief(
                id="",
                pool="tao",
                budget=100,
                start_date=start,
                end_date=end,
                brief_text="Test"
            )
    
    def test_empty_pool_raises_error(self):
        """Brief rejects empty pool name."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        with pytest.raises(ValueError, match="Pool name cannot be empty"):
            Brief(
                id="test",
                pool="",
                budget=100,
                start_date=start,
                end_date=end,
                brief_text="Test"
            )


class TestBriefProperties:
    """Tests for Brief computed properties."""
    
    def test_daily_budget_calculation(self):
        """Brief calculates daily budget correctly."""
        from bitcast.validator.utils.config import EMISSIONS_PERIOD
        
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        brief = Brief(
            id="test",
            pool="tao",
            budget=700.0,
            start_date=start,
            end_date=end,
            brief_text="Test"
        )
        
        # daily_budget should be budget / EMISSIONS_PERIOD
        assert brief.daily_budget == 700.0 / EMISSIONS_PERIOD


class TestBriefFromDict:
    """Tests for creating Brief from dictionary."""
    
    def test_from_dict_with_string_dates(self):
        """Can create Brief from API response with string dates."""
        data = {
            'id': 'api_001',
            'pool': 'tao',
            'budget': 5000,
            'start_date': '2025-11-01T00:00:00Z',
            'end_date': '2025-11-08T00:00:00Z',
            'brief': 'API test campaign',
            'tag': '#test',
            'qrt': '9876543210',
            'prompt_version': 2,
            'boost': 1.2
        }
        
        brief = Brief.from_dict(data)
        
        assert brief.id == 'api_001'
        assert brief.pool == 'tao'
        assert brief.budget == 5000.0
        assert brief.brief_text == 'API test campaign'
        assert brief.tag == '#test'
        assert brief.qrt == '9876543210'
        assert brief.prompt_version == 2
        assert brief.boost == 1.2
        assert isinstance(brief.start_date, datetime)
        assert isinstance(brief.end_date, datetime)
    
    def test_from_dict_with_defaults(self):
        """from_dict uses defaults for missing optional fields."""
        data = {
            'id': 'api_002',
            'start_date': '2025-11-01T00:00:00Z',
            'end_date': '2025-11-08T00:00:00Z',
        }
        
        brief = Brief.from_dict(data)
        
        assert brief.pool == 'tao'  # Default
        assert brief.budget == 0.0  # Default
        assert brief.brief_text == ''  # Default
        assert brief.prompt_version == 1  # Default
        assert brief.boost == 1.0  # Default


class TestBriefToDict:
    """Tests for converting Brief to dictionary."""
    
    def test_to_dict_creates_valid_dict(self):
        """to_dict creates dictionary with all fields."""
        start = datetime(2025, 11, 1, tzinfo=timezone.utc)
        end = datetime(2025, 11, 8, tzinfo=timezone.utc)
        
        brief = Brief(
            id="test_001",
            pool="tao",
            budget=1000.0,
            start_date=start,
            end_date=end,
            brief_text="Test campaign",
            tag="#test",
            qrt="123",
            prompt_version=2,
            boost=1.5
        )
        
        data = brief.to_dict()
        
        assert data['id'] == "test_001"
        assert data['pool'] == "tao"
        assert data['budget'] == 1000.0
        assert data['brief'] == "Test campaign"
        assert data['tag'] == "#test"
        assert data['qrt'] == "123"
        assert data['prompt_version'] == 2
        assert data['boost'] == 1.5
        assert 'start_date' in data
        assert 'end_date' in data
    
    def test_round_trip_dict_conversion(self):
        """Can convert Brief to dict and back."""
        original_data = {
            'id': 'round_trip',
            'pool': 'tao',
            'budget': 2000,
            'start_date': '2025-11-01T00:00:00Z',
            'end_date': '2025-11-08T00:00:00Z',
            'brief': 'Round trip test',
            'tag': '#roundtrip',
            'prompt_version': 3
        }
        
        brief = Brief.from_dict(original_data)
        converted = brief.to_dict()
        brief2 = Brief.from_dict(converted)
        
        assert brief.id == brief2.id
        assert brief.pool == brief2.pool
        assert brief.budget == brief2.budget
        assert brief.brief_text == brief2.brief_text
        assert brief.tag == brief2.tag


class TestBriefEligibilityConfig:
    """Tests for brief-level eligibility configuration."""
    
    def test_brief_with_eligibility_config(self):
        """Can create Brief with max_members and max_considered."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        brief = Brief(
            id="test_003",
            pool="tao",
            budget=1000.0,
            start_date=start,
            end_date=end,
            brief_text="Test campaign",
            max_members=100,
            max_considered=250
        )
        
        assert brief.max_members == 100
        assert brief.max_considered == 250
    
    def test_brief_eligibility_config_optional(self):
        """max_members and max_considered are optional."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=7)
        
        brief = Brief(
            id="test_004",
            pool="tao",
            budget=1000.0,
            start_date=start,
            end_date=end,
            brief_text="Test campaign"
        )
        
        assert brief.max_members is None
        assert brief.max_considered is None
    
    def test_from_dict_with_eligibility_config(self):
        """from_dict parses max_members and max_considered."""
        data = {
            'id': 'api_003',
            'pool': 'tao',
            'budget': 5000,
            'start_date': '2025-11-01T00:00:00Z',
            'end_date': '2025-11-08T00:00:00Z',
            'brief': 'API test with config',
            'max_members': 75,
            'max_considered': 200
        }
        
        brief = Brief.from_dict(data)
        
        assert brief.max_members == 75
        assert brief.max_considered == 200
    
    def test_from_dict_without_eligibility_config(self):
        """from_dict handles missing eligibility config (defaults to None)."""
        data = {
            'id': 'api_004',
            'pool': 'tao',
            'budget': 3000,
            'start_date': '2025-11-01T00:00:00Z',
            'end_date': '2025-11-08T00:00:00Z',
            'brief': 'API test without config'
        }
        
        brief = Brief.from_dict(data)
        
        assert brief.max_members is None
        assert brief.max_considered is None
    
    def test_to_dict_includes_eligibility_config(self):
        """to_dict includes max_members and max_considered."""
        start = datetime(2025, 11, 1, tzinfo=timezone.utc)
        end = datetime(2025, 11, 8, tzinfo=timezone.utc)
        
        brief = Brief(
            id="test_005",
            pool="tao",
            budget=2000.0,
            start_date=start,
            end_date=end,
            brief_text="Test campaign",
            max_members=150,
            max_considered=300
        )
        
        data = brief.to_dict()
        
        assert data['max_members'] == 150
        assert data['max_considered'] == 300
    
    def test_to_dict_includes_none_eligibility_config(self):
        """to_dict includes max_members and max_considered even when None."""
        start = datetime(2025, 11, 1, tzinfo=timezone.utc)
        end = datetime(2025, 11, 8, tzinfo=timezone.utc)
        
        brief = Brief(
            id="test_006",
            pool="tao",
            budget=1500.0,
            start_date=start,
            end_date=end,
            brief_text="Test campaign"
        )
        
        data = brief.to_dict()
        
        assert 'max_members' in data
        assert 'max_considered' in data
        assert data['max_members'] is None
        assert data['max_considered'] is None

