"""
Pool status management for social discovery.

Handles pool membership selection and status transitions.
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Set
import bittensor as bt


class PoolStatusManager:
    """Manages pool membership status and transitions."""
    
    @staticmethod
    def calculate_interaction_weights(
        adjacency_matrix: np.ndarray, 
        usernames: List[str]
    ) -> Dict[str, float]:
        """
        Calculate interaction weights for each account.
        
        Interaction weight = sum of row + column in adjacency matrix
        This represents total interactions (incoming + outgoing).
        
        Args:
            adjacency_matrix: NxN matrix of interaction weights
            usernames: Ordered list of usernames corresponding to matrix indices
            
        Returns:
            Dict mapping username to interaction weight
        """
        n = len(usernames)
        interaction_weights = {}
        
        for i, username in enumerate(usernames):
            # Row sum: outgoing interactions
            row_sum = np.sum(adjacency_matrix[i, :])
            # Column sum: incoming interactions  
            col_sum = np.sum(adjacency_matrix[:, i])
            # Total interaction weight
            interaction_weights[username] = row_sum + col_sum
            
        return interaction_weights
    
    @staticmethod
    def determine_account_status(
        account: str,
        was_active: bool,
        is_active: bool,
        is_first_run: bool
    ) -> str:
        """
        Determine the status of an account based on transitions.
        
        Args:
            account: Username
            was_active: Was in pool previously (in/promoted)
            is_active: Is in pool now
            is_first_run: Is this the first discovery run
            
        Returns:
            Status string: 'in', 'out', 'promoted', or 'relegated'
        """
        if is_first_run:
            return 'promoted' if is_active else 'out'
        
        if was_active and is_active:
            return 'in'  # Stayed active
        elif was_active and not is_active:
            return 'relegated'  # Removed from pool
        elif not was_active and is_active:
            return 'promoted'  # Added to pool
        else:  # not was_active and not is_active
            return 'out'  # Stayed inactive
    
    def calculate_pool_membership(
        self,
        scores: Dict[str, float],
        adjacency_matrix: np.ndarray,
        usernames: List[str],
        pool_config: Dict,
        previous_status: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """
        Calculate pool membership and status for all accounts.
        
        Args:
            scores: PageRank scores for all accounts
            adjacency_matrix: Interaction matrix
            usernames: Ordered list of usernames
            pool_config: Pool configuration with max_members, min_interaction_weight
            previous_status: Previous run's status mapping (None for first run)
            
        Returns:
            Dict mapping username to status
        """
        is_first_run = previous_status is None
        interaction_weights = self.calculate_interaction_weights(adjacency_matrix, usernames)
        
        # Get current active members
        if is_first_run:
            active_members = set()
        else:
            active_members = {
                acc for acc, status in previous_status.items() 
                if status in ['in', 'promoted']
            }
        
        # Calculate membership changes based on PageRank scores
        promoted, relegated = self.calculate_membership_changes(
            active_members=active_members,
            all_accounts=set(scores.keys()),
            scores=scores,
            interaction_weights=interaction_weights,
            pool_config=pool_config
        )
        
        # Calculate new active members
        new_active_members = (active_members - relegated) | promoted
        
        # Assign status to all accounts
        status_map = {}
        for account in scores.keys():
            was_active = account in active_members
            is_active = account in new_active_members
            status_map[account] = self.determine_account_status(
                account, was_active, is_active, is_first_run
            )
        
        return status_map
    
    def calculate_membership_changes(
        self,
        active_members: Set[str],
        all_accounts: Set[str],
        scores: Dict[str, float],
        interaction_weights: Dict[str, float],
        pool_config: Dict
    ) -> Tuple[Set[str], Set[str]]:
        """
        Calculate pool membership changes based on PageRank scores.
        
        Selects the top max_members accounts by PageRank score
        that meet the min_interaction_weight threshold, then
        determines which accounts should be promoted or relegated.
        
        Args:
            active_members: Current active pool members
            all_accounts: All discovered accounts
            scores: PageRank scores
            interaction_weights: Interaction weights from adjacency matrix
            pool_config: Pool configuration
            
        Returns:
            Tuple of (promoted_accounts, relegated_accounts)
        """
        max_members = pool_config['max_members']
        min_interaction_weight = pool_config['min_interaction_weight']
        
        # Filter all accounts by interaction weight threshold
        eligible_accounts = [
            acc for acc in all_accounts
            if interaction_weights.get(acc, 0) >= min_interaction_weight
        ]
        
        # Sort all eligible accounts by PageRank score (descending)
        sorted_accounts = sorted(
            eligible_accounts, 
            key=lambda x: scores.get(x, 0), 
            reverse=True
        )
        
        # Take top max_members as new active pool
        new_active_members = set(sorted_accounts[:max_members])
        
        # Calculate promoted and relegated
        promoted = new_active_members - active_members
        relegated = active_members - new_active_members
        
        bt.logging.info(
            f"Pool membership: {len(new_active_members)}/{max_members} active, "
            f"{len(promoted)} promoted, {len(relegated)} relegated "
            f"(eligible accounts: {len(eligible_accounts)})"
        )
        
        return promoted, relegated
    
