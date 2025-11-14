"""Tests for brief state assignment and filtering utilities."""

import pytest
from datetime import datetime, timezone, timedelta
from bitcast.validator.reward_engine.utils.brief_fetcher import assign_brief_states
from bitcast.validator.utils.config import EMISSIONS_PERIOD, REWARDS_DELAY_DAYS


class TestAssignBriefStates:
    """Test assign_brief_states() function."""
    
    def test_empty_briefs_list(self):
        """Test with empty briefs list."""
        result = assign_brief_states([])
        assert result == []
    
    def test_assign_brief_states_scoring_active(self):
        """Test active brief (A to B - scoring state)."""
        # Brief is currently active
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=10)
        end_date = today + timedelta(days=5)
        briefs = [
            {
                'id': 'test_active',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 1000
            }
        ]
        result = assign_brief_states(briefs)
        assert len(result) == 1
        assert result[0]['id'] == 'test_active'
        assert result[0]['state'] == 'scoring'
    
    def test_assign_brief_states_scoring_wait_period(self):
        """Test brief in wait period (B to C - scoring state)."""
        # Brief ended 1 day ago, in wait period
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=10)
        end_date = today - timedelta(days=1)
        briefs = [
            {
                'id': 'test_wait',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 1000
            }
        ]
        result = assign_brief_states(briefs)
        assert len(result) == 1
        assert result[0]['id'] == 'test_wait'
        assert result[0]['state'] == 'scoring'
    
    def test_assign_brief_states_at_scoring_end(self):
        """Test brief at last day of scoring phase."""
        # Brief ended exactly REWARDS_DELAY_DAYS ago - still scoring
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=30)
        end_date = today - timedelta(days=REWARDS_DELAY_DAYS)
        briefs = [
            {
                'id': 'test_scoring_end',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 2000
            }
        ]
        result = assign_brief_states(briefs)
        assert len(result) == 1
        assert result[0]['id'] == 'test_scoring_end'
        assert result[0]['state'] == 'scoring'
    
    def test_assign_brief_states_emission(self):
        """Test brief in emission period (D to E)."""
        # Brief ended REWARDS_DELAY_DAYS + 1 days ago - first day of emission
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=30)
        end_date = today - timedelta(days=REWARDS_DELAY_DAYS + 1)
        briefs = [
            {
                'id': 'test_emission',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 2000
            }
        ]
        result = assign_brief_states(briefs)
        assert len(result) == 1
        assert result[0]['id'] == 'test_emission'
        assert result[0]['state'] == 'emission'
    
    def test_assign_brief_states_expired(self):
        """Test brief past emission period (expired)."""
        # Brief ended more than REWARDS_DELAY_DAYS + EMISSIONS_PERIOD ago
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=30)
        end_date = today - timedelta(days=REWARDS_DELAY_DAYS + EMISSIONS_PERIOD + 1)
        briefs = [
            {
                'id': 'test_expired',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 3000
            }
        ]
        result = assign_brief_states(briefs)
        # Expired briefs should be excluded
        assert len(result) == 0
    
    def test_assign_brief_states_mixed(self):
        """Test mix of briefs in different states."""
        today = datetime.now(timezone.utc).date()
        briefs = [
            {
                'id': 'scoring_brief',
                'start_date': (today - timedelta(days=10)).strftime('%Y-%m-%d'),
                'end_date': (today - timedelta(days=1)).strftime('%Y-%m-%d'),
                'budget': 1000
            },
            {
                'id': 'emission_brief',
                'start_date': (today - timedelta(days=30)).strftime('%Y-%m-%d'),
                'end_date': (today - timedelta(days=REWARDS_DELAY_DAYS + 1)).strftime('%Y-%m-%d'),
                'budget': 2000
            },
            {
                'id': 'expired_brief',
                'start_date': (today - timedelta(days=60)).strftime('%Y-%m-%d'),
                'end_date': (today - timedelta(days=REWARDS_DELAY_DAYS + EMISSIONS_PERIOD + 1)).strftime('%Y-%m-%d'),
                'budget': 3000
            },
            {
                'id': 'future_brief',
                'start_date': (today + timedelta(days=1)).strftime('%Y-%m-%d'),
                'end_date': (today + timedelta(days=10)).strftime('%Y-%m-%d'),
                'budget': 4000
            }
        ]
        result = assign_brief_states(briefs)
        
        # Should have 2 active briefs (scoring + emission)
        assert len(result) == 2
        
        result_by_id = {b['id']: b for b in result}
        assert 'scoring_brief' in result_by_id
        assert result_by_id['scoring_brief']['state'] == 'scoring'
        assert 'emission_brief' in result_by_id
        assert result_by_id['emission_brief']['state'] == 'emission'
        assert 'expired_brief' not in result_by_id
        assert 'future_brief' not in result_by_id
    
    def test_brief_at_emission_start_boundary(self):
        """Test brief at exact start of emission period."""
        # Brief ended REWARDS_DELAY_DAYS + 1 days ago (first emission day)
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=30)
        end_date = today - timedelta(days=REWARDS_DELAY_DAYS + 1)
        briefs = [
            {
                'id': 'at_emission_start',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 1000
            }
        ]
        result = assign_brief_states(briefs)
        assert len(result) == 1
        assert result[0]['state'] == 'emission'
    
    def test_brief_at_emission_end_boundary(self):
        """Test brief at exact end of emission period."""
        # Brief ended exactly REWARDS_DELAY_DAYS + EMISSIONS_PERIOD days ago
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=60)
        end_date = today - timedelta(days=REWARDS_DELAY_DAYS + EMISSIONS_PERIOD)
        briefs = [
            {
                'id': 'at_emission_end',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 1000
            }
        ]
        result = assign_brief_states(briefs)
        assert len(result) == 1
        assert result[0]['state'] == 'emission'
    
    def test_brief_not_started_yet(self):
        """Test brief that hasn't started yet."""
        today = datetime.now(timezone.utc).date()
        start_date = today + timedelta(days=1)
        end_date = today + timedelta(days=10)
        briefs = [
            {
                'id': 'test_future',
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'budget': 5000
            }
        ]
        result = assign_brief_states(briefs)
        # Future briefs should be excluded
        assert len(result) == 0
    
    def test_state_field_added_to_brief(self):
        """Test that state field is properly added to brief dict."""
        today = datetime.now(timezone.utc).date()
        briefs = [
            {
                'id': 'test_brief',
                'start_date': (today - timedelta(days=10)).strftime('%Y-%m-%d'),
                'end_date': (today - timedelta(days=1)).strftime('%Y-%m-%d'),
                'budget': 1000,
                'pool': 'tao'
            }
        ]
        result = assign_brief_states(briefs)
        assert 'state' in result[0]
        assert result[0]['state'] in ['scoring', 'emission']
        # Original fields should still be present
        assert result[0]['id'] == 'test_brief'
        assert result[0]['budget'] == 1000
        assert result[0]['pool'] == 'tao'

