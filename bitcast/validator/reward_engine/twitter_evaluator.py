"""
Twitter/X platform evaluator for tweet-based reward distribution.

This module implements the Twitter evaluator that:
1. Scores tweets from connected accounts using tweet_scoring module
2. Filters tweets against briefs using tweet_filtering module
3. Maps tweets to UIDs using connection database
4. Calculates budget distribution proportionally
5. Creates USD targets for reward distribution
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import asyncio
import bittensor as bt

from bitcast.validator.reward_engine.interfaces.platform_evaluator import ScanBasedEvaluator
from bitcast.validator.reward_engine.models.evaluation_result import (
    EvaluationResult,
    EvaluationResultCollection,
    AccountResult
)
from bitcast.validator.tweet_scoring.tweet_scorer import score_tweets_for_pool
from bitcast.validator.tweet_filtering.tweet_filter import filter_tweets_for_brief
from bitcast.validator.utils.config import (
    EMISSIONS_PERIOD, TWEETS_SUBMIT_ENDPOINT, ENABLE_DATA_PUBLISH,
    NOCODE_UID, SIMULATE_CONNECTIONS, REWARD_SMOOTHING_EXPONENT
)
from bitcast.validator.utils.date_utils import parse_brief_date
from bitcast.validator.utils.token_pricing import get_bitcast_alpha_price
from bitcast.validator.reward_engine.utils import (
    publish_brief_tweets,
    create_tweet_payload,
    save_reward_snapshot,
    load_reward_snapshot
)


class TwitterEvaluator(ScanBasedEvaluator):
    """Evaluator for Twitter/X platform content."""
    
    def platform_name(self) -> str:
        """Return platform identifier."""
        return "twitter"
    
    async def score_briefs_for_monitoring(
        self,
        briefs: List[Dict[str, Any]],
        connected_accounts: set,
        run_id: Optional[str] = None
    ) -> None:
        """
        Score and publish briefs in 'scoring' state for monitoring purposes.
        
        This handles briefs from A to C (end_date to end_date + REWARDS_DELAY_DAYS).
        Scores tweets and publishes data, but does NOT calculate rewards.
        
        Args:
            briefs: List of briefs in 'scoring' state
            connected_accounts: Set of connected account usernames to filter scoring
            run_id: Optional run identifier
        """
        if not briefs:
            return
        
        bt.logging.info(f"📊 Monitoring {len(briefs)} briefs in scoring phase (no rewards)")
        
        for brief in briefs:
            brief_id = brief['id']
            pool_name = brief['pool']
            tag = brief.get('tag')
            qrt = brief.get('qrt')
            
            # Parse brief dates
            start_date = parse_brief_date(brief.get('start_date'))
            end_date = parse_brief_date(brief.get('end_date'), end_of_day=True)
            
            # Extract brief-level configuration
            brief_max_members = brief.get('max_members')
            brief_considered = brief.get('max_considered')
            
            try:
                # Step 1: Score tweets (always fresh)
                scored_tweets = self._score_tweets_for_brief(
                    pool_name=pool_name,
                    brief_id=brief_id,
                    connected_accounts=connected_accounts,
                    tag=tag,
                    qrt=qrt,
                    run_id=run_id,
                    start_date=start_date,
                    end_date=end_date,
                    max_members=brief_max_members,
                    considered_accounts=brief_considered
                )
                
                if not scored_tweets:
                    bt.logging.info(f"✓ Brief {brief_id}: No tweets to score yet")
                    continue
                
                # Step 2: Filter tweets by brief criteria (includes max_tweets)
                filtered_tweets = self._filter_tweets_for_brief(
                    scored_tweets=scored_tweets,
                    brief=brief,
                    run_id=run_id,
                    max_tweets=brief.get('max_tweets')
                )
                
                bt.logging.info(
                    f"✓ Brief {brief_id}: {len(scored_tweets)} scored, "
                    f"{len(filtered_tweets)} passed filtering"
                )
                
                # Step 3: Publish for monitoring (per BA requirement)
                await self._publish_brief_tweets(
                    brief_id=brief_id,
                    brief=brief,
                    tweets_with_targets=filtered_tweets,  # No targets yet
                    usd_targets={},  # Empty - no rewards in scoring phase
                    run_id=run_id
                )
                
            except Exception as e:
                bt.logging.error(f"Error monitoring brief {brief_id} in scoring phase: {e}")
                continue
    
    async def evaluate_briefs(
        self,
        briefs: List[Dict[str, Any]],
        uid_account_mappings: List[Dict[str, Any]],
        connected_accounts: set,
        metagraph: Any,
        run_id: Optional[str] = None
    ) -> EvaluationResultCollection:
        """
        Evaluate briefs in 'emission' state and calculate reward distribution.
        
        Handles briefs from D to E (end_date + REWARDS_DELAY_DAYS to end_date + REWARDS_DELAY_DAYS + EMISSIONS_PERIOD).
        
        For each brief:
        - First emission run (D): Calculates rewards and saves snapshot
        - Subsequent runs (D to E): Loads snapshot for stable daily payouts
        
        Args:
            briefs: List of briefs in 'emission' state only
            uid_account_mappings: List of {account_username, uid} mappings
            connected_accounts: Set of connected account usernames to filter scoring
            metagraph: Bittensor metagraph
            run_id: Optional run identifier
            
        Returns:
            EvaluationResultCollection with reward results per UID
        """
        bt.logging.info(f"🐦 Evaluating {len(briefs)} briefs in emission phase for rewards")
        
        collection = EvaluationResultCollection()
        
        if not briefs:
            bt.logging.warning("No briefs to evaluate")
            return collection
        
        if not uid_account_mappings:
            bt.logging.warning("No UID-account mappings provided")
            return collection
        
        # Create account_username -> uid mapping for quick lookup
        account_to_uid = {
            mapping['account_username']: mapping['uid']
            for mapping in uid_account_mappings
        }
        
        # Create UID -> accounts mapping (for multiple accounts per UID)
        uid_to_accounts = {}
        for mapping in uid_account_mappings:
            uid = mapping['uid']
            username = mapping['account_username']
            if uid not in uid_to_accounts:
                uid_to_accounts[uid] = []
            uid_to_accounts[uid].append(username)
        
        bt.logging.debug(f"Account mappings: {len(account_to_uid)} accounts → {len(uid_to_accounts)} unique UIDs")
        
        # Process each brief
        brief_scores_by_uid = {}  # {uid: {brief_id: usd_amount}}
        contributing_accounts = {}  # {uid: set of account_usernames that actually tweeted}
        
        for brief in briefs:
            brief_id = brief['id']
            pool_name = brief['pool']
            tag = brief.get('tag')  # Optional
            qrt = brief.get('qrt')  # Optional
            budget = brief.get('budget', 0)
            
            # Parse brief dates
            start_date = parse_brief_date(brief.get('start_date'))
            end_date = parse_brief_date(brief.get('end_date'), end_of_day=True)
            
            bt.logging.info(f"📝 Brief {brief_id}: pool={pool_name}, budget=${budget}")
            
            # Try to load reward snapshot first
            try:
                snapshot_data, snapshot_file = load_reward_snapshot(brief_id, pool_name)
                tweet_rewards = snapshot_data['tweet_rewards']
                bt.logging.info(f"📸 Using reward snapshot for brief {brief_id} ({len(tweet_rewards)} tweets)")
                
                # Aggregate to UID level and convert to daily payouts
                uid_total_targets = {}
                uid_to_authors = {}
                for tweet in tweet_rewards:
                    uid = tweet['uid']
                    uid_total_targets[uid] = uid_total_targets.get(uid, 0.0) + tweet['total_usd']
                    uid_to_authors.setdefault(uid, set()).add(tweet['author'])
                
                daily_budget = budget / EMISSIONS_PERIOD
                usd_targets = {uid: total / EMISSIONS_PERIOD for uid, total in uid_total_targets.items()}
                
                # Store results and track contributing accounts
                for uid, usd_amount in usd_targets.items():
                    if uid not in brief_scores_by_uid:
                        brief_scores_by_uid[uid] = {}
                    brief_scores_by_uid[uid][brief_id] = usd_amount
                    contributing_accounts[uid] = contributing_accounts.get(uid, set()) | uid_to_authors[uid]
                
                bt.logging.info(f"  → ${daily_budget:.2f}/day distributed to {len(usd_targets)} UIDs (from snapshot)")
                
                # Log USD rewards by account
                self._log_usd_rewards_by_account(tweet_rewards, account_to_uid, brief_id)
                
                # Convert snapshot to publishing format and publish
                tweets_with_targets = self._convert_snapshot_to_tweets_with_targets(tweet_rewards)
                await self._publish_brief_tweets(
                    brief_id=brief_id,
                    brief=brief,
                    tweets_with_targets=tweets_with_targets,
                    usd_targets=usd_targets,
                    run_id=run_id
                )
                
                continue
                
            except FileNotFoundError:
                # No snapshot - this is first emission run, calculate and save
                bt.logging.info(f"🔍 First emission run for brief {brief_id} - calculating rewards")
            
            # First emission run: score, filter, calculate, and save snapshot
            try:
                # Extract brief-level configuration
                brief_max_members = brief.get('max_members')
                brief_considered = brief.get('max_considered')
                
                # Step 1: Score tweets
                scored_tweets = self._score_tweets_for_brief(
                    pool_name=pool_name,
                    brief_id=brief_id,
                    connected_accounts=connected_accounts,
                    tag=tag,
                    qrt=qrt,
                    run_id=run_id,
                    start_date=start_date,
                    end_date=end_date,
                    max_members=brief_max_members,
                    considered_accounts=brief_considered
                )
                
                if not scored_tweets:
                    bt.logging.warning(f"No scored tweets for brief {brief_id}")
                    continue
                
                bt.logging.debug(f"Scored {len(scored_tweets)} tweets for brief {brief_id}")
                
                # Step 2: Filter tweets by brief criteria (includes max_tweets)
                filtered_tweets = self._filter_tweets_for_brief(
                    scored_tweets=scored_tweets,
                    brief=brief,
                    run_id=run_id,
                    max_tweets=brief.get('max_tweets')
                )
                
                if not filtered_tweets:
                    bt.logging.warning(f"No tweets passed filtering for brief {brief_id}")
                    continue
                
                bt.logging.info(f"  → {len(filtered_tweets)} tweets passed filtering")
                
                # Step 3: Calculate USD/alpha targets per tweet
                daily_budget = budget / EMISSIONS_PERIOD
                tweets_with_targets = self._calculate_tweet_targets(
                    filtered_tweets=filtered_tweets,
                    daily_budget=daily_budget,
                    brief_id=brief_id
                )
                
                # Step 4: Aggregate targets to UID level
                usd_targets = self._aggregate_targets_to_uids(
                    tweets_with_targets=tweets_with_targets,
                    account_to_uid=account_to_uid
                )
                
                if not usd_targets:
                    bt.logging.warning(f"No USD targets for brief {brief_id}")
                    continue
                
                # Build tweet-level reward data for snapshot
                tweet_rewards = []
                for tweet in tweets_with_targets:
                    author = tweet.get('author')
                    if not author:
                        continue
                    
                    uid = account_to_uid.get(author, NOCODE_UID if SIMULATE_CONNECTIONS else None)
                    if uid is None:
                        continue
                    
                    tweet_rewards.append({
                        'tweet_id': tweet.get('tweet_id'),
                        'author': author,
                        'uid': uid,
                        'score': tweet.get('score', 0.0),
                        'total_usd': tweet.get('total_usd_target', 0.0),
                        'favorite_count': tweet.get('favorite_count', 0),
                        'retweet_count': tweet.get('retweet_count', 0),
                        'reply_count': tweet.get('reply_count', 0),
                        'quote_count': tweet.get('quote_count', 0),
                        'bookmark_count': tweet.get('bookmark_count', 0),
                        'retweets': tweet.get('retweets', []),
                        'quotes': tweet.get('quotes', []),
                        'created_at': tweet.get('created_at', ''),
                        'lang': tweet.get('lang', 'und')
                    })
                    
                    # Track contributing accounts
                    if uid in usd_targets:
                        contributing_accounts.setdefault(uid, set()).add(author)
                
                # Save reward snapshot with tweet-level granularity
                snapshot_data = {
                    'brief_id': brief_id,
                    'pool_name': pool_name,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'tweet_rewards': tweet_rewards
                }
                
                try:
                    snapshot_file = save_reward_snapshot(brief_id, pool_name, snapshot_data)
                    bt.logging.info(f"💾 Saved reward snapshot: {len(tweet_rewards)} tweets → {snapshot_file}")
                    self._log_usd_rewards_by_account(tweet_rewards, account_to_uid, brief_id)
                except Exception as e:
                    bt.logging.error(f"Failed to save reward snapshot: {e}")
                
                # Store UID-level results
                for uid, usd_amount in usd_targets.items():
                    if uid not in brief_scores_by_uid:
                        brief_scores_by_uid[uid] = {}
                    brief_scores_by_uid[uid][brief_id] = usd_amount
                
                bt.logging.info(f"  → ${daily_budget:.2f}/day distributed to {len(usd_targets)} UIDs")
                
                # Log top 3 tweets by score for this brief
                self._log_top_tweets(tweets_with_targets, brief_id)
                
                # Step 5: Publish tweet data
                await self._publish_brief_tweets(
                    brief_id=brief_id,
                    brief=brief,
                    tweets_with_targets=tweets_with_targets,
                    usd_targets=usd_targets,
                    run_id=run_id
                )
                
            except Exception as e:
                bt.logging.error(f"Error processing brief {brief_id}: {e}", exc_info=True)
                continue
        
        # Step 5: Create EvaluationResults for each UID
        for uid, brief_scores in brief_scores_by_uid.items():
            # Get accounts that actually contributed tweets (not all mapped accounts)
            accounts = list(contributing_accounts.get(uid, set()))
            
            # Create AccountResults (one per account)
            account_results = {}
            for account in accounts:
                account_results[account] = AccountResult(
                    account_id=account,
                    platform_data={'username': account},
                    content={},  # Twitter tweets (empty for now, could add tweet details later)
                    scores=brief_scores,  # Same scores for all accounts of this UID
                    performance_stats={'platform': 'twitter'},
                    success=True,
                    error_message=""
                )
            
            # Create EvaluationResult
            result = EvaluationResult(
                uid=uid,
                platform="twitter",
                account_results=account_results,
                aggregated_scores=brief_scores,
                metagraph_info={}
            )
            
            collection.add_result(uid, result)
        
        bt.logging.info(f"🎯 Twitter evaluation complete: {len(collection.results)} UIDs evaluated")
        return collection
    
    def _score_tweets_for_brief(
        self,
        pool_name: str,
        brief_id: str,
        connected_accounts: set,
        tag: Optional[str],
        qrt: Optional[str],
        run_id: Optional[str],
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        max_members: Optional[int] = None,
        considered_accounts: Optional[int] = None
    ) -> List[Dict]:
        """
        Score tweets using tweet_scoring module.
        
        Always scores fresh to capture new tweets and evolving engagement.
        Results are saved to disk for auditing but not reused as snapshots.
        
        Args:
            pool_name: Pool name
            brief_id: Brief identifier
            connected_accounts: Set of connected account usernames to filter scoring
            tag: Optional tag filter
            qrt: Optional quote tweet filter
            run_id: Run identifier
            start_date: Brief start date (inclusive)
            end_date: Brief end date (inclusive)
            max_members: Optional brief-level max members limit
            considered_accounts: Optional brief-level considered accounts limit
        
        Returns:
            List of dicts with keys: author, tweet_id, score
        """
        try:
            # Always score tweets fresh (no snapshot loading)
            scored_tweets = score_tweets_for_pool(
                pool_name=pool_name,
                brief_id=brief_id,
                connected_accounts=connected_accounts,
                run_id=run_id,
                tag=tag,
                qrt=qrt,
                start_date=start_date,
                end_date=end_date,
                max_members=max_members,
                considered_accounts_limit=considered_accounts
            )
            
            # Files are saved by score_tweets_for_pool() for audit purposes
            # but we don't load old scores - always use fresh data
            
            return scored_tweets
                
        except Exception as e:
            bt.logging.error(f"Error scoring tweets for pool {pool_name}, brief {brief_id}: {e}")
            return []
    
    def _filter_tweets_for_brief(
        self,
        scored_tweets: List[Dict],
        brief: Dict[str, Any],
        run_id: Optional[str],
        max_tweets: Optional[int] = None
    ) -> List[Dict]:
        """
        Filter scored tweets using existing tweet_filtering module.
        
        Args:
            scored_tweets: List of scored tweets from score_tweets_for_pool()
            brief: Brief dict with id, brief text, start_date, end_date
            run_id: Run identifier
            max_tweets: Maximum tweets per account (passed to filter module)
            
        Returns:
            List of filtered tweets (only those meeting brief)
        """
        try:
            brief_id = brief['id']
            brief_text = brief.get('brief', '')
            prompt_version = brief.get('prompt_version', 1)
            
            # Filter tweets using LLM evaluation (includes max_tweets filtering)
            filtered_results = filter_tweets_for_brief(
                brief_id=brief_id,
                brief_text=brief_text,
                prompt_version=prompt_version,
                run_id=run_id,
                max_tweets=max_tweets
            )
            
            # Filter to only tweets that meet brief
            passed_tweets = [t for t in filtered_results if t.get('meets_brief', False)]
            return passed_tweets
            
        except Exception as e:
            bt.logging.error(f"Error filtering tweets for brief {brief.get('id')}: {e}")
            return []
    
    def _calculate_tweet_targets(
        self,
        filtered_tweets: List[Dict],
        daily_budget: float,
        brief_id: str
    ) -> List[Dict]:
        """
        Calculate USD and alpha targets for each tweet based on engagement scores.
        
        Applies power law smoothing to create more separation between high and 
        low performers while maintaining budget constraints.
        
        Args:
            filtered_tweets: Tweets that passed LLM filtering
            daily_budget: Brief budget / EMISSIONS_PERIOD
            brief_id: For logging
            
        Returns:
            Tweets with added usd_target and alpha_target fields
        """
        from bitcast.validator.utils.token_pricing import get_bitcast_alpha_price
        
        # Apply power law smoothing to scores before calculating proportions
        smoothed_scores = [t.get('score', 0.0) ** REWARD_SMOOTHING_EXPONENT for t in filtered_tweets]
        
        total_smoothed = sum(smoothed_scores)
        if total_smoothed == 0:
            bt.logging.warning(f"Total smoothed score is 0 for brief {brief_id}")
            return filtered_tweets
        
        alpha_price = get_bitcast_alpha_price()
        
        bt.logging.debug(f"Applied power law smoothing (exponent={REWARD_SMOOTHING_EXPONENT}) to {len(filtered_tweets)} tweets")
        
        for tweet, smoothed_score in zip(filtered_tweets, smoothed_scores):
            proportion = smoothed_score / total_smoothed
            tweet['usd_target'] = daily_budget * proportion
            tweet['total_usd_target'] = tweet['usd_target'] * EMISSIONS_PERIOD
            tweet['alpha_target'] = tweet['usd_target'] / alpha_price
        
        bt.logging.debug(f"Calculated targets for {len(filtered_tweets)} tweets (${daily_budget:.2f} total)")
        return filtered_tweets
    
    def _aggregate_targets_to_uids(
        self,
        tweets_with_targets: List[Dict],
        account_to_uid: Dict[str, int]
    ) -> Dict[int, float]:
        """
        Aggregate tweet USD targets to UID level.
        
        Args:
            tweets_with_targets: Tweets with usd_target field
            account_to_uid: Author -> UID mapping
            
        Returns:
            Dict of {uid: total_usd_target}
        """
        uid_targets = {}
        
        for tweet in tweets_with_targets:
            author = tweet.get('author')
            if not author:
                continue
            
            uid = account_to_uid.get(author, NOCODE_UID if SIMULATE_CONNECTIONS else None)
            if uid is None:
                continue
            
            usd_target = tweet.get('usd_target', 0.0)
            uid_targets[uid] = uid_targets.get(uid, 0.0) + usd_target
        
        bt.logging.debug(f"Aggregated to {len(uid_targets)} UIDs: {list(uid_targets.keys())}")
        return uid_targets
    
    def _convert_snapshot_to_tweets_with_targets(
        self,
        tweet_rewards: List[Dict]
    ) -> List[Dict]:
        """
        Convert snapshot tweet_rewards to tweets_with_targets format for publishing.
        
        Args:
            tweet_rewards: Snapshot data with tweet_id, author, uid, score, total_usd,
                          and engagement metrics
            
        Returns:
            List of tweets in tweets_with_targets format with daily USD/alpha targets
        """
        alpha_price = get_bitcast_alpha_price()
        tweets_with_targets = []
        
        for tweet_reward in tweet_rewards:
            daily_usd = tweet_reward.get('total_usd', 0.0) / EMISSIONS_PERIOD
            tweets_with_targets.append({
                'tweet_id': tweet_reward.get('tweet_id'),
                'author': tweet_reward.get('author'),
                'score': tweet_reward.get('score', 0.0),
                'usd_target': daily_usd,
                'total_usd_target': tweet_reward.get('total_usd', 0.0),
                'alpha_target': daily_usd / alpha_price,
                # Include engagement metrics from snapshot
                'favorite_count': tweet_reward.get('favorite_count', 0),
                'retweet_count': tweet_reward.get('retweet_count', 0),
                'reply_count': tweet_reward.get('reply_count', 0),
                'quote_count': tweet_reward.get('quote_count', 0),
                'bookmark_count': tweet_reward.get('bookmark_count', 0),
                'retweets': tweet_reward.get('retweets', []),
                'quotes': tweet_reward.get('quotes', []),
                'created_at': tweet_reward.get('created_at', ''),
                'lang': tweet_reward.get('lang', 'und')
            })
        
        return tweets_with_targets
    
    def _log_usd_rewards_by_account(
        self,
        tweet_rewards: List[Dict],
        account_to_uid: Dict[str, int],
        brief_id: str
    ) -> None:
        """
        Log USD rewards by account in descending order.
        
        Args:
            tweet_rewards: List of tweets with 'author' and 'total_usd' fields
            account_to_uid: Mapping from account username to UID
            brief_id: Brief identifier for logging
        """
        account_usd_totals = {}
        for tweet in tweet_rewards:
            author = tweet.get('author')
            if author:
                total_usd = tweet.get('total_usd', 0.0)
                account_usd_totals[author] = account_usd_totals.get(author, 0.0) + total_usd
        
        sorted_accounts = sorted(account_usd_totals.items(), key=lambda x: x[1], reverse=True)
        bt.logging.info(f"💰 USD rewards by account for brief {brief_id}:")
        for author, total_usd in sorted_accounts:
            uid = account_to_uid.get(author, 'unknown')
            bt.logging.info(f"    @{author} (UID {uid}): ${total_usd:.2f}")
    
    def _log_top_tweets(
        self,
        tweets_with_targets: List[Dict],
        brief_id: str
    ) -> None:
        """
        Log top 5 tweets by score with considered account engagement.
        
        Args:
            tweets_with_targets: Tweets with score and engagement metrics
            brief_id: Brief identifier for logging
        """
        if not tweets_with_targets:
            return
        
        # Sort by score and get top 5
        sorted_by_score = sorted(tweets_with_targets, key=lambda t: t.get('score', 0.0), reverse=True)
        
        bt.logging.info(f"  📊 Top 5 tweets by score:")
        for i, tweet in enumerate(sorted_by_score[:5], 1):
            retweets = tweet.get('retweets', [])
            quotes = tweet.get('quotes', [])
            
            bt.logging.info(
                f"    {i}. @{tweet.get('author', 'unknown')} (score: {tweet.get('score', 0):.6f}) - "
                f"🔁 {len(retweets)} RTs, 💭 {len(quotes)} QRTs"
            )
    
    async def _publish_brief_tweets(
        self,
        brief_id: str,
        brief: Dict[str, Any],
        tweets_with_targets: List[Dict[str, Any]],
        usd_targets: Dict[int, float],
        run_id: str
    ) -> None:
        """
        Publish tweet data with pre-calculated USD/alpha targets.
        
        Args:
            brief_id: Brief identifier
            brief: Brief configuration dict
            tweets_with_targets: Tweets with usd_target and alpha_target fields
            usd_targets: UID-level USD targets (for summary)
            run_id: Validation run identifier
        """
        if not ENABLE_DATA_PUBLISH:
            return
            
        try:
            payload = create_tweet_payload(
                brief_id=brief_id,
                pool_name=brief.get('pool', 'unknown'),
                tweets_with_targets=tweets_with_targets,
                brief_metadata={
                    "tag": brief.get('tag'),
                    "qrt": brief.get('qrt'),
                    "budget": brief.get('budget', 0.0),
                    "daily_budget": brief.get('budget', 0.0) / EMISSIONS_PERIOD
                },
                uid_targets=usd_targets
            )
            
            # Publish with fire-and-forget pattern
            success = await publish_brief_tweets(
                brief_tweets_data=payload,
                run_id=run_id,
                endpoint=TWEETS_SUBMIT_ENDPOINT
            )
            
            if success:
                tweet_count = len(tweets_with_targets)
                bt.logging.debug(f"Published {tweet_count} filtered tweets for brief {brief_id}")
            else:
                bt.logging.debug(f"Tweet publishing failed for brief {brief_id} (continuing...)")
                
        except Exception as e:
            # Fire-and-forget: log error but don't raise
            bt.logging.error(f"Exception during tweet publishing for brief {brief_id}: {e}")
            # Continue processing - publishing failures should not break validation

