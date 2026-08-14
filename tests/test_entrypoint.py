"""Container entrypoint wallet identity checks."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

EXPECTED_HOTKEY = "5DAZk8VdarYaEeUByitfm34Fgor7sgBXDmhj2Q2tepMKQ9fv"


def encoded_keyfile(address: str) -> str:
    """Return an encoded synthetic keyfile without real private material."""

    payload = json.dumps({"ss58Address": address, "secretSeed": "synthetic-test-only"})
    return base64.b64encode(payload.encode()).decode()


def entrypoint_environment(tmp_path: Path, address: str) -> dict[str, str]:
    """Build an isolated entrypoint environment and harmless application stub."""

    executable = tmp_path / "bitcast-x"
    executable.write_text("#!/bin/sh\nprintf 'started:%s' \"$*\"\n")
    executable.chmod(0o755)
    return {
        "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        "WALLET_PATH": str(tmp_path / "wallets"),
        "HOTKEY_DATA": encoded_keyfile(address),
        "BITCAST_X_EXPECTED_HOTKEY": EXPECTED_HOTKEY,
    }


def test_entrypoint_starts_when_hotkey_matches_expected_uid(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", "./entrypoint.sh", "--probe"],
        cwd=Path(__file__).parents[1],
        env=entrypoint_environment(tmp_path, EXPECTED_HOTKEY),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.endswith("started:--probe")


def test_entrypoint_help_does_not_require_or_write_hotkey(tmp_path: Path) -> None:
    executable = tmp_path / "bitcast-x"
    executable.write_text("#!/bin/sh\nprintf 'started:%s' \"$*\"\n")
    executable.chmod(0o755)
    wallet_path = tmp_path / "wallets"

    result = subprocess.run(
        ["/bin/sh", "./entrypoint.sh", "--help"],
        cwd=Path(__file__).parents[1],
        env={
            "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "WALLET_PATH": str(wallet_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "started:--help"
    assert not wallet_path.exists()


def test_entrypoint_version_does_not_require_or_write_hotkey(tmp_path: Path) -> None:
    executable = tmp_path / "bitcast-x"
    executable.write_text("#!/bin/sh\nprintf 'started:%s' \"$*\"\n")
    executable.chmod(0o755)
    wallet_path = tmp_path / "wallets"

    result = subprocess.run(
        ["/bin/sh", "./entrypoint.sh", "--version"],
        cwd=Path(__file__).parents[1],
        env={
            "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "WALLET_PATH": str(wallet_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "started:--version"
    assert not wallet_path.exists()


def test_entrypoint_uses_existing_mounted_hotkey_without_secret(tmp_path: Path) -> None:
    executable = tmp_path / "bitcast-x"
    executable.write_text("#!/bin/sh\nprintf 'started:%s' \"$*\"\n")
    executable.chmod(0o755)
    wallet_path = tmp_path / "wallets"
    hotkey = wallet_path / "default" / "hotkeys" / "default"
    hotkey.parent.mkdir(parents=True)
    hotkey.write_text(
        json.dumps({"ss58Address": EXPECTED_HOTKEY, "secretSeed": "synthetic-test-only"})
    )

    result = subprocess.run(
        ["/bin/sh", "./entrypoint.sh", "--probe"],
        cwd=Path(__file__).parents[1],
        env={
            "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "WALLET_PATH": str(wallet_path),
            "BITCAST_X_EXPECTED_HOTKEY": EXPECTED_HOTKEY,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Using mounted wallet hotkey" in result.stdout
    assert result.stdout.endswith("started:--probe")


def test_entrypoint_rejects_wrong_hotkey_before_start(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", "./entrypoint.sh"],
        cwd=Path(__file__).parents[1],
        env=entrypoint_environment(
            tmp_path,
            "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "does not match BITCAST_X_EXPECTED_HOTKEY" in result.stderr
    assert "started" not in result.stdout
