#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
operator_home="${HOME:?HOME must be set}"
validator_template="${project_root}/config/validator.env.example"
provider_template="${project_root}/config/providers.env.example"
env_file="${project_root}/.env"

fail() {
    printf 'setup error: %s\n' "$1" >&2
    exit 1
}

command -v uv >/dev/null 2>&1 || fail "uv is required: https://docs.astral.sh/uv/"
command -v node >/dev/null 2>&1 || fail "Node.js 18 or newer is required for PM2"
command -v pm2 >/dev/null 2>&1 || fail "PM2 is required: npm install --global pm2@latest"

node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ ! "${node_major}" =~ ^[0-9]+$ ]] || ((node_major < 18)); then
    fail "Node.js 18 or newer is required for PM2"
fi

cd "${project_root}"
printf 'Installing the locked Bitcast X runtime...\n'
uv sync --locked --no-dev

if [[ -e "${env_file}" ]]; then
    printf 'Keeping existing %s\n' "${env_file}"
else
    umask 077
    temporary_env="$(mktemp "${project_root}/.env.tmp.XXXXXX")"
    trap 'rm -f -- "${temporary_env}"' EXIT

    while IFS= read -r line || [[ -n "${line}" ]]; do
        case "${line}" in
            BITCAST_X_WALLET_PATH=*)
                line="BITCAST_X_WALLET_PATH=${operator_home}/.bittensor/wallets"
                ;;
            BITCAST_X_STATE_DIR=*)
                line="BITCAST_X_STATE_DIR=${operator_home}/.bitcast-x/validator-state"
                ;;
        esac
        printf '%s\n' "${line}"
    done < "${validator_template}" > "${temporary_env}"

    printf '\n' >> "${temporary_env}"
    sed -n '4,$p' "${provider_template}" >> "${temporary_env}"
    mv -- "${temporary_env}" "${env_file}"
    trap - EXIT
    printf 'Created %s from the validator and provider templates\n' "${env_file}"
fi

chmod 0600 "${env_file}"
mkdir -p -- "${operator_home}/.bitcast-x/validator-state" "${project_root}/logs"

printf '\nSetup complete. Next:\n'
printf '  1. Edit %s and add the provider keys and correct wallet name/hotkey.\n' "${env_file}"
printf '  2. Restore verified legacy state if legacy campaigns remain.\n'
printf '  3. Run %s/scripts/start-pm2-validator.sh\n' "${project_root}"
