"""Release metadata must identify one consistent, supportable artifact."""

import tomllib
from pathlib import Path

from bitcast_x import __version__

ROOT = Path(__file__).parents[1]


def test_release_version_is_consistent_across_artifact_surfaces() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]

    assert version == __version__
    assert f"ARG VERSION={version}" in (ROOT / "Dockerfile").read_text()
    assert 'BITCAST_X_SOURCE_REVISION="${REVISION}"' in (ROOT / "Dockerfile").read_text()
    assert f'IMAGE_VERSION: "{version}"' in (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert f"## [{version}]" in (ROOT / "CHANGELOG.md").read_text()
