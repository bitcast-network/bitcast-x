"""Brief tweet publishing utilities for reward engine."""

import bittensor as bt
from datetime import datetime, timezone
from typing import Dict, List, Any
from bitcast.validator.utils.data_publisher import get_global_publisher


async def publish_brief_tweets(
    brief_tweets_data: Dict[str, Any], 
    run_id: str,
    endpoint: str
) -> bool:
    """
    Publish comprehensive tweet data for a specific brief to external storage.
    
    This function publishes detailed tweet information including metadata, scores,
    engagement metrics, and financial targets for tweets that passed LLM filtering
    for a given brief. Publishes synchronously with instant server response.
    
    Args:
        brief_tweets_data: Complete brief tweet data payload containing:
            - brief_id: Brief identifier  
            - tweets: List of tweet objects (ID, URL, engagement, scores, targets)
            - summary: Aggregated statistics (totals, unique creators)
            - timestamp: ISO format timestamp
        run_id: Validation cycle identifier
        endpoint: Target endpoint URL for publishing
        
    Returns:
        bool: True if successful, False if failed
        
    Example:
        >>> data = {
        ...     "brief_id": "001_example",
        ...     "tweets": [...],
        ...     "summary": {"total_tweets": 5, "total_usd_target": 142.50, "unique_creators": 4}
        ... }
        >>> success = await publish_brief_tweets(data, "run_123", endpoint_url)
    """
    try:
        # Validate required fields
        if not brief_tweets_data or not isinstance(brief_tweets_data, dict):
            bt.logging.warning("Invalid brief_tweets_data provided - skipping publish")
            return False
            
        if not brief_tweets_data.get("brief_id"):
            bt.logging.warning("Missing brief_id in tweet data - skipping publish")
            return False
            
        # Add timestamp if not present
        if "timestamp" not in brief_tweets_data:
            brief_tweets_data["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        # Get global publisher
        publisher = get_global_publisher()
        
        # Log before publishing
        brief_id = brief_tweets_data.get("brief_id", "unknown")
        tweet_count = brief_tweets_data.get("summary", {}).get("total_tweets", 0)
        bt.logging.info(f"📤 Publishing {tweet_count} tweets for brief {brief_id} to {endpoint}")
        
        # Publish using unified format
        success = await publisher.publish_unified_payload(
            payload_type="brief_tweets",
            run_id=run_id,
            payload_data=brief_tweets_data,
            endpoint=endpoint
        )
        
        if success:
            brief_id = brief_tweets_data.get("brief_id", "unknown")
            tweet_count = brief_tweets_data.get("summary", {}).get("total_tweets", 0)
            bt.logging.info(f"✅ Published {tweet_count} tweets for brief {brief_id}")
        else:
            bt.logging.error(f"❌ Failed to publish tweets for brief {brief_tweets_data.get('brief_id', 'unknown')}")
            
        return success
        
    except Exception as e:
        # Log error but don't raise exception to avoid breaking the evaluation cycle
        brief_id = brief_tweets_data.get("brief_id", "unknown") if brief_tweets_data else "unknown"
        bt.logging.error(f"Exception publishing tweets for brief {brief_id}: {e}")
        return False


def create_tweet_payload(
    brief_id: str,
    pool_name: str,
    tweets_with_targets: List[Dict[str, Any]],
    brief_metadata: Dict[str, Any],
    uid_targets: Dict[int, float]
) -> Dict[str, Any]:
    """
    Create payload for tweet publishing.
    
    Args:
        brief_id: Brief identifier
        pool_name: Pool name
        tweets_with_targets: Tweets with pre-calculated usd_target and alpha_target
        brief_metadata: Brief configuration (tag, qrt, budget, daily_budget)
        uid_targets: UID-level USD targets for summary
        
    Returns:
        Payload dict ready for publishing
    """
    try:
        # Extract metadata
        tag = brief_metadata.get("tag")
        qrt = brief_metadata.get("qrt") 
        budget = brief_metadata.get("budget", 0.0)
        daily_budget = brief_metadata.get("daily_budget", 0.0)
        
        # Process tweets (targets already calculated)
        processed_tweets = []
        unique_creators = set()
        
        for idx, tweet in enumerate(tweets_with_targets):
            author = tweet.get("author", "")
            tweet_id = tweet.get("tweet_id", "")
            unique_creators.add(author)
            
            processed_tweets.append({
                # Tweet metadata
                "tweet_id": tweet_id,
                "author": author,
                "text": tweet.get("text", ""),
                "created_at": tweet.get("created_at", ""),
                "lang": tweet.get("lang", "und"),
                
                # Engagement metrics
                "favorite_count": tweet.get("favorite_count", 0),
                "retweet_count": tweet.get("retweet_count", 0),
                "reply_count": tweet.get("reply_count", 0),
                "quote_count": tweet.get("quote_count", 0),
                "bookmark_count": tweet.get("bookmark_count", 0),
                
                # Scoring data
                "score": tweet.get("score", 0.0),
                "retweets": tweet.get("retweets", []),
                "quotes": tweet.get("quotes", []),
                
                # Brief evaluation
                "meets_brief": True,
                
                # Financial targets (pre-calculated)
                "usd_target": tweet.get("usd_target", 0.0),
                "total_usd_target": tweet.get("total_usd_target", 0.0),
                "alpha_target": tweet.get("alpha_target", 0.0)
            })
        
        # Create complete payload
        payload = {
            "brief_id": brief_id,
            "tweets": processed_tweets,
            "summary": {
                "total_tweets": len(processed_tweets),
                "total_usd_target": sum(uid_targets.values()),
                "unique_creators": len(unique_creators),
                "uid_usd_targets": uid_targets
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        return payload
        
    except Exception as e:
        bt.logging.error(f"Error creating tweet payload for brief {brief_id}: {e}")
        # Return minimal valid payload on error
        return {
            "brief_id": brief_id,
            "tweets": [],
            "summary": {"total_tweets": 0, "total_usd_target": 0.0, "unique_creators": 0},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

