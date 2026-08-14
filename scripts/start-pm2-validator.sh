#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"
app_bin="${project_root}/.venv/bin/bitcast-x"
env_file="${project_root}/.env"
app_name="bitcast-x-validator"

fail() {
    printf 'launch error: %s\n' "$1" >&2
    exit 1
}

command -v pm2 >/dev/null 2>&1 || fail "PM2 is required; run scripts/setup-pm2-validator.sh"
command -v curl >/dev/null 2>&1 || fail "curl is required for the local health check"
[[ -x "${python_bin}" && -x "${app_bin}" ]] || \
    fail "the locked virtual environment is missing; run scripts/setup-pm2-validator.sh"
[[ -f "${env_file}" ]] || fail ".env is missing; run scripts/setup-pm2-validator.sh"

cd "${project_root}"
ops_port="$(${python_bin} - <<'PY'
import sys

from bitcast_x.config import Settings

settings = Settings()
missing: list[str] = []
if settings.campaign_feed_url is None:
    missing.append("BITCAST_X_CAMPAIGN_FEED_URL")
if not settings.desearch_api_key:
    missing.append("BITCAST_X_DESEARCH_API_KEY")
if not settings.llm_api_key:
    missing.append(
        "BITCAST_X_CHUTES_API_KEY"
        if settings.llm_provider == "chutes"
        else "BITCAST_X_OPENROUTER_API_KEY"
    )

hotkey_path = settings.wallet_path / settings.wallet_name / "hotkeys" / settings.wallet_hotkey
if not hotkey_path.is_file():
    missing.append(f"wallet hotkey file {hotkey_path}")

if missing:
    print("launch error: missing required validator configuration:", file=sys.stderr)
    for item in missing:
        print(f"  - {item}", file=sys.stderr)
    raise SystemExit(1)

print(settings.ops_port)
PY
)"

pm2 startOrRestart "${project_root}/ecosystem.config.cjs" --only "${app_name}" --update-env

health_url="http://127.0.0.1:${ops_port}/health"
for _attempt in {1..30}; do
    if curl --fail --silent --show-error "${health_url}" >/dev/null 2>&1; then
        pm2 save
        pm2 status "${app_name}"
        printf 'Validator health check passed: %s\n' "${health_url}"
        printf 'Follow logs with: pm2 logs %s\n' "${app_name}"
        exit 0
    fi
    sleep 2
done

printf 'Validator did not become healthy at %s within 60 seconds.\n' "${health_url}" >&2
pm2 status "${app_name}" || true
pm2 logs "${app_name}" --lines 50 --nostream || true
exit 1
