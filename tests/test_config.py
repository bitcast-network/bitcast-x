"""Tests for safe, launch-ready configuration defaults."""

from pathlib import Path

import pytest

from bitcast_x.campaign_urls import CAMPAIGN_FEED_URL
from bitcast_x.config import Settings
from bitcast_x.qualification import (
    PUBLIC_FINNEY_QUALIFICATION_SCHEDULE,
    PUBLIC_QUALIFICATION_OWNER_HOTKEY,
)

ROOT = Path(__file__).parents[1]


def test_operator_defaults_match_the_published_network() -> None:
    settings = Settings(_env_file=None)

    assert settings.campaign_feed_url == (
        "https://bitcast-api.bitcast.network/api/v2/public/x/campaign-manifest-v4"
    )
    assert settings.qualification_policy is PUBLIC_FINNEY_QUALIFICATION_SCHEDULE
    # Legacy single-config fields apply only off Finney and default to the public values.
    assert (
        settings.qualification_owner_hotkey,
        settings.qualification_minimum_alpha,
        settings.qualification_minimum_self_stake_alpha,
    ) == (PUBLIC_QUALIFICATION_OWNER_HOTKEY, "15000", None)
    assert settings.enable_data_publish is True
    assert settings.enable_weight_submission is True
    assert settings.auto_update is False
    assert settings.validator_preview_max_concurrency == 2
    # Host identity, secrets and third-party log forwarding are injected at runtime.
    for unconfigured in (
        "public_ip",
        "desearch_api_key",
        "llm_api_key",
        "loki_url",
        "loki_username",
        "loki_token",
    ):
        assert getattr(settings, unconfigured) is None, unconfigured


@pytest.mark.parametrize(
    "template",
    (".env.example", "config/validator.env.example", "config/miner.env.example"),
)
def test_environment_templates_ship_the_canonical_feed_without_placeholders(
    template: str,
) -> None:
    text = (ROOT / template).read_text(encoding="utf-8")

    assert f"BITCAST_X_CAMPAIGN_FEED_URL={CAMPAIGN_FEED_URL}\n" in text
    # Finney qualification history and the protocol start ship with the release.
    assert "BITCAST_X_QUALIFICATION_SCHEDULE_JSON=" not in text
    assert "BITCAST_X_PROTOCOL_START_BLOCK" not in text
    assert "example.invalid" not in text
    assert "ReplaceWithPublished" not in text


def test_environment_template_documents_release_owned_qualification_history() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "qualification history ships with each reviewed release" in text
