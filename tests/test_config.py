"""Tests for safe, launch-ready configuration defaults."""

from pathlib import Path

from bitcast_x.config import Settings
from bitcast_x.qualification import PUBLIC_FINNEY_QUALIFICATION_SCHEDULE


def test_public_protocol_defaults_match_the_published_network() -> None:
    settings = Settings(_env_file=None)

    assert settings.campaign_feed_url == (
        "https://bitcast-api.bitcast.network/api/v2/public/x/campaign-manifest-v4"
    )
    assert settings.legacy_nocode_uid == 154
    assert settings.legacy_connection_tweet_ids == "2031383975088836738"
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


def test_environment_template_contains_real_public_protocol_values() -> None:
    template = Path(".env.example").read_text()

    assert "BITCAST_X_CAMPAIGN_FEED_URL=" + str(Settings().campaign_feed_url) in template
    assert "BITCAST_X_PROTOCOL_START_BLOCK" not in template
    assert "BITCAST_X_LEGACY_NOCODE_UID=154" in template
    assert "BITCAST_X_LEGACY_CONNECTION_TWEET_IDS=2031383975088836738" in template
    assert "qualification history ships with each reviewed release" in template
    assert "BITCAST_X_QUALIFICATION_SCHEDULE_JSON=" not in template
    assert "BITCAST_X_VALIDATOR_PREVIEW_MAX_CONCURRENCY=2" in template
    assert "BITCAST_X_ENABLE_DATA_PUBLISH=true" in template
    assert "BITCAST_X_ENABLE_WEIGHT_SUBMISSION=true" in template
    assert "example.invalid" not in template
    assert "ReplaceWithPublished" not in template
