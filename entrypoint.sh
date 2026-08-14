#!/bin/sh
set -eu

# Informational CLI output must not require or write wallet material.
case "${1:-}" in
    -h|--help|--version)
        exec bitcast-x "$@"
        ;;
esac

wallet_base="${WALLET_PATH:-${BITCAST_X_WALLET_PATH:-/var/lib/bitcast-wallets}}"
wallet_name="${WALLET_NAME:-${BITCAST_X_WALLET_NAME:-default}}"
hotkey_name="${HOTKEY_NAME:-${BITCAST_X_WALLET_HOTKEY:-default}}"
wallet_dir="${wallet_base}/${wallet_name}"
hotkey_dir="${wallet_dir}/hotkeys"
hotkey_path="${hotkey_dir}/${hotkey_name}"

mkdir -p "${hotkey_dir}"
if [ -n "${HOTKEY_DATA:-}" ]; then
    printf '%s' "${HOTKEY_DATA}" | base64 -d > "${hotkey_path}"
    chmod 0600 "${hotkey_path}"
    echo "[entrypoint] Wallet bootstrapped at ${hotkey_path}"
elif [ -f "${hotkey_path}" ]; then
    echo "[entrypoint] Using mounted wallet hotkey at ${hotkey_path}"
else
    echo "[entrypoint] ERROR: provide HOTKEY_DATA or mount ${hotkey_path}" >&2
    exit 1
fi

if [ -n "${BITCAST_X_EXPECTED_HOTKEY:-}" ]; then
    python - "${hotkey_path}" "${BITCAST_X_EXPECTED_HOTKEY}" <<'PY'
import json
import sys

keyfile_path, expected_hotkey = sys.argv[1:]
try:
    with open(keyfile_path, encoding="utf-8") as keyfile:
        keyfile_data = json.load(keyfile)
except (OSError, ValueError) as error:
    raise SystemExit("[entrypoint] ERROR: HOTKEY_DATA is not a valid JSON keyfile") from error

if keyfile_data.get("ss58Address") != expected_hotkey:
    raise SystemExit("[entrypoint] ERROR: HOTKEY_DATA does not match BITCAST_X_EXPECTED_HOTKEY")
PY
fi

if [ -n "${COLDKEYPUB_DATA:-}" ]; then
    printf '%s\n' "${COLDKEYPUB_DATA}" > "${wallet_dir}/coldkeypub.txt"
    chmod 0644 "${wallet_dir}/coldkeypub.txt"
fi

unset HOTKEY_DATA
exec bitcast-x "$@"
