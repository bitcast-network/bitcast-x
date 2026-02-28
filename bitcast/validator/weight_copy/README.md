# Weight Copy Mode

Fetch weights from a reference validator instead of running validation logic.

## Overview

Weight copy mode is the **recommended default** for most validators. Instead of running social discovery, tweet scoring, and reward calculations, weight copy validators simply fetch pre-calculated weights from a reference validator's API. No validation work is performed, so no social maps or API keys are needed.

**Benefits:**
- ✅ No API keys required
- ✅ No social maps needed (doesn't perform validation)
- ✅ Simpler setup and maintenance
- ✅ Lower resource usage (1 CPU, 2GB RAM)
- ✅ Automatic weight synchronization
- ✅ Perfect for backup validators

## How It Works

1. Reference validator runs full validation and exposes weights via API
2. Weight copy validator fetches weights every ~1 minute from API endpoint
3. Weight copy validator stores weights locally and sets them on-chain

## Configuration

Set in `bitcast/validator/.env`:
```bash
VALIDATOR_MODE=weight_copy
REFERENCE_VALIDATOR_URL=http://44.241.197.212  # Reference validator endpoint
```

The validator automatically detects the mode and runs the appropriate logic.

## Architecture

```
weight_copy/
├── wc_client.py   # API client for fetching weights
└── wc_forward.py  # Weight copy mode forward pass logic
```

## Verification

### Quick Health Check
```bash
# Check logs for weight copy activity
pm2 logs bitcast_x_validator | grep "WEIGHT COPY"
```

You should see:
```
🔄 Running in WEIGHT COPY mode - fetching weights from reference validator
✅ Updated scores from reference validator
```

### State File Check
```bash
# Find your state file
ls -lh ~/.bittensor/miners/*/*/netuid93/validator/state_mech_*.npz

# Check it's being updated (< 5 minutes ago)
stat ~/.bittensor/miners/<WALLET_NAME>/<HOTKEY_NAME>/netuid93/validator/state_mech_1.npz
```

### Compare with Reference Validator
```bash
# Get reference validator weights
curl http://44.241.197.212:8094/weights | jq '.weights[:5]'

# Get your weight copy weights
python -c "import numpy as np; s=np.load('~/.bittensor/miners/<WALLET>/<HOTKEY>/netuid93/validator/state_mech_1.npz', allow_pickle=True); print(s['scores'][:5])"
```

Values should match closely (minor differences < 0.001 are normal due to timing).

## Troubleshooting

### State file not found or not updating
1. Check weight copy validator is running: `pm2 list`
2. Check logs for errors: `pm2 logs bitcast_x_validator`
3. Verify reference validator endpoint is accessible: `curl http://44.241.197.212:8094/health`
4. Wait 2-3 minutes for initial sync

### Weight mismatch
- Small differences (< 0.001) are normal due to timing
- Large differences may indicate:
  - Weight copy validator hasn't synced recently
  - Reference validator updated between fetches
  - Network connectivity issues

### Connection errors
- Check `REFERENCE_VALIDATOR_URL` in `.env`
- Verify network connectivity to reference validator
- Check reference validator is running and API is exposed

## Success Indicators

✅ State file exists and < 5 minutes old  
✅ Logs show "Updated scores from reference validator"  
✅ Weights match reference validator  
✅ PM2 process is online  

## Switching Modes

The validator supports three modes:

### Standard Mode (Medium Resources)
Performs full validation (account scanning, tweet scoring, filtering, rewards) using social maps downloaded from reference validator. Downloads maps at startup if missing, then refreshes them periodically (every 12 hours).

1. Set `VALIDATOR_MODE=standard` in `.env`
2. Add required API keys:
   - `DESEARCH_API_KEY` - Desearch.ai Twitter API
   - `CHUTES_API_KEY` - LLM evaluation
   - `WANDB_API_KEY` - Monitoring/logging
3. Restart: `pm2 restart bitcast_x_validator`

**Resources**: 2 CPU, 4GB RAM  
**Startup**: Downloads social maps from reference validator (quick start)  
**Operation**: Performs full validation, computes own weights

### Discovery Mode (High Resources)
Performs full validation including social discovery and mapping. Downloads social maps at startup for quick start, then generates fresh maps bi-weekly via social discovery process.

1. Set `VALIDATOR_MODE=discovery` in `.env`
2. Add required API keys:
   - `DESEARCH_API_KEY` - Desearch.ai Twitter API
   - `CHUTES_API_KEY` - LLM evaluation
   - `WANDB_API_KEY` - Monitoring/logging
3. Restart: `pm2 restart bitcast_x_validator`

**Resources**: 2 CPU, 8GB RAM, higher API costs  
**Startup**: Downloads social maps from reference validator (quick start)  
**Operation**: Generates fresh social maps bi-weekly, performs full validation

