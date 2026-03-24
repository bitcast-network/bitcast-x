"""
Referral bonus service for managing and paying referral bonuses.

Referral bonuses are paid when:
1. A referee participates in a brief (has tweets passing filter)
2. Their payout date is scheduled (set to tomorrow when first detected)
3. On the payout date, referee and referrer receive a dynamic bonus based on
   the referee's follower count and influence score
"""

from datetime import date, timedelta
from math import log10
from typing import Dict, List, NamedTuple, Set
import bittensor as bt

from bitcast.validator.account_connection.connection_db import ConnectionDatabase


def compute_referral_reward(followers: int, influence: float) -> float:
    """
    Compute the referral bonus (USD) from the referee's followers and influence score.

    Followers component (80% weight): log-scales from 1,000 to 25,000 followers.
    Influence component (20% weight): log-scales from 1 to 1,000 influence score.
    Result is in the range [$0, $100].
    """
    follower_raw = 100 * log10(max(followers, 1000) / 1000) / log10(25000 / 1000)
    follower_score = 0.8 * max(0.0, min(follower_raw, 100.0))

    influence_raw = 100 * log10(max(influence, 1)) / log10(1000)
    influence_score = 0.2 * max(0.0, min(influence_raw, 100.0))

    return round(follower_score + influence_score, 2)


class ReferralBonusResult(NamedTuple):
    """Result of computing referral bonuses."""
    bonuses: Dict[int, float]          # {uid: total_bonus_usd}
    referrals: List[Dict]              # Raw referral records from DB


class ReferralBonusService:
    """Service for managing referral bonuses."""
    
    def __init__(self, connection_db: ConnectionDatabase):
        self.connection_db = connection_db
    
    def get_referral_bonuses(
        self,
        payout_date: date,
        account_to_uid: Dict[str, int],
        account_data: Dict[str, Dict] = None,
    ) -> ReferralBonusResult:
        """
        Get referral bonuses to add to rewards for a specific payout date.

        The bonus amount is computed dynamically from the referee's follower
        count and influence score (via ``account_data``).  Both referee and
        referrer receive the same computed amount.

        Args:
            payout_date: The date to pay out bonuses
            account_to_uid: Mapping of account_username -> uid
            account_data: Mapping of username -> {"followers_count": int, "score": float}
                          from the social map.  If *None*, every bonus will be $0.

        Returns:
            ReferralBonusResult with bonuses dict and enriched referral records
        """
        referrals = self.connection_db.get_referrals_for_payout(payout_date)

        if not referrals:
            return ReferralBonusResult(bonuses={}, referrals=[])

        bt.logging.info(f"Processing {len(referrals)} referrals for payout on {payout_date}")

        if account_data is None:
            account_data = {}

        bonuses: Dict[int, float] = {}
        paid_pairs: Set[tuple] = set()

        for referral in referrals:
            referee_username = referral['account_username']
            referrer_username = referral.get('referred_by')

            pair = (referee_username, referrer_username)
            if pair in paid_pairs:
                referral['computed_amount'] = 0.0
                bt.logging.debug(
                    f"Skipping duplicate referral for @{referee_username} "
                    f"referred by @{referrer_username} (already paid in another pool)"
                )
                continue
            paid_pairs.add(pair)

            referee_info = account_data.get(referee_username, {})
            followers = referee_info.get('followers_count', 0)
            influence = referee_info.get('score', 0.0)
            amount = compute_referral_reward(followers, influence)

            referral['computed_amount'] = amount

            # Referee bonus
            referee_uid = account_to_uid.get(referee_username)
            if referee_uid is not None:
                bonuses[referee_uid] = bonuses.get(referee_uid, 0.0) + amount
                bt.logging.info(
                    f"Referee bonus: @{referee_username} (UID {referee_uid}) "
                    f"+${amount:.2f} (followers={followers}, influence={influence:.2f})"
                )
            else:
                bt.logging.warning(f"No UID mapping for referee @{referee_username}")

            # Referrer bonus (same amount)
            if referrer_username:
                referrer_uid = account_to_uid.get(referrer_username)
                if referrer_uid is not None:
                    bonuses[referrer_uid] = bonuses.get(referrer_uid, 0.0) + amount
                    bt.logging.info(
                        f"Referrer bonus: @{referrer_username} (UID {referrer_uid}) "
                        f"+${amount:.2f}"
                    )
                else:
                    bt.logging.warning(f"No UID mapping for referrer @{referrer_username}")

        return ReferralBonusResult(bonuses=bonuses, referrals=referrals)
    
    def check_and_activate_referrals(
        self,
        participating_accounts: Set[str],
    ) -> int:
        """
        Find referees who participated in briefs for the first time and set their
        payout dates to tomorrow. Payout dates are only set once (immutable after).
        
        Args:
            participating_accounts: Set of account usernames that participated in briefs
            
        Returns:
            Number of new referrals activated
        """
        all_referrals = self.connection_db.get_all_connections_with_referrals()
        
        pending = [r for r in all_referrals if r.get('payout_date') is None]
        
        if not pending:
            return 0

        already_paid = {
            (r['account_username'], r.get('referred_by'))
            for r in all_referrals
            if r.get('payout_date') is not None
        }
        
        tomorrow = date.today() + timedelta(days=1)
        activated = 0
        activated_pairs: Set[tuple] = set()
        
        for referral in pending:
            referee_username = referral['account_username']
            referrer = referral.get('referred_by')
            pair = (referee_username, referrer)
            
            if referee_username not in participating_accounts:
                continue

            if pair in already_paid or pair in activated_pairs:
                bt.logging.debug(
                    f"Skipping duplicate referral activation for @{referee_username} "
                    f"referred by @{referrer} (already activated in another pool)"
                )
                continue
            
            success = self.connection_db.set_payout_date(
                connection_id=referral['connection_id'],
                payout_date=tomorrow
            )
            
            if success:
                activated += 1
                activated_pairs.add(pair)
                bt.logging.info(
                    f"Activated referral: @{referee_username} referred by @{referrer}, "
                    f"payout on {tomorrow}"
                )
        
        if activated > 0:
            bt.logging.info(f"Activated {activated} new referrals")
        
        return activated
