# Weight Copy (WC) Mode

Fetch weights from a primary validator instead of running full validation logic.

## Overview

Weight copy mode is the **recommended default** for most validators. Instead of running social discovery, tweet scoring, and reward calculations, WC validators simply fetch pre-calculated weights from a primary validator's API.

**Benefits:**
- ✅ No API keys required
- ✅ Simpler setup and maintenance
- ✅ Lower resource usage (1 CPU, 2GB RAM)
- ✅ Automatic weight synchronization
- ✅ Perfect for backup validators

## How It Works

1. Primary validator runs full validation and exposes weights via API
2. WC validator fetches weights every ~1 minute from API endpoint
3. WC validator stores weights locally and sets them on-chain

## Configuration

Set in `bitcast/validator/.env`:
```bash
WC_MODE=true
WC_SERVER_URL=http://44.241.197.212  # Primary validator endpoint
```

The validator automatically detects WC mode and runs the appropriate logic.

## Architecture

```
weight_copy/
├── wc_client.py   # API client for fetching weights
└── wc_forward.py  # WC mode forward pass logic
```

## Verification

### Quick Health Check
```bash
# Check logs for WC activity
pm2 logs bitcast_x_validator | grep "WC Mode"
```

You should see:
```
🔄 WC Mode: Fetching weights from primary validator
✅ Successfully fetched weights from primary validator
✅ Updated scores from primary validator
```

### State File Check
```bash
# Find your state file
ls -lh ~/.bittensor/miners/*/*/netuid93/validator/state_mech_*.npz

# Check it's being updated (< 5 minutes ago)
stat ~/.bittensor/miners/<WALLET_NAME>/<HOTKEY_NAME>/netuid93/validator/state_mech_1.npz
```

### Compare with Primary
```bash
# Get primary weights
curl http://44.241.197.212:8094/weights | jq '.weights[:5]'

# Get your WC weights
python -c "import numpy as np; s=np.load('~/.bittensor/miners/<WALLET>/<HOTKEY>/netuid93/validator/state_mech_1.npz', allow_pickle=True); print(s['scores'][:5])"
```

Values should match closely (minor differences < 0.001 are normal due to timing).

## Troubleshooting

### State file not found or not updating
1. Check WC validator is running: `pm2 list`
2. Check logs for errors: `pm2 logs bitcast_x_validator`
3. Verify primary endpoint is accessible: `curl http://44.241.197.212:8094/health`
4. Wait 2-3 minutes for initial sync

### Weight mismatch
- Small differences (< 0.001) are normal due to timing
- Large differences may indicate:
  - WC validator hasn't synced recently
  - Primary updated between fetches
  - Network connectivity issues

### Connection errors
- Check `WC_SERVER_URL` in `.env`
- Verify network connectivity to primary
- Check primary validator is running and API is exposed

## Success Indicators

✅ State file exists and < 5 minutes old  
✅ Logs show "Successfully fetched weights"  
✅ Weights match primary validator  
✅ PM2 process is online  

## Switching to Full Validation

If you want to run full validation logic:

1. Set `WC_MODE=false` in `.env`
2. Add required API keys:
   - `RAPID_API_KEY` - Twitter API
   - `CHUTES_API_KEY` - LLM evaluation
   - `WANDB_API_KEY` - Logging
3. Restart: `pm2 restart bitcast_x_validator`

Note: Full validation requires more resources (2 CPU, 8GB RAM) and API costs.

