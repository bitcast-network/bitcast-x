"""
Brief evaluation wrapper using ChuteClient LLM.

Wraps the existing evaluate_content_against_brief function with
batching, parallel processing, and error handling.
"""

import time
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import bittensor as bt

from bitcast.validator.clients.ChuteClient import evaluate_content_against_brief
from bitcast.validator.utils.config import SOCIAL_DISCOVERY_MAX_WORKERS


class BriefEvaluator:
    """
    Evaluates tweets against a brief using LLM.
    
    Provides single tweet and batch evaluation with parallel processing.
    """
    
    def __init__(self, brief: Dict, max_workers: int = None):
        """
        Initialize evaluator with brief.
        
        Args:
            brief: Brief dict with 'id', 'brief' text, and optional 'prompt_version'
            max_workers: Max parallel workers for batch evaluation
                        (defaults to SOCIAL_DISCOVERY_MAX_WORKERS)
        """
        self.brief = brief
        self.max_workers = max_workers or SOCIAL_DISCOVERY_MAX_WORKERS
        
        # Validate brief has required fields
        if 'brief' not in brief:
            raise ValueError("Brief must contain 'brief' text field")
        if 'id' not in brief:
            raise ValueError("Brief must contain 'id' field")
    
    def evaluate_tweet(self, tweet: Dict) -> Dict:
        """
        Evaluate a single tweet against the brief.
        
        Args:
            tweet: Tweet dict with at least 'text', 'tweet_id', 'author'
            
        Returns:
            Tweet dict enriched with 'meets_brief' and 'reasoning' fields.
            All original tweet fields are preserved.
        """
        # Extract tweet text
        tweet_text = tweet.get('text', '')
        
        if not tweet_text:
            bt.logging.warning(
                f"Tweet {tweet.get('tweet_id', 'unknown')} has no text, marking as failed"
            )
            return {
                **tweet,
                'meets_brief': False,
                'reasoning': 'Tweet has no text content'
            }
        
        # Evaluate using ChuteClient
        try:
            meets_brief, reasoning = evaluate_content_against_brief(
                self.brief, 
                tweet_text, 
                tweet_id=tweet.get('tweet_id'),
                author=tweet.get('author')
            )
            
            return {
                **tweet,
                'meets_brief': meets_brief,
                'reasoning': reasoning
            }
            
        except Exception as e:
            bt.logging.error(
                f"LLM evaluation failed for tweet {tweet.get('tweet_id', 'unknown')}: {e}"
            )
            return {
                **tweet,
                'meets_brief': False,
                'reasoning': f'Evaluation failed: {str(e)}'
            }
    
    def evaluate_tweets_batch(self, tweets: List[Dict]) -> List[Dict]:
        """
        Evaluate multiple tweets in parallel.
        
        Args:
            tweets: List of tweet dicts
            
        Returns:
            List of enriched tweet dicts with evaluation results
        """
        if not tweets:
            bt.logging.warning("No tweets to evaluate")
            return []
        
        bt.logging.info(f"Evaluating {len(tweets)} tweets with {self.max_workers} workers")
        
        evaluated_tweets = []
        failed_count = 0
        start_time = time.time()
        
        # Use ThreadPoolExecutor for parallel evaluation
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all evaluation tasks
            future_to_tweet = {
                executor.submit(self.evaluate_tweet, tweet): tweet
                for tweet in tweets
            }
            
            # Process completed evaluations
            for i, future in enumerate(as_completed(future_to_tweet), 1):
                try:
                    result = future.result()
                    evaluated_tweets.append(result)
                    
                    # Log progress every 10 tweets or at the end
                    if i % 10 == 0 or i == len(tweets):
                        elapsed = time.time() - start_time
                        rate = i / elapsed if elapsed > 0 else 0
                        bt.logging.info(
                            f"Progress: {i}/{len(tweets)} tweets evaluated "
                            f"({rate:.1f} tweets/sec)"
                        )
                    
                    # Track failures
                    if not result.get('meets_brief', False):
                        if 'Evaluation failed' in result.get('reasoning', ''):
                            failed_count += 1
                            
                except Exception as e:
                    # This should rarely happen since evaluate_tweet handles errors
                    tweet = future_to_tweet[future]
                    bt.logging.error(f"Unexpected error processing tweet: {e}")
                    evaluated_tweets.append({
                        **tweet,
                        'meets_brief': False,
                        'reasoning': f'Unexpected error: {str(e)}'
                    })
                    failed_count += 1
        
        elapsed = time.time() - start_time
        bt.logging.info(
            f"Batch evaluation complete: {len(evaluated_tweets)} tweets in {elapsed:.1f}s"
        )
        
        if failed_count > 0:
            bt.logging.warning(f"{failed_count} tweets had evaluation failures")
        
        return evaluated_tweets

