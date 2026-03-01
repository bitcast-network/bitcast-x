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
PM2_PROCESS_NAME=${PM2_PROCESS_NAME:-"bitcast_x_validator"}

# Activate virtual environment
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please run setup_env.sh first"
    exit 1
fi

echo "Activating virtual environment..."
source "$VENV_PATH/bin/activate"

# Ensure required environment variables are set
if [ -z "$WALLET_NAME" ]; then
  echo "Error: WALLET_NAME is not set in the .env file."
  exit 1
fi
if [ -z "$HOTKEY_NAME" ]; then
  echo "Error: HOTKEY_NAME is not set in the .env file."
  exit 1
fi

# Warn about missing API keys (not required for WC mode)
if [ -z "$CHUTES_API_KEY" ]; then
  echo "Warning: CHUTES_API_KEY is not set (required for full validation mode)"
fi
if [ -z "$DESEARCH_API_KEY" ]; then
  echo "Warning: DESEARCH_API_KEY is not set (required for full validation mode)"
fi
if [ -z "$WANDB_API_KEY" ]; then
  echo "Warning: WANDB_API_KEY is not set (required for full validation mode)"
fi

# Set default values for validator parameters if not set in .env
NETUID=${NETUID:-93}
MECHID=${MECHID:-1}
SUBTENSOR_NETWORK=${SUBTENSOR_NETWORK:-"finney"}
SUBTENSOR_CHAIN_ENDPOINT=${SUBTENSOR_CHAIN_ENDPOINT:-"wss://entrypoint-finney.opentensor.ai:443"}
PORT=${PORT:-8093}
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

# Login to Weights & Biases if API key is provided
if [ -n "$WANDB_API_KEY" ]; then
  if ! wandb login $WANDB_API_KEY; then
    echo "Warning: Failed to login to Weights & Biases with the provided API key."
  fi
fi

# STOP/START VALIDATOR PROCESS
if pm2 list | grep -q "$PM2_PROCESS_NAME"; then
  echo "Process '$PM2_PROCESS_NAME' is already running. Restarting it..."
  pm2 restart "$PM2_PROCESS_NAME"
else
  echo "Process '$PM2_PROCESS_NAME' is not running. Starting it for the first time..."
  pm2 start python --name "$PM2_PROCESS_NAME" -- "$PROJECT_ROOT/neurons/validator.py" --netuid $NETUID --subtensor.chain_endpoint $SUBTENSOR_CHAIN_ENDPOINT --subtensor.network $SUBTENSOR_NETWORK --wallet.name $WALLET_NAME --wallet.hotkey $HOTKEY_NAME --axon.port $PORT $LOGGING $DISABLE_AUTO_UPDATE_FLAG
fi