"""
Main tweet filtering orchestrator.

Loads scored tweets for a brief, evaluates them against the brief using LLM,
and saves the filtered results.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import bittensor as bt

from bitcast.validator.utils.data_publisher import get_global_publisher

from .scored_tweets_loader import load_latest_scored_tweets
from .brief_evaluator import BriefEvaluator


def save_filtered_tweets(
    brief_id: str,
    filtered_tweets: List[Dict],
    passed_tweets: List[Dict],
    failed_tweets: List[Dict],
    metadata: Dict
) -> str:
    """
    Save filtered tweets and metadata to file.
    
    Args:
        brief_id: Brief identifier
        filtered_tweets: All evaluated tweets
        passed_tweets: Tweets that met the brief
        failed_tweets: Tweets that didn't meet the brief
        metadata: Metadata dict
        
    Returns:
        Path to saved file
    """
    # Create output directory (brief-level, not pool-level)
    output_dir = Path(__file__).parent / "filtered_tweets"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate timestamp
    timestamp_str = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    
    # Save filtered tweets with metadata
    output_file = output_dir / f"{brief_id}_{timestamp_str}.json"
    output_data = {
        'metadata': metadata,
        'filtered_tweets': filtered_tweets,
        'passed_tweets': passed_tweets,
        'failed_tweets': failed_tweets
    }
    
    with open(output_file, 'w') as f:
        json.dump(output_data, f, indent=2)
    
    bt.logging.debug(f"Saved filtered tweets to {output_file}")
    
    return str(output_file)


def apply_max_tweets_filter(
    filtered_tweets: List[Dict],
    max_tweets: Optional[int],
    brief_id: str
) -> List[Dict]:
    """
    Limit the number of tweets per account based on max_tweets setting.
    
    For each account, if they have more than max_tweets qualifying tweets,
    keep only the top max_tweets by engagement score (using timestamp as tiebreaker).
    
    Args:
        filtered_tweets: Tweets that passed LLM filtering
        max_tweets: Maximum tweets per account (None or 0 = no limit)
        brief_id: For logging
        
    Returns:
        Filtered list with max_tweets applied per account
    """
    from collections import defaultdict
    
    # No limit if max_tweets not set or <= 0
    if not max_tweets or max_tweets <= 0:
        return filtered_tweets
    
    # Group by author
    tweets_by_author = defaultdict(list)
    for tweet in filtered_tweets:
        author = tweet.get('author', '')
        if author:
            tweets_by_author[author].append(tweet)
    
    # Apply limit per author
    limited_tweets = []
    total_excluded = 0
    accounts_limited = 0
    
    for author, author_tweets in tweets_by_author.items():
        if len(author_tweets) <= max_tweets:
            limited_tweets.extend(author_tweets)
        else:
            # Sort by score (descending), then by timestamp (ascending) as tiebreaker
            sorted_tweets = sorted(
                author_tweets,
                key=lambda t: (
                    -t.get('score', 0.0),  # Higher score first (negative for desc)
                    t.get('created_at', '')  # Earlier timestamp first (asc)
                )
            )
            limited_tweets.extend(sorted_tweets[:max_tweets])
            excluded = len(author_tweets) - max_tweets
            total_excluded += excluded
            accounts_limited += 1
            
            bt.logging.debug(
                f"  @{author}: {len(author_tweets)} tweets → {max_tweets} "
                f"(excluded {excluded} lower-scored tweets)"
            )
    
    if accounts_limited > 0:
        bt.logging.debug(
            f"Applied max_tweets={max_tweets} for brief {brief_id}: "
            f"{len(limited_tweets)}/{len(filtered_tweets)} tweets kept, "
            f"{total_excluded} excluded from {accounts_limited} accounts"
        )
    
    return limited_tweets


def filter_tweets_for_brief(
    brief_id: str,
    brief_text: str,
    prompt_version: int = 1,
    run_id: Optional[str] = None,
    max_workers: int = 10,
    max_tweets: Optional[int] = None
) -> List[Dict]:
    """
    Filter scored tweets for a brief using LLM evaluation.
    
    This is the main entry point for tweet filtering. It:
    1. Loads the latest scored tweets for the brief_id
    2. Constructs a brief dict for LLM evaluation
    3. Evaluates each tweet against the brief using LLM
    4. Applies max_tweets limit per account (if specified)
    5. Saves results with passed/failed separation
    
    Args:
        brief_id: Brief identifier (e.g., '001_bitcast')
        brief_text: Brief requirements text
        prompt_version: Prompt version to use (default: 1)
        run_id: Optional run identifier (auto-generated if not provided)
        max_workers: Max parallel workers for LLM evaluation (default: 10)
        max_tweets: Maximum tweets per account (None or 0 = no limit)
        
    Returns:
        List of dicts with keys: author, tweet_id, meets_brief, score
        Complete results are also saved to file
        
    Raises:
        FileNotFoundError: If no scored tweets exist for brief_id
        ValueError: If scored tweets file is invalid
    """
    start_time = time.time()
    
    bt.logging.info(f"🔍 Starting tweet filtering: brief={brief_id}")
    
    # Generate run_id if not provided
    if run_id is None:
        try:
            publisher = get_global_publisher()
            vali_hotkey = publisher.wallet.hotkey.ss58_address
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = f"tweet_filtering_{brief_id}_{vali_hotkey}_{timestamp}"
        except RuntimeError:
            # Global publisher not initialized - fallback to timestamp
            run_id = f"tweet_filtering_{brief_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    bt.logging.debug(f"Run ID: {run_id}")
    bt.logging.debug(f"Brief text: {brief_text[:100]}..." if len(brief_text) > 100 else f"Brief text: {brief_text}")
    bt.logging.debug(f"Prompt version: {prompt_version}")
    
    # Step 1: Load scored tweets
    bt.logging.debug("Loading scored tweets")
    
    scored_data, scoring_file = load_latest_scored_tweets(brief_id)
    scored_tweets = scored_data.get('scored_tweets', [])
    scoring_metadata = scored_data.get('metadata', {})
    
    bt.logging.debug(f"Scoring file: {scoring_file}")
    bt.logging.info(f"  → Loaded {len(scored_tweets)} scored tweets")
    
    if not scored_tweets:
        bt.logging.warning("No tweets to evaluate (scored tweets list is empty)")
        # Return empty results but still save metadata
        metadata = {
            'run_id': run_id,
            'brief_id': brief_id,
            'brief_text': brief_text,
            'created_at': datetime.now().isoformat(),
            'source_scoring_file': scoring_file,
            'prompt_version': prompt_version,
            'total_evaluated': 0,
            'passed_count': 0,
            'failed_count': 0,
            'pass_rate': 0.0,
            'execution_time_seconds': round(time.time() - start_time, 2)
        }
        save_filtered_tweets(brief_id, [], [], [], metadata)
        return []
    
    # Step 2: Construct brief dict
    bt.logging.debug("Constructing brief for evaluation")
    
    brief_dict = {
        'id': brief_id,
        'brief': brief_text,
        'prompt_version': prompt_version
    }
    
    bt.logging.debug(f"Brief ID: {brief_dict['id']}, Prompt version: {brief_dict['prompt_version']}")
    
    # Step 3: Evaluate tweets using LLM
    bt.logging.debug("Evaluating tweets against brief")
    
    evaluator = BriefEvaluator(brief_dict, max_workers=max_workers)
    evaluation_start = time.time()
    
    filtered_tweets = evaluator.evaluate_tweets_batch(scored_tweets)
    
    evaluation_time = time.time() - evaluation_start
    bt.logging.debug(f"Evaluation complete in {evaluation_time:.1f}s")
    
    # Step 4: Separate passed and failed tweets
    bt.logging.debug("Analyzing results")
    
    passed_tweets = [t for t in filtered_tweets if t.get('meets_brief', False)]
    failed_tweets = [t for t in filtered_tweets if not t.get('meets_brief', False)]
    
    pass_rate = len(passed_tweets) / len(filtered_tweets) if filtered_tweets else 0.0
    
    bt.logging.info(f"  → {len(passed_tweets)}/{len(filtered_tweets)} tweets passed ({pass_rate:.1%})")
    
    # Step 4b: Apply max_tweets filter to passed tweets
    if max_tweets and max_tweets > 0:
        bt.logging.debug(f"Applying max_tweets={max_tweets} filter")
        original_count = len(passed_tweets)
        passed_tweets = apply_max_tweets_filter(passed_tweets, max_tweets, brief_id)
        if original_count != len(passed_tweets):
            bt.logging.info(f"  → {len(passed_tweets)}/{original_count} tweets after max_tweets filter")
    
    if passed_tweets and len(passed_tweets) <= 5:
        bt.logging.debug(f"Passed tweets:")
        for i, tweet in enumerate(passed_tweets[:5], 1):
            bt.logging.debug(
                f"  {i}. @{tweet['author']} (score: {tweet.get('score', 0):.6f}) - "
                f"{tweet.get('reasoning', 'No reasoning')[:80]}..."
            )
    
    # Step 5: Build metadata and save
    bt.logging.debug("Saving results")
    
    # Try to get validator hotkey
    validator_hotkey = None
    try:
        publisher = get_global_publisher()
        validator_hotkey = publisher.wallet.hotkey.ss58_address
    except RuntimeError:
        pass
    
    metadata = {
        'run_id': run_id,
        'brief_id': brief_id,
        'brief_text': brief_text,
        'pool_name': scoring_metadata.get('pool_name'),
        'created_at': datetime.now().isoformat(),
        'source_scoring_file': scoring_file,
        'source_scoring_run_id': scoring_metadata.get('run_id'),
        'prompt_version': prompt_version,
        'max_tweets': max_tweets,
        'total_evaluated': len(filtered_tweets),
        'passed_count': len(passed_tweets),
        'failed_count': len(failed_tweets),
        'pass_rate': round(pass_rate, 4),
        'execution_time_seconds': round(time.time() - start_time, 2),
        'validator_hotkey': validator_hotkey
    }
    
    output_file = save_filtered_tweets(
        brief_id,
        filtered_tweets,
        passed_tweets,
        failed_tweets,
        metadata
    )
    
    # Final summary
    total_time = time.time() - start_time
    bt.logging.debug(f"✅ Tweet filtering complete: {len(passed_tweets)} passed ({total_time:.1f}s)")
    bt.logging.debug(f"Output: {output_file}")
    
    # Return complete tweet data for programmatic use
    # Preserve all fields from scored tweets for downstream processing
    # Use passed_tweets + failed_tweets (after max_tweets filter applied)
    result = [
        {
            'author': tweet['author'],
            'tweet_id': tweet['tweet_id'],
            'meets_brief': tweet['meets_brief'],
            'score': tweet.get('score', 0.0),
            'text': tweet.get('text', ''),
            'url': tweet.get('url', ''),
            'created_at': tweet.get('created_at', ''),
            'lang': tweet.get('lang', 'und'),
            # Engagement metrics (from Twitter API via scored tweets)
            'favorite_count': tweet.get('favorite_count', 0),
            'retweet_count': tweet.get('retweet_count', 0),
            'reply_count': tweet.get('reply_count', 0),
            'quote_count': tweet.get('quote_count', 0),
            'bookmark_count': tweet.get('bookmark_count', 0),
            # Scoring engagement lists (who retweeted/quoted from considered accounts)
            'retweets': tweet.get('retweets', []),
            'quotes': tweet.get('quotes', []),
            'quoted_tweet_id': tweet.get('quoted_tweet_id')  # For QRT transparency
        }
        for tweet in passed_tweets + failed_tweets
    ]
    
    return result


# CLI interface for standalone execution
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    from bitcast.validator.reward_engine.utils import get_briefs
    
    # Load environment variables
    env_path = Path(__file__).parents[2] / '.env'
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from {env_path}")
    
    try:
        # Create argument parser
        parser = argparse.ArgumentParser(
            description="Filter scored tweets for a brief using LLM evaluation"
        )
        bt.logging.add_args(parser)
        
        parser.add_argument(
            "--brief-id",
            type=str,
            default=None,
            help="Brief identifier (fetches brief text and prompt version from brief server) (required)"
        )
        
        parser.add_argument(
            "--max-workers",
            type=int,
            default=10,
            help="Max parallel workers for LLM evaluation (default: 10)"
        )
        
        # Build args list from command line
        import sys
        args_list = sys.argv[1:]
        
        # Add info logging if no logging level specified
        if not any(arg.startswith('--logging.') for arg in args_list):
            args_list.insert(0, '--logging.info')
        
        # Parse configuration
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Validate required arguments
        if not config.brief_id:
            raise ValueError("--brief-id is required")
        
        # Fetch brief from server
        bt.logging.info(f"Fetching brief '{config.brief_id}' from brief server...")
        briefs = get_briefs()
        brief_data = next((b for b in briefs if b['id'] == config.brief_id), None)
        
        if not brief_data:
            raise ValueError(f"Brief ID '{config.brief_id}' not found on brief server")
        
        # Extract brief parameters
        brief_text = brief_data.get('brief', '')
        prompt_version = int(brief_data.get('prompt_version', 1))
        max_tweets = brief_data.get('max_tweets')
        
        if not brief_text:
            raise ValueError(f"Brief text is empty for brief ID '{config.brief_id}'")
        
        bt.logging.info(f"  → Brief text: {brief_text[:80]}{'...' if len(brief_text) > 80 else ''}")
        bt.logging.info(f"  → Prompt version: {prompt_version}")
        if max_tweets:
            bt.logging.info(f"  → Max tweets per account: {max_tweets}")
        
        # Generate run_id for CLI execution
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"tweet_filtering_cli_{timestamp}"
        
        # Run tweet filtering
        results = filter_tweets_for_brief(
            brief_id=config.brief_id,
            brief_text=brief_text,
            prompt_version=prompt_version,
            run_id=run_id,
            max_workers=config.max_workers,
            max_tweets=max_tweets
        )
        
        # Print summary
        passed_tweets = [r for r in results if r['meets_brief']]
        failed_tweets = [r for r in results if not r['meets_brief']]
        
        print(f"\n✅ Tweet filtering complete:")
        print(f"   Total evaluated: {len(results)}")
        print(f"   Passed: {len(passed_tweets)}")
        print(f"   Failed: {len(failed_tweets)}")
        
        if passed_tweets:
            # Calculate budget split if available
            budget = brief_data.get('budget')
            total_score = sum(t['score'] for t in passed_tweets)
            show_budget = budget and budget > 0 and total_score > 0
            
            if show_budget:
                print(f"\n✅ Passed Tweets (sorted by score, ${budget:,.2f} budget):")
                print(f"{'Rank':<6} {'Score':<12} {'Budget $':<12} {'Author':<20} {'Tweet ID'}")
                print("-" * 100)
            else:
                print(f"\n✅ Passed Tweets (sorted by score):")
                print(f"{'Rank':<6} {'Score':<12} {'Author':<20} {'Tweet ID'}")
                print("-" * 80)
            
            sorted_tweets = sorted(passed_tweets, key=lambda t: t['score'], reverse=True)
            
            for idx, tweet in enumerate(sorted_tweets[:20], 1):
                author = tweet['author']
                tweet_id = tweet['tweet_id']
                score = tweet['score']
                
                if show_budget:
                    budget_allocation = budget * (score / total_score)
                    print(f"{idx:<6} {score:<12.6f} ${budget_allocation:<11.2f} @{author:<19} {tweet_id}")
                else:
                    print(f"{idx:<6} {score:<12.6f} @{author:<19} {tweet_id}")
            
            if len(passed_tweets) > 20:
                print(f"\n... and {len(passed_tweets) - 20} more passed tweets")
        
        if failed_tweets and len(failed_tweets) <= 10:
            print(f"\n❌ Failed Tweets:")
            print(f"{'Author':<20} {'Tweet ID'}")
            print("-" * 60)
            
            for tweet in failed_tweets:
                author = tweet['author']
                tweet_id = tweet['tweet_id']
                print(f"@{author:<19} {tweet_id}")
        elif failed_tweets:
            print(f"\n❌ {len(failed_tweets)} tweets failed brief evaluation")
        
    except KeyboardInterrupt:
        print("\n\n❌ Cancelled by user")
        exit(1)
    except Exception as e:
        bt.logging.error(f"Tweet filtering failed: {e}", exc_info=True)
        print(f"❌ Error: {e}")
        exit(1)

