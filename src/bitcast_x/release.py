"""Immutable runtime identity for source and container installations."""

import os
import re
import shutil
import subprocess
from pathlib import Path

_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def source_revision() -> str:
    """Return the exact build revision, or detect it from a source checkout."""

    configured = os.environ.get("BITCAST_X_SOURCE_REVISION", "").strip().lower()
    if _REVISION_PATTERN.fullmatch(configured):
        return configured

    source_root = Path(__file__).resolve().parents[2]
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        result = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
            [git, "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip().lower()
    return revision if _REVISION_PATTERN.fullmatch(revision) else "unknown"
