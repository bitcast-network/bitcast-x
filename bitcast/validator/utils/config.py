import os
import sys
from dotenv import load_dotenv
from pathlib import Path
import bittensor as bt

env_path = Path(__file__).parents[1] / '.env'
load_dotenv(dotenv_path=env_path)

# Cache Configuration
CACHE_ROOT = Path(__file__).resolve().parents[2] / "cache"
CACHE_DIRS = {
    "briefs": os.path.join(CACHE_ROOT, "briefs"),
    "twitter": os.path.join(CACHE_ROOT, "twitter"),
    "llm": os.path.join(CACHE_ROOT, "llm")
}

__version__ = "1.2.4"

# Wallet Configuration
WALLET_NAME = os.getenv('WALLET_NAME')
HOTKEY_NAME = os.getenv('HOTKEY_NAME')

MECHID = int(os.getenv('MECHID', '1'))

# Bitcast server
BITCAST_SERVER_URL = os.getenv('BITCAST_SERVER_URL', 'http://44.227.253.127')
BITCAST_BRIEFS_ENDPOINT = f"{BITCAST_SERVER_URL}:8013/x-briefs"

# Data publishing configuration
ENABLE_DATA_PUBLISH = os.getenv('ENABLE_DATA_PUBLISH', 'False').lower() == 'true'
DATA_CLIENT_URL = os.getenv('DATA_CLIENT_URL', 'http://44.254.20.95')
X_SOCIAL_MAP_ENDPOINT = f"{DATA_CLIENT_URL}:7999/api/v1/x-social-map"
X_ACCOUNT_CONNECTIONS_ENDPOINT = f"{DATA_CLIENT_URL}:7999/api/v1/x-account-connections"
TWEETS_SUBMIT_ENDPOINT = f"{DATA_CLIENT_URL}:7999/api/v1/brief-tweets"

RAPID_API_KEY = os.getenv('RAPID_API_KEY')
CHUTES_API_KEY = os.getenv('CHUTES_API_KEY')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'bitcast-X_vali_logs')

# Twitter API Configuration - Fetching Strategy
INITIAL_FETCH_DAYS = 30
INCREMENTAL_FETCH_DAYS = 4
MAX_TWEETS_PER_FETCH = 200

# Twitter API Configuration - PageRank Weights
PAGERANK_RETWEET_WEIGHT = 1.0
PAGERANK_MENTION_WEIGHT = 2.0
PAGERANK_QUOTE_WEIGHT = 3.0
BASELINE_TWEET_SCORE_FACTOR = 2
PAGERANK_ALPHA = 0.85

# Cache Management
CACHE_EXPIRY_DAYS = 90
TWITTER_CACHE_EXPIRY = CACHE_EXPIRY_DAYS * 24 * 60 * 60  # Convert days to seconds
TWITTER_CACHE_FRESHNESS = int(os.getenv('TWITTER_CACHE_FRESHNESS', str(6 * 60 * 60)))  # Default 6 hours in seconds

# Social Discovery Concurrency (1 = sequential, 2+ = concurrent)
SOCIAL_DISCOVERY_MAX_WORKERS = 10

# Twitter emissions
EMISSIONS_PERIOD = 7  # 7 days
REWARDS_DELAY_DAYS = 1  # Wait period before rewards start after brief closes
REWARD_SMOOTHING_EXPONENT = 0.65

# LLM caching
DISABLE_LLM_CACHING = os.getenv('DISABLE_LLM_CACHING', 'False').lower() == 'true'
LLM_CACHE_EXPIRY = 7 * 24 * 60 * 60  # 7 days in seconds

# Content length limit for LLM evaluation
TWEET_MAX_LENGTH = 10000

# Validation cycle
VALIDATOR_WAIT = 60  # 60 seconds
ACCOUNT_CONNECTION_INTERVAL_HOURS = 1
REWARDS_INTERVAL_HOURS = 1

# Subnet treasury
SUBNET_TREASURY_PERCENTAGE = 1.0
SUBNET_TREASURY_UID = int(os.getenv('SUBNET_TREASURY_UID', '106'))

# No-code mining
NOCODE_UID = int(os.getenv('NOCODE_UID', '114'))
SIMULATE_CONNECTIONS = os.getenv('SIMULATE_CONNECTIONS', 'False').lower() == 'true'

# Reference Validator API Configuration
WC_MODE = os.getenv('WC_MODE', 'true').lower() == 'true'
REFERENCE_VALIDATOR_URL = os.getenv('REFERENCE_VALIDATOR_URL', 'http://44.241.197.212')
REFERENCE_VALIDATOR_ENDPOINT = f"{REFERENCE_VALIDATOR_URL}:8094"

# Log out all non-sensitive config variables
bt.logging.info(f"MECHID: {MECHID}")
bt.logging.info(f"BITCAST_BRIEFS_ENDPOINT: {BITCAST_BRIEFS_ENDPOINT}")
bt.logging.info(f"ENABLE_DATA_PUBLISH: {ENABLE_DATA_PUBLISH}")
bt.logging.info(f"X_SOCIAL_MAP_ENDPOINT: {X_SOCIAL_MAP_ENDPOINT}")
bt.logging.info(f"X_ACCOUNT_CONNECTIONS_ENDPOINT: {X_ACCOUNT_CONNECTIONS_ENDPOINT}")
bt.logging.info(f"DISABLE_LLM_CACHING: {DISABLE_LLM_CACHING}")
bt.logging.info(f"INITIAL_FETCH_DAYS: {INITIAL_FETCH_DAYS}")
bt.logging.info(f"INCREMENTAL_FETCH_DAYS: {INCREMENTAL_FETCH_DAYS}")
bt.logging.info(f"MAX_TWEETS_PER_FETCH: {MAX_TWEETS_PER_FETCH}")
bt.logging.info(f"TWITTER_CACHE_FRESHNESS: {TWITTER_CACHE_FRESHNESS}s ({TWITTER_CACHE_FRESHNESS/3600:.1f} hours)")
bt.logging.info(f"EMISSIONS_PERIOD: {EMISSIONS_PERIOD}")
bt.logging.info(f"REWARDS_DELAY_DAYS: {REWARDS_DELAY_DAYS}")
bt.logging.info(f"REWARD_SMOOTHING_EXPONENT: {REWARD_SMOOTHING_EXPONENT}")
bt.logging.info(f"VALIDATOR_WAIT: {VALIDATOR_WAIT}")
bt.logging.info(f"ACCOUNT_CONNECTION_INTERVAL_HOURS: {ACCOUNT_CONNECTION_INTERVAL_HOURS}")
bt.logging.info(f"REWARDS_INTERVAL_HOURS: {REWARDS_INTERVAL_HOURS}")
bt.logging.info(f"SUBNET_TREASURY_PERCENTAGE: {SUBNET_TREASURY_PERCENTAGE}")
bt.logging.info(f"SUBNET_TREASURY_UID: {SUBNET_TREASURY_UID}")
bt.logging.info(f"NOCODE_UID: {NOCODE_UID}")
bt.logging.info(f"SIMULATE_CONNECTIONS: {SIMULATE_CONNECTIONS}")
bt.logging.info(f"WC_MODE: {WC_MODE}")
bt.logging.info(f"REFERENCE_VALIDATOR_ENDPOINT: {REFERENCE_VALIDATOR_ENDPOINT}")