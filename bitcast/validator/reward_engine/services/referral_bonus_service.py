"""
Referral bonus service for managing and paying referral bonuses.

Referral bonuses are paid when:
1. A referee participates in a brief (has tweets passing filter)
2. Their payout date is scheduled (set to tomorrow when first detected)
3. On the payout date, referee and referrer receive the bonus amount that was
   locked when the referral was registered
"""

from datetime import date, timedelta
from typing import Dict, List, NamedTuple, Set
import bittensor as bt

from bitcast.validator.account_connection.connection_db import ConnectionDatabase
from bitcast.validator.utils.referral_rewards import (
    compute_referral_reward,
    compute_referral_reward_from_account,
)


class ReferralBonusResult(NamedTuple):
    """Result of computing referral bonuses."""
    bonuses: Dict[int, float]          # {uid: total_bonus_usd}
    referrals: List[Dict]              # Raw referral records from DB


class ReferralBonusService:
    """Service for managing referral bonuses."""
    
    def __init__(self, connection_db: ConnectionDatabase):
        self.connection_db = connection_db

    @staticmethod
    def _locked_referral_total(referral: Dict) -> float:
        """Return the locked total USD value for a referral row."""
        return float(referral.get('referee_amount') or 0.0) + float(referral.get('referrer_amount') or 0.0)
    
    def get_referral_bonuses(
        self,
        payout_date: date,
        account_to_uid: Dict[str, int],
        account_data: Dict[str, Dict] = None,
    ) -> ReferralBonusResult:
        """
        Get referral bonuses to add to rewards for a specific payout date.

        The bonus amount is read from the referral row. Amounts are locked when
        the referral is registered; ``account_data`` is only used as a fallback
        for legacy rows with missing amounts.

        Args:
            payout_date: The date to pay out bonuses
            account_to_uid: Mapping of account_username -> uid
            account_data: Optional mapping used only as a fallback for legacy
                          rows with missing locked amounts.

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

        for referral in referrals:
            referee_username = referral['account_username']
            referrer_username = referral.get('referred_by')

            referee_amount = referral.get('referee_amount')
            referrer_amount = referral.get('referrer_amount')
            if referee_amount is None or referrer_amount is None:
                fallback_amount = compute_referral_reward_from_account(
                    account_data.get(referee_username, {})
                )
                referee_amount = fallback_amount if referee_amount is None else referee_amount
                referrer_amount = fallback_amount if referrer_amount is None else referrer_amount
                bt.logging.warning(
                    f"Referral @{referee_username} had missing locked amount; "
                    f"using fallback ${fallback_amount:.2f}"
                )

            referee_amount = float(referee_amount)
            referrer_amount = float(referrer_amount)

            referral['computed_referee_amount'] = referee_amount
            referral['computed_referrer_amount'] = referrer_amount
            referral['computed_amount'] = referee_amount

            # Referee bonus
            referee_uid = account_to_uid.get(referee_username)
            if referee_uid is not None:
                bonuses[referee_uid] = bonuses.get(referee_uid, 0.0) + referee_amount
                bt.logging.info(
                    f"Referee bonus: @{referee_username} (UID {referee_uid}) "
                    f"+${referee_amount:.2f} (locked at registration)"
                )
            else:
                bt.logging.warning(f"No UID mapping for referee @{referee_username}")

            # Referrer bonus (same amount)
            if referrer_username:
                referrer_uid = account_to_uid.get(referrer_username)
                if referrer_uid is not None:
                    bonuses[referrer_uid] = bonuses.get(referrer_uid, 0.0) + referrer_amount
                    bt.logging.info(
                        f"Referrer bonus: @{referrer_username} (UID {referrer_uid}) "
                        f"+${referrer_amount:.2f} (locked at registration)"
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
        pending = [
            r for r in self.connection_db.get_all_connections_with_referrals()
            if r.get('payout_date') is None
        ]

        if not pending:
            return 0

        tomorrow = date.today() + timedelta(days=1)
        activated = 0

        for referral in pending:
            referee_username = referral['account_username']
            if referee_username not in participating_accounts:
                continue

            success = self.connection_db.set_payout_date(
                connection_id=referral['connection_id'],
                payout_date=tomorrow,
            )

            if success:
                activated += 1
                bt.logging.info(
                    f"Activated referral: @{referee_username} referred by "
                    f"@{referral.get('referred_by')}, payout on {tomorrow} "
                    f"(${self._locked_referral_total(referral):.2f} locked total)"
                )
        
        if activated > 0:
            bt.logging.info(f"Activated {activated} new referrals")
        
        return activated
