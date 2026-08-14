"""Tests for exact source revision discovery."""

from unittest.mock import Mock, patch

from bitcast_x.release import source_revision


def test_configured_source_revision_takes_precedence() -> None:
    revision = "a" * 40
    with patch.dict("os.environ", {"BITCAST_X_SOURCE_REVISION": revision}):
        assert source_revision() == revision


def test_invalid_configured_revision_falls_back_to_source_checkout() -> None:
    completed = Mock(stdout=("b" * 40) + "\n")
    with (
        patch.dict("os.environ", {"BITCAST_X_SOURCE_REVISION": "not-a-revision"}),
        patch("bitcast_x.release.shutil.which", return_value="/usr/bin/git"),
        patch("bitcast_x.release.subprocess.run", return_value=completed) as run,
    ):
        assert source_revision() == "b" * 40

    run.assert_called_once()


def test_revision_is_unknown_outside_a_build_or_checkout() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("bitcast_x.release.shutil.which", return_value="/usr/bin/git"),
        patch("bitcast_x.release.subprocess.run", side_effect=FileNotFoundError),
    ):
        assert source_revision() == "unknown"
