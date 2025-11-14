#!/bin/bash

# Exit on error
set -e

# Get the absolute path of the project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_PARENT="$(cd "$PROJECT_ROOT/.." && pwd)"

# Load environment variables from .env file
if [ -f "$PROJECT_ROOT/bitcast/validator/.env" ]; then
  export $(grep -v '^#' "$PROJECT_ROOT/bitcast/validator/.env" | sed 's/ *= */=/g' | xargs)
fi

# Set default values if variables are not set
VENV_PATH=${VENV_PATH:-"$PROJECT_PARENT/venv_bitcast_x"}
PM2_VALIDATOR_NAME=${PM2_VALIDATOR_NAME:-"bitcast_x_validator"}
PM2_API_NAME=${PM2_API_NAME:-"bitcast_x_weights_api"}

# Activate virtual environment
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please run setup_env.sh first"
    exit 1
fi

echo "Activating virtual environment..."
source "$VENV_PATH/bin/activate"

# Ensure required environment variables are set
if [ -z "$CHUTES_API_KEY" ]; then
  echo "Error: CHUTES_API_KEY is not set in the .env file."
  exit 1
fi
if [ -z "$RAPID_API_KEY" ]; then
  echo "Error: RAPID_API_KEY is not set in the .env file."
  exit 1
fi
if [ -z "$WANDB_API_KEY" ]; then
  echo "Error: WANDB_API_KEY is not set in the .env file."
  exit 1
fi
if [ -z "$WALLET_NAME" ]; then
  echo "Error: WALLET_NAME is not set in the .env file."
  exit 1
fi
if [ -z "$HOTKEY_NAME" ]; then
  echo "Error: HOTKEY_NAME is not set in the .env file."
  exit 1
fi

# Set default values for validator parameters if not set in .env
NETUID=${NETUID:-93}
MECHID=${MECHID:-1}
SUBTENSOR_NETWORK=${SUBTENSOR_NETWORK:-"finney"}
SUBTENSOR_CHAIN_ENDPOINT=${SUBTENSOR_CHAIN_ENDPOINT:-"wss://entrypoint-finney.opentensor.ai:443"}
PORT=${PORT:-8092}
API_PORT=${API_PORT:-8094}
LOGGING=${LOGGING:-"--logging.info"}

# Handle boolean flags
DISABLE_AUTO_UPDATE_FLAG=""
if [ "${DISABLE_AUTO_UPDATE,,}" = "true" ]; then
    DISABLE_AUTO_UPDATE_FLAG="--neuron.disable_auto_update"
fi

# Clear cache if specified 
while [[ $# -gt 0 ]]; do
  case $1 in
    --clear-cache)
      rm -rf "$PROJECT_ROOT/cache"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

# Login to Weights & Biases
if ! wandb login $WANDB_API_KEY; then
  echo "Failed to login to Weights & Biases with the provided API key."
  exit 1
fi

# START/RESTART VALIDATOR PROCESS
if pm2 list | grep -q "$PM2_VALIDATOR_NAME"; then
  echo "Process '$PM2_VALIDATOR_NAME' is already running. Restarting it..."
  pm2 restart "$PM2_VALIDATOR_NAME"
else
  echo "Process '$PM2_VALIDATOR_NAME' is not running. Starting it for the first time..."
  pm2 start python --name "$PM2_VALIDATOR_NAME" -- neurons/validator.py --netuid $NETUID --subtensor.chain_endpoint $SUBTENSOR_CHAIN_ENDPOINT --subtensor.network $SUBTENSOR_NETWORK --wallet.name $WALLET_NAME --wallet.hotkey $HOTKEY_NAME --axon.port $PORT $LOGGING $DISABLE_AUTO_UPDATE_FLAG
fi

# START/RESTART WEIGHTS API PROCESS
if pm2 list | grep -q "$PM2_API_NAME"; then
  echo "Process '$PM2_API_NAME' is already running. Restarting it..."
  pm2 restart "$PM2_API_NAME"
else
  echo "Process '$PM2_API_NAME' is not running. Starting it for the first time..."
  pm2 start "$VENV_PATH/bin/python" --name "$PM2_API_NAME" -- -m bitcast.validator.api.weights_api
fi

echo ""
echo "✅ Validator and Weights API started successfully!"
echo "   Validator: $PM2_VALIDATOR_NAME (port $PORT)"
echo "   Weights API: $PM2_API_NAME (port $API_PORT)"
echo ""
echo "View logs:"
echo "   pm2 logs $PM2_VALIDATOR_NAME"
echo "   pm2 logs $PM2_API_NAME"
echo ""
echo "Stop processes:"
echo "   pm2 stop $PM2_VALIDATOR_NAME"
echo "   pm2 stop $PM2_API_NAME"

