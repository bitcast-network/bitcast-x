"""Essential tests for token pricing."""

import pytest
from unittest.mock import Mock, patch

from bitcast.validator.utils.token_pricing import get_bitcast_alpha_price, get_total_miner_emissions


class TestGetBitcastAlphaPrice:
    """Test alpha price fetching (basic functionality)."""
    
    def test_fetches_price_from_api(self):
        """Should fetch price from CoinGecko API."""
        with patch('bitcast.validator.utils.token_pricing.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = {
                'bitcast': {'usd': 0.15}
            }
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response
            
            price = get_bitcast_alpha_price()
            
            # Should return valid price
            assert isinstance(price, float)
            assert price > 0


class TestGetTotalMinerEmissions:
    """Test miner emissions calculation (basic functionality)."""
    
    def test_calculates_miner_emissions(self):
        """Should calculate daily miner emissions from chain data."""
        with patch('bitcast.validator.utils.token_pricing.bt.Subtensor') as mock_subtensor:
            # Mock subnet info
            mock_subnet = Mock()
            mock_subnet.alpha_out_emission = 10.0  # TAO per block
            
            mock_instance = Mock()
            mock_instance.subnet.return_value = mock_subnet
            mock_subtensor.return_value = mock_instance
            
            emissions = get_total_miner_emissions()
            
            # Should return positive value
            assert isinstance(emissions, float)
            assert emissions > 0
