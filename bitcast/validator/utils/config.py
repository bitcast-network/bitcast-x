import os
import sys
from dotenv import load_dotenv
from pathlib import Path
import bittensor as bt

env_path = Path(__file__).parents[1] / '.env'
load_dotenv(dotenv_path=env_path)

__version__ = "1.5.1"

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
BITCAST_API_URL = os.getenv('BITCAST_API_URL', 'https://bitcast-api.bitcast.network')
BITCAST_BRIEFS_ENDPOINT = os.getenv('BITCAST_BRIEFS_ENDPOINT', f"{BITCAST_API_URL}/api/v2/validator/x-briefs")
POOLS_API_URL = os.getenv('POOLS_API_URL', f"{BITCAST_API_URL}/api/v2/validator/pools")

# Data publishing configuration
ENABLE_DATA_PUBLISH = os.getenv('ENABLE_DATA_PUBLISH', 'False').lower() == 'true'
DATA_CLIENT_URL = os.getenv('DATA_CLIENT_URL', 'https://ingestion.bitcast.network:443')
X_SOCIAL_MAP_ENDPOINT = f"{DATA_CLIENT_URL}/api/v1/x-social-map"
X_ACCOUNT_CONNECTIONS_ENDPOINT = f"{DATA_CLIENT_URL}/api/v1/x-account-connections"
TWEETS_SUBMIT_ENDPOINT = f"{DATA_CLIENT_URL}/api/v1/brief-tweets"
REFERRAL_BONUSES_ENDPOINT = f"{DATA_CLIENT_URL}/api/v1/referral-bonuses"

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

# LLM Provider
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'chutes').lower()  # Options: 'chutes' or 'openrouter'

# Other API Keys
CHUTES_API_KEY = os.getenv('CHUTES_API_KEY')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
WANDB_API_KEY = os.getenv('WANDB_API_KEY')
WANDB_PROJECT = os.getenv('WANDB_PROJECT', 'bitcast-X_vali_logs')

# =============================================================================
# Twitter API Configuration
# =============================================================================

# Fetching Strategy
SOCIAL_DISCOVERY_FETCH_DAYS = 30  # Days of tweet history for social network discovery
SOCIAL_DISCOVERY_LOOKBACK = 60  # Maximum age of cached tweets to use in analysis (in days, None = use all)
MAX_TWEETS_PER_FETCH = 200       # Max tweets per user per fetch cycle (cursor-based pagination)

# Twitter API Configuration - PageRank Weights
PAGERANK_RETWEET_WEIGHT = 1.0
PAGERANK_MENTION_WEIGHT = 2.0
PAGERANK_QUOTE_WEIGHT = 3.0
BASELINE_TWEET_SCORE_FACTOR = 2
STALE_INFLUENCE_DECAY = 0.5  # Influence multiplier for accounts dropped from social map mid-brief
PAGERANK_ALPHA = 0.85

# =============================================================================
# Social Discovery Configuration
# =============================================================================

# Cache freshness for social discovery
SOCIAL_DISCOVERY_CACHE_HOURS = 36
CACHE_FRESHNESS_SECONDS = SOCIAL_DISCOVERY_CACHE_HOURS * 3600

# Concurrency (1 = sequential, 2+ = concurrent)
SOCIAL_DISCOVERY_MAX_WORKERS = 10

# -----------------------------------------------------------------------------
# Social Discovery v2: Relevance gradient (continuous on-topic ratio)
# -----------------------------------------------------------------------------
# Replaces the legacy 2-tier keyword-count relevance gate with a beta-smoothed
# on-topic ratio. Feeds the PageRank personalization vector and an inclusion gate.
# Flag-gated so behaviour is identical to legacy until explicitly enabled.
RELEVANCE_GRADIENT_ENABLED = os.getenv('RELEVANCE_GRADIENT_ENABLED', 'False').lower() == 'true'
# Beta prior: smoothed_ratio = (relevant + a) / (total + a + b) with
# a = mean*strength, b = (1-mean)*strength. Low mean => "off-topic until proven";
# strength is the pseudo-count at which an account's own data equals the prior.
RELEVANCE_PRIOR_MEAN = float(os.getenv('RELEVANCE_PRIOR_MEAN', '0.02'))
RELEVANCE_PRIOR_STRENGTH = float(os.getenv('RELEVANCE_PRIOR_STRENGTH', '15'))
# Default per-pool inclusion floor on the smoothed ratio (pools can override).
# This is the OUTER/extended gate (who makes the final map).
RELEVANCE_MIN_RATIO_DEFAULT = float(os.getenv('RELEVANCE_MIN_RATIO_DEFAULT', '0.02'))
# Core crawl gate: stricter than the outer gate. Core only seeds the Stage-2
# crawl frontier, so we anchor it on high-confidence on-topic accounts.
RELEVANCE_CORE_MIN_RATIO_DEFAULT = float(os.getenv('RELEVANCE_CORE_MIN_RATIO_DEFAULT', '0.05'))
# Absolute floor on relevant-tweet count (guards against small-sample gaming).
MIN_RELEVANT_TWEETS = int(os.getenv('MIN_RELEVANT_TWEETS', '1'))

# -----------------------------------------------------------------------------
# Social Discovery v2: AI out-link dampening (its-ai.org)
# -----------------------------------------------------------------------------
# Dampens an account's outgoing PageRank influence in proportion to how
# AI-generated its content is, by leaking transition probability to a sink node.
# Breaks "circle-jerk" mutual amplification among low-quality AI accounts.
AI_DAMPENING_ENABLED = os.getenv('AI_DAMPENING_ENABLED', 'False').lower() == 'true'
ITS_AI_API_URL = os.getenv('ITS_AI_API_URL', 'https://api.its-ai.org/api/v2/text')
ITS_AI_BATCH_API_URL = os.getenv('ITS_AI_BATCH_API_URL', 'https://api.its-ai.org/api/v2/batch')
ITS_AI_API_KEY = os.getenv('ITS_AI_API_KEY')
ITS_AI_TIMEOUT = int(os.getenv('ITS_AI_TIMEOUT', '300'))  # synchronous, blocks up to 5 min
# Retries for transient its-ai failures (network errors, 429, 5xx). A failed
# batch request fails open for up to ITS_AI_BATCH_SIZE tweets at once, so retrying
# matters: one blip would otherwise wipe out dampening for many accounts.
ITS_AI_MAX_RETRIES = int(os.getenv('ITS_AI_MAX_RETRIES', '3'))      # additional attempts after the first
ITS_AI_RETRY_BACKOFF = float(os.getenv('ITS_AI_RETRY_BACKOFF', '2'))  # base seconds, doubled each retry
AI_SAMPLE_SIZE = int(os.getenv('AI_SAMPLE_SIZE', '4'))     # tweets sampled per account
AI_MIN_TWEET_CHARS = int(os.getenv('AI_MIN_TWEET_CHARS', '200'))  # its-ai requires >=200 chars
# Texts per /v2/batch request. Must not exceed the its-ai plan's batch limit
# (Free 3 / Plus 10 / Premium 25 / Pro 50 / Enterprise 250) or the request
# fails with 404 validation:batch_limit and that batch fails open (no dampening).
ITS_AI_BATCH_SIZE = int(os.getenv('ITS_AI_BATCH_SIZE', '250'))
AI_DETECTION_CONCURRENCY = int(os.getenv('AI_DETECTION_CONCURRENCY', '4'))  # concurrent batch requests
AI_SCORE_BUCKET = float(os.getenv('AI_SCORE_BUCKET', '0.2'))  # bucketise to absorb API jitter
AI_SCORE_CAP = float(os.getenv('AI_SCORE_CAP', '0.95'))      # cap so sink weight stays finite
AI_SCORE_TTL_SECONDS = int(os.getenv('AI_SCORE_TTL_DAYS', '14')) * 24 * 60 * 60  # per-account cache
# Cap the number of accounts AI-checked per run to bound cost: only the top-N by
# interaction weight (most influential) are scored; the rest are assumed human
# (no dampening). 0 = unlimited (check every account).
AI_MAX_ACCOUNTS_CHECKED = int(os.getenv('AI_MAX_ACCOUNTS_CHECKED', '0'))

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
VALIDATOR_WAIT = 10  # 10 seconds per step
SCORING_INTERVAL_STEPS = 120  # 120 steps × 10s = 20 minutes
THOROUGH_SCORING_INTERVAL_STEPS = 2880  # 2880 steps × 10s = 8 hours
SOCIAL_MAP_DOWNLOAD_INTERVAL = 4320  # 4320 steps × 10s = 12 hours

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

# Account connection scanning -- designated tweets that miners reply to with connection tags
CONNECTION_TWEET_IDS = ['2031383975088836738']

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
bt.logging.info(f"LLM_PROVIDER: {LLM_PROVIDER}")
bt.logging.info(f"TWITTER_API_PROVIDER: {TWITTER_API_PROVIDER}")
bt.logging.info(f"SOCIAL_DISCOVERY_FETCH_DAYS: {SOCIAL_DISCOVERY_FETCH_DAYS}")
bt.logging.info(f"MAX_TWEETS_PER_FETCH: {MAX_TWEETS_PER_FETCH}")
bt.logging.info(f"EMISSIONS_PERIOD: {EMISSIONS_PERIOD}")
bt.logging.info(f"REWARDS_DELAY_DAYS: {REWARDS_DELAY_DAYS}")
bt.logging.info(f"REWARD_SMOOTHING_EXPONENT: {REWARD_SMOOTHING_EXPONENT}")
bt.logging.info(f"VALIDATOR_WAIT: {VALIDATOR_WAIT}s (step interval)")
bt.logging.info(f"SCORING_INTERVAL: {SCORING_INTERVAL_STEPS} steps ({SCORING_INTERVAL_STEPS * VALIDATOR_WAIT // 60} min)")
bt.logging.info(f"THOROUGH_SCORING_INTERVAL: {THOROUGH_SCORING_INTERVAL_STEPS} steps ({THOROUGH_SCORING_INTERVAL_STEPS * VALIDATOR_WAIT // 3600} hr)")
bt.logging.info(f"SOCIAL_MAP_DOWNLOAD_INTERVAL: {SOCIAL_MAP_DOWNLOAD_INTERVAL} steps ({SOCIAL_MAP_DOWNLOAD_INTERVAL * VALIDATOR_WAIT // 3600} hr)")
bt.logging.info(f"ENGAGEMENT_FETCH_INTERVAL_NEW: {ENGAGEMENT_FETCH_INTERVAL_NEW}h (tweets < 1h old)")
bt.logging.info(f"ENGAGEMENT_FETCH_INTERVAL_RECENT: {ENGAGEMENT_FETCH_INTERVAL_RECENT}h (tweets 1-24h old)")
bt.logging.info(f"ENGAGEMENT_FETCH_INTERVAL_OLD: {ENGAGEMENT_FETCH_INTERVAL_OLD}h (tweets > 24h old)")
bt.logging.info(f"SUBNET_TREASURY_PERCENTAGE: {SUBNET_TREASURY_PERCENTAGE}")
bt.logging.info(f"SUBNET_TREASURY_UID: {SUBNET_TREASURY_UID}")
bt.logging.info(f"NOCODE_UID: {NOCODE_UID}")
bt.logging.info(f"SIMULATE_CONNECTIONS: {SIMULATE_CONNECTIONS}")
bt.logging.info(f"VALIDATOR_MODE: {VALIDATOR_MODE}")
bt.logging.info(f"RELEVANCE_GRADIENT_ENABLED: {RELEVANCE_GRADIENT_ENABLED}")
bt.logging.info(f"AI_DAMPENING_ENABLED: {AI_DAMPENING_ENABLED}")
bt.logging.info(f"REFERENCE_VALIDATOR_ENDPOINT: {REFERENCE_VALIDATOR_ENDPOINT}")
bt.logging.info(f"CONNECTION_TWEET_IDS: {CONNECTION_TWEET_IDS}")
