# Validator Weights API

Simple read-only API for exposing validator weights with rate limiting.

## Features

- **Read-only**: No state modification possible
- **Rate Limited**: Protection against abuse
- **Simple**: Minimal dependencies, clean code
- **Fast**: Direct disk reads, no complex processing

## Endpoints

### `GET /health`
Health check endpoint.
- **Rate Limit**: 60 requests/minute per IP
- **Response**: `{"status": "healthy"}`

### `GET /weights`
Get all validator weights.
- **Rate Limit**: 10 requests/minute per IP
- **Response**:
```json
{
  "step": 1234,
  "total_miners": 256,
  "weights": [
    {
      "uid": 0,
      "hotkey": "5F3sa...",
      "raw_weight": 0.123
    }
  ],
  "normalized_weights": [0.004, 0.003, ...]
}
```

### `GET /weights/{uid}`
Get weight for a specific UID.
- **Rate Limit**: 20 requests/minute per IP
- **Response**:
```json
{
  "uid": 42,
  "hotkey": "5F3sa...",
  "raw_weight": 0.123,
  "normalized_weight": 0.004
}
```

## Running the API

### Option 1: With Validator (Recommended)
Run both validator and API together with pm2:
```bash
./scripts/run_validator_with_api.sh
```

The API will run on port 8094 by default (configurable via `API_PORT` env var).

### Option 2: Standalone
For testing or development:
```bash
python -m bitcast.validator.api.weights_api
```

### Option 3: Custom Port
```python
from bitcast.validator.api.weights_api import run_api
run_api(host="0.0.0.0", port=8888)
```

## Rate Limits

Rate limits are enforced per IP address:
- `/health`: 60 requests/minute
- `/weights`: 10 requests/minute
- `/weights/{uid}`: 20 requests/minute

Exceeding limits returns HTTP 429 (Too Many Requests).

## Configuration

The API reads the netuid from the `NETUID` environment variable (defaults to 93).
It will look for the state file at:
```
~/.bittensor/miners/{WALLET_NAME}/{HOTKEY_NAME}/netuid{NETUID}/validator/state_mech_{MECHID}.npz
```

If the state file doesn't exist yet (validator still initializing), the API returns HTTP 404.

## Security Notes

1. **Read-Only**: This API cannot modify validator state
2. **No Authentication**: Currently public - add auth if needed
3. **Local State**: Reads from local disk, not blockchain
4. **CORS**: Not configured - add if needed for web frontends

## Example Usage

```bash
# Check health
curl http://localhost:8094/health

# Get all weights
curl http://localhost:8094/weights

# Get specific miner
curl http://localhost:8094/weights/42
```

## Development

Run tests:
```bash
source ~/venv_bitcast_x/bin/activate
pytest tests/validator/api/test_weights_api.py -v
```

