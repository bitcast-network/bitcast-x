"""Static smoke tests for the checked PM2 role definitions."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(NODE is None, reason="node is required to evaluate the PM2 configuration")
def test_pm2_config_declares_isolated_miner_and_validator_roles(tmp_path: Path) -> None:
    executable = tmp_path / "bitcast-x"
    log_dir = tmp_path / "logs"
    result = subprocess.run(  # noqa: S603 - NODE is resolved from the trusted host PATH
        [
            str(NODE),
            "-e",
            "console.log(JSON.stringify(require('./ecosystem.config.cjs').apps))",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "BITCAST_X_PM2_EXECUTABLE": str(executable),
            "BITCAST_X_PM2_LOG_DIR": str(log_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    apps = json.loads(result.stdout)
    assert {app["name"] for app in apps} == {"bitcast-x-miner", "bitcast-x-validator"}
    assert {tuple(app["args"]) for app in apps} == {("run-miner",), ("run-validator",)}
    assert all(app["script"] == str(executable) for app in apps)
    assert all(app["interpreter"] == "none" for app in apps)
    assert all(app["instances"] == 1 and app["exec_mode"] == "fork" for app in apps)
    assert all(app["autorestart"] is True and app["watch"] is False for app in apps)
    assert all(Path(app["out_file"]).parent == log_dir for app in apps)
    assert all(Path(app["error_file"]).parent == log_dir for app in apps)


@pytest.mark.parametrize(
    "script_name",
    ("setup-pm2-validator.sh", "start-pm2-validator.sh"),
)
def test_pm2_validator_helper_scripts_parse(script_name: str) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required to inspect the PM2 helper scripts")

    subprocess.run(  # noqa: S603 - Bash is resolved from the trusted host PATH
        [bash, "-n", str(ROOT / "scripts" / script_name)],
        check=True,
    )


def test_pm2_validator_helpers_use_private_local_environment_and_one_role() -> None:
    setup = (ROOT / "scripts" / "setup-pm2-validator.sh").read_text(encoding="utf-8")
    start = (ROOT / "scripts" / "start-pm2-validator.sh").read_text(encoding="utf-8")

    assert "config/validator.env.example" in setup
    assert "config/providers.env.example" in setup
    assert 'if [[ -e "${env_file}" ]]' in setup
    assert 'chmod 0600 "${env_file}"' in setup
    assert '--only "${app_name}"' in start
    assert 'app_name="bitcast-x-validator"' in start
    assert "pm2 save" in start
