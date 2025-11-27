<p align="center">
  <a href="https://www.bitcast.network/">
    <img src="assets/lockup_gradient.svg" alt="Bitcast Logo" width="800" />
  </a>
</p>

# Bitcast X — Decentralized Social Mining on X.com

Bitcast X is a Bittensor subnet that incentivizes X content creators to connect brands to audiences. Creators publish tweets to satisfy defined briefs and earn rewards based on engagement from influential accounts within curated social networks.

---

## ⚙️ High-Level Architecture

- **Miners**: Content creators on X who connect their accounts and earn rewards for tweet performance.  
- **Validators**: Discover social networks, track account connections, score tweet engagement, and distribute on-chain rewards.  
- **Brands**: Define and publish content briefs for X campaigns.  
- **Briefs Server**: Hosts the active campaign briefs.  
- **Bittensor Network**: Manages on-chain compensation, rewarding Validators and Miners with the [Bitcast alpha token](https://www.coingecko.com/en/coins/bitcast).

---

## 🚀 Getting Started

### For Miners

**No code required!** There are two ways to mine on Bitcast X:

> **Eligibility:** Eligibility is determined per campaign brief. Some briefs may target all accounts in the social network, while others may limit eligibility to top-ranked accounts. Visit [x.bitcast.network](https://x.bitcast.network/) to check your eligibility for active campaigns.

#### Option 1: Managed Mining

Visit [x.bitcast.network](https://x.bitcast.network/) for the simplest setup:
1. Create a Bittensor wallet using [Crucible](https://cruciblelabs.com/) or [Talisman](https://talisman.xyz/)
2. Go to [x.bitcast.network](https://x.bitcast.network/) and paste your wallet address
3. Click "Generate Tag" to receive your unique connection tag
4. Post a tweet containing your tag to link your X account
5. Complete briefs(https://x.bitcast.network/) to start earning!
6. Rewards are distributed daily through a managed UID (UID 68)

*Note: Emissions will incur a 5% fee*

#### Option 2: Self-Managed Mining

Register your own UID and receive rewards directly:
1. Register a UID on subnet 93 using `btcli subnet register --netuid 93`
2. Post a tweet containing `bitcast-hk:{your_substrate_hotkey}`
   - Example: `bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq`
3. Complete briefs(https://x.bitcast.network/) to start earning!
4. Rewards go directly to your UID on-chain

Tweets are automatically discovered and scored based on engagement from influential accounts in the network.

### For Validators

Validators maintain the integrity of the network by:
- Discovering and mapping social influence networks on X using PageRank  
- Tracking account connections via on-chain tags  
- Scoring tweet engagement from top influencers  
- Evaluating content against campaign briefs using LLM  
- Distributing on-chain rewards to miners

See below for detailed validator setup instructions.

---

## 📊 Scoring & Rewards System

Bitcast X employs a sophisticated, multi-layered scoring mechanism to fairly distribute emissions and incentivize high-quality participation.

### 1. Social Network Discovery

- **PageRank Algorithm**: Analyzes X interaction networks to identify influential accounts
- **Pool Management**: Curated social networks (pools) like "tao", "ai_crypto", etc.
- **Recalibration Schedule**: Network is recalibrated every 2nd Sunday to update influence rankings

### 2. Influence Score

- **Score Calculation**: Influence scores are derived from PageRank analysis of X interaction networks
- **Interaction Weights**: Different engagement types contribute differently to influence:
  - Retweets: 1.0x
  - Mentions: 2.0x
  - Quote tweets: 3.0x
- **Comprehensive Rankings**: All discovered accounts are ranked by their PageRank influence scores

### 3. Account Connection

- **Connection Tags**: Miners post tweets with special tags to link accounts:
  - `bitcast-hk:{substrate_hotkey}` - self-managed mining
  - `bitcast-xabcd` - bitcast managed mining
- **Verification**: Validators scan pool member tweets to discover and verify connections
- **Tag Replacement**: New connection tags automatically replace previous ones for the same account

### 4. Tweet Scoring

- **Engagement Analysis**: Tracks retweets and quote tweets from the most influential accounts (configurable per pool, typically 300+) over the past 30 days
- **Weighted Scoring**:
  - `score = (author_influence × 2) + Σ(influencer_score × engagement_weight)`
  - Retweet contribution: `influence_score × 1.0`
  - Quote tweet contribution: `influence_score × 3.0`
- **Quality Focus**: Only engagement from verified influential accounts counts
- **Interaction Limits**: Maximum of 1 connection per direction between miners (prevents gaming through repeated interactions)

### 5. Brief Evaluation

- **LLM Content Matching**: Each scored tweet is evaluated against brief requirements for topic, format, and brand alignment
- **Tag Requirements**: Some briefs require tweets to contain specific tags
- **Quote Tweet Requirements**: Some briefs require quote tweets of specific posts
- **Quality Filter**: Tweets portraying sponsors negatively will fail evaluation

### 6. Reward Distribution

- **Budget Allocation**: Each brief has a daily budget distributed over 7-day emissions period
- **Delay Period**: 2-day delay after brief closes before rewards begin (for engagement verification)
- **Proportional Distribution**: Rewards distributed based on relative tweet scores
- **Treasury Allocation**: Unclaimed emissions go to subnet treasury

---

## 💻 Validator Setup

### System Requirements

- **Operating System**: Linux
- **CPU**: 1 cores  
- **RAM**: 2 GB

### API Setup Requirements

**Weight Copy Mode (Recommended - Default)**
- No API keys required!
- Fetches weights from primary validator
- Much simpler setup

**Full Validation Mode (Optional)**
1. **RapidAPI Key** - [The Old Bird V2](https://rapidapi.com/datahungrybeast/api/twitter-v24) - Mega subscription ($200/month)
2. **Chutes API Key** - Get from [Chutes.ai](https://chutes.ai/) - Plus subscription ($10/month)
3. **Weights & Biases API Key** - Get from [wandb.ai](https://wandb.ai/)

---

## 🚀 Installation & Setup

### 1. Clone Repository
```bash
git clone https://github.com/bitcast-network/bitcast-x.git
cd bitcast-x
```

### 2. Setup Environment
```bash
chmod +x scripts/setup_env.sh
./scripts/setup_env.sh
```

This creates a Python virtual environment at `../venv_bitcast_x/` and installs dependencies.

### 3. Configure Environment

Copy the example environment file and edit it with your configuration:
```bash
cp bitcast/validator/.env.example bitcast/validator/.env
```

Edit `bitcast/validator/.env` and set your wallet information:
- `WALLET_NAME`: Your Bittensor wallet name (coldkey)
- `HOTKEY_NAME`: Your validator hotkey name
- `WC_MODE`: Set to `true` for weight copy mode (recommended, default)

For **full validation mode only** (`WC_MODE=false`), also set:
- `RAPID_API_KEY`: Your RapidAPI key
- `CHUTES_API_KEY`: Your Chutes API key
- `WANDB_API_KEY`: Your Weights & Biases API key

### 4. Register on Bittensor Network

Activate the virtual environment:
```bash
source ../venv_bitcast_x/bin/activate
```

Register your validator:
```bash
btcli subnet register \
  --netuid 93 \
  --wallet.name <WALLET_NAME> \
  --wallet.hotkey <HOTKEY_NAME>
```

---

## 🚀 Running the Validator

### Start Validator Service
```bash
./scripts/run_validator.sh
```

The validator automatically detects your configuration and runs in the appropriate mode (WC or full validation).

### Process Management with PM2

The validator runs under PM2 for process management:

---

## 📁 Project Structure

```
bitcast-x/
├── bitcast/validator/
│   ├── social_discovery/     # PageRank-based social network discovery
│   ├── account_connection/   # Connection tag scanning and tracking
│   ├── tweet_scoring/        # Engagement-based tweet scoring
│   ├── tweet_filtering/      # LLM-based brief evaluation
│   ├── reward_engine/        # Reward calculation and distribution
│   ├── weight_copy/          # Weight copy mode implementation
│   ├── api/                  # Weights API for WC validators
│   ├── clients/              # External API clients (Twitter, LLM)
│   └── utils/                # Shared utilities and configuration
├── scripts/                  # Setup and run scripts
└── neurons/                  # Bittensor neuron entry points
```

---

## ℹ️ General Notes

- **Auto-updates**: Enabled by default for security and feature updates
- **Subnet ID**: 93 (Bittensor mainnet)
- **Account Connection Scan**: Every 1 hour
- **Reward Distribution**: Every 1 hour
- **Social Discovery**: Bi-weekly (every other Sunday)

---

## 🤝 Contact & Support

For assistance or questions, join our Discord support channel:

[Bitcast Support on Bittensor Discord](https://discord.com/channels/799672011265015819/1362489640841380045)

---

## 🔗 Links

- **Website**: [bitcast.network](https://www.bitcast.network/)
- **Mining Platform**: [x.bitcast.network](https://x.bitcast.network/)
- **Token**: [Bitcast on CoinGecko](https://www.coingecko.com/en/coins/bitcast)
- **Validator Logs**: [wandb](https://wandb.ai/bitcast_network/bitcast-X_vali_logs)