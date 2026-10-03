"""Container entrypoint wallet identity checks."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
EXPECTED_HOTKEY = "5DAZk8VdarYaEeUByitfm34Fgor7sgBXDmhj2Q2tepMKQ9fv"


def keyfile(address: str) -> str:
    """Return a synthetic keyfile without real private material."""

    return json.dumps({"ss58Address": address, "secretSeed": "synthetic-test-only"})


def injected_hotkey(address: str) -> dict[str, str]:
    """Return the secret-injection environment for a hotkey with ``address``."""

    return {
        "HOTKEY_DATA": base64.b64encode(keyfile(address).encode()).decode(),
        "BITCAST_X_EXPECTED_HOTKEY": EXPECTED_HOTKEY,
    }


def run_entrypoint(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the entrypoint with an isolated wallet path and a harmless application stub."""

    executable = tmp_path / "bitcast-x"
    executable.write_text("#!/bin/sh\nprintf 'started:%s' \"$*\"\n")
    executable.chmod(0o755)
    return subprocess.run(  # noqa: S603 - fixed script, synthetic environment
        ["/bin/sh", "./entrypoint.sh", *args],
        cwd=ROOT,
        env={
            "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "WALLET_PATH": str(tmp_path / "wallets"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_entrypoint_starts_when_hotkey_matches_expected_uid(tmp_path: Path) -> None:
    result = run_entrypoint(tmp_path, ("--probe",), injected_hotkey(EXPECTED_HOTKEY))

    assert result.returncode == 0
    assert result.stdout.endswith("started:--probe")


@pytest.mark.parametrize("flag", ("--help", "--version"))
def test_entrypoint_informational_flags_do_not_require_or_write_hotkey(
    tmp_path: Path, flag: str
) -> None:
    result = run_entrypoint(tmp_path, (flag,), {})

    assert result.returncode == 0
    assert result.stdout == f"started:{flag}"
    assert not (tmp_path / "wallets").exists()


def test_entrypoint_uses_existing_mounted_hotkey_without_secret(tmp_path: Path) -> None:
    hotkey = tmp_path / "wallets" / "default" / "hotkeys" / "default"
    hotkey.parent.mkdir(parents=True)
    hotkey.write_text(keyfile(EXPECTED_HOTKEY))

    result = run_entrypoint(tmp_path, ("--probe",), {"BITCAST_X_EXPECTED_HOTKEY": EXPECTED_HOTKEY})

    assert result.returncode == 0
    assert "Using mounted wallet hotkey" in result.stdout
    assert result.stdout.endswith("started:--probe")


def test_entrypoint_rejects_wrong_hotkey_before_start(tmp_path: Path) -> None:
    result = run_entrypoint(
        tmp_path, (), injected_hotkey("5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA")
    )

    assert result.returncode != 0
    assert "does not match BITCAST_X_EXPECTED_HOTKEY" in result.stderr
    assert "started" not in result.stdout
