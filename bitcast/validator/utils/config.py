import os
import sys
from dotenv import load_dotenv
from pathlib import Path
import bittensor as bt

env_path = Path(__file__).parents[1] / '.env'
load_dotenv(dotenv_path=env_path)

__version__ = "1.2.5"

# =============================================================================
# Cache Configuration
# =============================================================================

# Cache directories
CACHE_ROOT = Path(__file__).resolve().parents[2] / "cache"
CACHE_DIRS = {
    "briefs": os.path.join(CACHE_ROOT, "briefs"),
    "twitter": os.path.join(CACHE_ROOT, "twitter"),
    "llm": os.path.join(CACHE_ROOT, "llm")
}

# Cache expiry settings
CACHE_EXPIRY_DAYS = 90
CACHE_EXPIRY_SECONDS = CACHE_EXPIRY_DAYS * 24 * 60 * 60  # Convert days to seconds

# LLM caching
DISABLE_LLM_CACHING = os.getenv('DISABLE_LLM_CACHING', 'False').lower() == 'true'
LLM_CACHE_EXPIRY = 7 * 24 * 60 * 60  # 7 days in seconds

# =============================================================================
# Wallet Configuration
# =============================================================================
WALLET_NAME = os.getenv('WALLET_NAME')
HOTKEY_NAME = os.getenv('HOTKEY_NAME')

MECHID = int(os.getenv('MECHID', '1'))

# =============================================================================
# Server Endpoints
# =============================================================================

# Bitcast server
BITCAST_SERVER_URL = os.getenv('BITCAST_SERVER_URL', 'http://44.227.253.127')
BITCAST_BRIEFS_ENDPOINT = f"{BITCAST_SERVER_URL}:8013/x-briefs"
POOLS_API_URL = os.getenv('POOLS_API_URL', f"{BITCAST_SERVER_URL}:8013/pools")

# Data publishing configuration
ENABLE_DATA_PUBLISH = os.getenv('ENABLE_DATA_PUBLISH', 'False').lower() == 'true'
DATA_CLIENT_URL = os.getenv('DATA_CLIENT_URL', 'http://44.254.20.95')
X_SOCIAL_MAP_ENDPOINT = f"{DATA_CLIENT_URL}:7999/api/v1/x-social-map"
X_ACCOUNT_CONNECTIONS_ENDPOINT = f"{DATA_CLIENT_URL}:7999/api/v1/x-account-connections"
TWEETS_SUBMIT_ENDPOINT = f"{DATA_CLIENT_URL}:7999/api/v1/brief-tweets"

# =============================================================================
# API Keys and Providers
# =============================================================================

# Twitter API Provider Configuration
TWITTER_API_PROVIDER = os.getenv('TWITTER_API_PROVIDER', 'rapidapi')  # Options: 'desearch' or 'rapidapi'

# Twitter API Keys
DESEARCH_API_KEY = os.getenv('DESEARCH_API_KEY')  # Required for Desearch.ai provider
# RapidAPI: Supports single key OR comma-separated list for load balancing
# Example: RAPID_API_KEY=key1,key2,key3 (distributes requests across all keys)
RAPID_API_KEY = os.getenv('RAPID_API_KEY')  # Optional: Required only if using RapidAPI provider

# Other API Keys
CHUTES_API_KEY = os.getenv('CHUTES_API_KEY')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'bitcast-X_vali_logs')

# =============================================================================
# Twitter API Configuration
# =============================================================================

# Fetching Strategy
SOCIAL_DISCOVERY_FETCH_DAYS = 30  # Days of tweet history for social network discovery
SOCIAL_DISCOVERY_LOOKBACK = 60  # Maximum age of cached tweets to use in analysis (in days, None = use all)
TWEET_SCORING_FETCH_DAYS = 1     # Days of tweet history for thorough scoring timeline pulls
MAX_TWEETS_PER_FETCH = 400       # Supports pagination to fetch up to 400 tweets via Desearch.ai

# Twitter API Configuration - PageRank Weights
PAGERANK_RETWEET_WEIGHT = 1.0
PAGERANK_MENTION_WEIGHT = 2.0
PAGERANK_QUOTE_WEIGHT = 3.0
BASELINE_TWEET_SCORE_FACTOR = 2
PAGERANK_ALPHA = 0.85

# =============================================================================
# Social Discovery Configuration
# =============================================================================

# Cache freshness for social discovery
SOCIAL_DISCOVERY_CACHE_HOURS = 48
CACHE_FRESHNESS_SECONDS = SOCIAL_DISCOVERY_CACHE_HOURS * 3600

# Concurrency (1 = sequential, 2+ = concurrent)
SOCIAL_DISCOVERY_MAX_WORKERS = 10

# =============================================================================
# Emissions and Rewards
# =============================================================================

# Twitter emissions
EMISSIONS_PERIOD = 7  # 7 days
REWARDS_DELAY_DAYS = 1  # Wait period before rewards start after brief closes
REWARD_SMOOTHING_EXPONENT = 0.65

# =============================================================================
# LLM and Validation Settings
# =============================================================================

# Content length limit for LLM evaluation
TWEET_MAX_LENGTH = 10000

# Validation cycle
VALIDATOR_WAIT = 60  # 60 seconds
SCORING_INTERVAL_MINUTES = 15
THOROUGH_SCORING_INTERVAL_MINUTES = 480  # 8 hours

# Tiered engagement fetching intervals (in hours)
# Reduces API calls by fetching engagements less frequently for older tweets
ENGAGEMENT_FETCH_INTERVAL_NEW = 1  # Hours: tweets < 1 hour old
ENGAGEMENT_FETCH_INTERVAL_RECENT = 4  # Hours: tweets 1-24 hours old
ENGAGEMENT_FETCH_INTERVAL_OLD = 24  # Hours: tweets > 24 hours old

# =============================================================================
# Subnet Treasury and No-Code Mining
# =============================================================================

# Subnet treasury
SUBNET_TREASURY_PERCENTAGE = 0
SUBNET_TREASURY_UID = int(os.getenv('SUBNET_TREASURY_UID', '106'))

# No-code mining
NOCODE_UID = int(os.getenv('NOCODE_UID', '114'))
SIMULATE_CONNECTIONS = os.getenv('SIMULATE_CONNECTIONS', 'False').lower() == 'true'

# Account connection scanning
CONNECTION_SEARCH_TAG = os.getenv('CONNECTION_SEARCH_TAG', '@bitcast')

# Validator mode: 'weight_copy' (default), 'standard', or 'discovery'
# - weight_copy: Fetches weights from reference validator
# - standard: Performs validation with downloaded social maps
# - discovery: Performs complete validation with social discovery
VALIDATOR_MODE = os.getenv('VALIDATOR_MODE', 'weight_copy').lower()
if VALIDATOR_MODE not in ['weight_copy', 'standard', 'discovery']:
    raise ValueError(f"Invalid VALIDATOR_MODE: {VALIDATOR_MODE}. Must be 'weight_copy', 'standard', or 'discovery'")

# Reference Validator API Configuration
# Reference validator = validator that provides authoritative data (weights, social maps, connections)
# Reference validator is typically running in 'discovery' mode but can run in any mode
REFERENCE_VALIDATOR_URL = os.getenv('REFERENCE_VALIDATOR_URL', 'http://44.241.197.212')
REFERENCE_VALIDATOR_ENDPOINT = f"{REFERENCE_VALIDATOR_URL}:8094"

# Log out all non-sensitive config variables
bt.logging.info(f"MECHID: {MECHID}")
bt.logging.info(f"BITCAST_BRIEFS_ENDPOINT: {BITCAST_BRIEFS_ENDPOINT}")
bt.logging.info(f"POOLS_API_URL: {POOLS_API_URL}")
bt.logging.info(f"ENABLE_DATA_PUBLISH: {ENABLE_DATA_PUBLISH}")
bt.logging.info(f"X_SOCIAL_MAP_ENDPOINT: {X_SOCIAL_MAP_ENDPOINT}")
bt.logging.info(f"X_ACCOUNT_CONNECTIONS_ENDPOINT: {X_ACCOUNT_CONNECTIONS_ENDPOINT}")
bt.logging.info(f"DISABLE_LLM_CACHING: {DISABLE_LLM_CACHING}")
bt.logging.info(f"TWITTER_API_PROVIDER: {TWITTER_API_PROVIDER}")
bt.logging.info(f"SOCIAL_DISCOVERY_FETCH_DAYS: {SOCIAL_DISCOVERY_FETCH_DAYS}")
bt.logging.info(f"TWEET_SCORING_FETCH_DAYS: {TWEET_SCORING_FETCH_DAYS}")
bt.logging.info(f"MAX_TWEETS_PER_FETCH: {MAX_TWEETS_PER_FETCH}")
bt.logging.info(f"EMISSIONS_PERIOD: {EMISSIONS_PERIOD}")
bt.logging.info(f"REWARDS_DELAY_DAYS: {REWARDS_DELAY_DAYS}")
bt.logging.info(f"REWARD_SMOOTHING_EXPONENT: {REWARD_SMOOTHING_EXPONENT}")
bt.logging.info(f"VALIDATOR_WAIT: {VALIDATOR_WAIT}")
bt.logging.info(f"SCORING_INTERVAL_MINUTES: {SCORING_INTERVAL_MINUTES}")
bt.logging.info(f"THOROUGH_SCORING_INTERVAL_MINUTES: {THOROUGH_SCORING_INTERVAL_MINUTES}")
bt.logging.info(f"ENGAGEMENT_FETCH_INTERVAL_NEW: {ENGAGEMENT_FETCH_INTERVAL_NEW}h (tweets < 1h old)")
bt.logging.info(f"ENGAGEMENT_FETCH_INTERVAL_RECENT: {ENGAGEMENT_FETCH_INTERVAL_RECENT}h (tweets 1-24h old)")
bt.logging.info(f"ENGAGEMENT_FETCH_INTERVAL_OLD: {ENGAGEMENT_FETCH_INTERVAL_OLD}h (tweets > 24h old)")
bt.logging.info(f"SUBNET_TREASURY_PERCENTAGE: {SUBNET_TREASURY_PERCENTAGE}")
bt.logging.info(f"SUBNET_TREASURY_UID: {SUBNET_TREASURY_UID}")
bt.logging.info(f"NOCODE_UID: {NOCODE_UID}")
bt.logging.info(f"SIMULATE_CONNECTIONS: {SIMULATE_CONNECTIONS}")
bt.logging.info(f"VALIDATOR_MODE: {VALIDATOR_MODE}")
bt.logging.info(f"REFERENCE_VALIDATOR_ENDPOINT: {REFERENCE_VALIDATOR_ENDPOINT}")
bt.logging.info(f"CONNECTION_SEARCH_TAG: {CONNECTION_SEARCH_TAG}")
