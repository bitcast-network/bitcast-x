"""Tests for safe, launch-ready configuration defaults."""

from pathlib import Path

import pytest

from bitcast_x.campaign_urls import CAMPAIGN_FEED_URL
from bitcast_x.config import Settings
from bitcast_x.qualification import PUBLIC_FINNEY_QUALIFICATION_SCHEDULE

ROOT = Path(__file__).parents[1]


def test_public_protocol_defaults_match_the_published_network() -> None:
    settings = Settings(_env_file=None)

    assert settings.campaign_feed_url == (
        "https://bitcast-api.bitcast.network/api/v2/public/x/campaign-manifest-v4"
    )
    assert settings.qualification_owner_hotkey == (
        "5DAoDtMxVqtMu2Nd5E7QhPEGXDMgrySvE1b3rRT5ARDhfNNK"
    )
    assert settings.qualification_minimum_alpha == "15000"
    assert settings.qualification_minimum_self_stake_alpha is None
    assert settings.qualification_policy is PUBLIC_FINNEY_QUALIFICATION_SCHEDULE
    assert settings.validator_preview_max_concurrency == 2


def test_stale_finney_environment_cannot_override_release_schedule() -> None:
    settings = Settings(
        _env_file=None,
        qualification_schedule_json=(
            '{"configurations":[{"version":1,"owner_hotkey":'
            '"5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK",'
            '"minimum_conviction_alpha":"1","effective_block":0}]}'
        ),
    )

    assert settings.qualification_policy is PUBLIC_FINNEY_QUALIFICATION_SCHEDULE


def test_secrets_remain_unconfigured_and_production_outputs_are_enabled() -> None:
    settings = Settings(_env_file=None)

    assert settings.public_ip is None
    assert settings.desearch_api_key is None
    assert settings.llm_api_key is None
    assert settings.enable_data_publish is True
    assert settings.enable_weight_submission is True


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
