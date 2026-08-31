"""Command-line journey for the minimal Bitcast X reference miner."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bitcast_x import __version__
from bitcast_x.auto_update import (
    auto_update_enabled,
    find_source_root,
    run_validator_supervised,
    verify_automatic_upgrade,
)
from bitcast_x.campaigns import CampaignFeedClient
from bitcast_x.config import Settings, get_settings
from bitcast_x.legacy.preflight import inspect_legacy_state
from bitcast_x.logging import configure_logging
from bitcast_x.miner.service import ReferenceMiner, build_sdk
from bitcast_x.miner.web import run_miner_api
from bitcast_x.state import backup_state, inspect_state, shadow_report
from bitcast_x.validator.service import ValidatorService


def build_parser() -> argparse.ArgumentParser:
    """Build the stable, deliberately small reference-miner CLI."""

    parser = argparse.ArgumentParser(prog="bitcast-x")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run-miner", help="run and advertise the signed reference miner")
    commands.add_parser(
        "run-miner-api",
        help="run the authenticated platform API and signed miner protocol",
    )
    commands.add_parser("run-validator", help="run validator ingestion and optional outputs")
    commands.add_parser("_run-validator", help=argparse.SUPPRESS)
    commands.add_parser("_auto-update-check", help=argparse.SUPPRESS)
    commands.add_parser("campaigns", help="list the shared public campaign feed")

    claim = commands.add_parser("claim", help="commit a private creator draft")
    claim.add_argument("--campaign-id", required=True)
    claim.add_argument("--creator-x-id", required=True)
    claim.add_argument("--draft", required=True)

    claim_status = commands.add_parser("claim-status", help="inspect a local claim")
    claim_status.add_argument("claim_id")

    submit = commands.add_parser("submit", help="commit a completed tweet mapping")
    submit.add_argument("--campaign-id", required=True)
    submit.add_argument("--tweet-id", required=True)
    submit.add_argument("--claim-id")
    submit.add_argument("--creator-x-id", required=True)

    submission_status = commands.add_parser(
        "submission-status", help="inspect a local tweet submission"
    )
    submission_status.add_argument("submission_id")
    commands.add_parser("qualification", help="explain current miner qualification")
    commands.add_parser("state-info", help="check durable database integrity and schema versions")
    commands.add_parser("legacy-state-info", help="verify the imported v1/v2 state read-only")
    commands.add_parser("shadow-report", help="hash frozen shadow outputs for validator comparison")
    backup = commands.add_parser("backup-state", help="create a consistent online state backup")
    backup.add_argument("--output", required=True)
    resume = commands.add_parser(
        "resume-history",
        help="seal unusable local history and resume future participation",
    )
    resume.add_argument(
        "--confirm-hotkey",
        required=True,
        help="must exactly match the configured signing hotkey",
    )
    return parser


async def run_command(arguments: argparse.Namespace, settings: Settings) -> dict[str, Any] | None:
    """Execute one CLI operation and return its machine-readable result."""

    if arguments.command == "state-info":
        return inspect_state(settings.state_dir)
    if arguments.command == "legacy-state-info":
        return inspect_legacy_state(
            settings.legacy_connections_path or settings.state_dir / "connections.db",
            settings.legacy_snapshots_path or settings.state_dir / "reward_snapshots",
            settings.legacy_tweet_store_path or settings.state_dir / "legacy_tweet_store",
        )
    if arguments.command == "shadow-report":
        return shadow_report(settings.state_dir)
    if arguments.command == "backup-state":
        return backup_state(settings.state_dir, Path(arguments.output))
    if arguments.command == "_auto-update-check":
        return {"schemas": verify_automatic_upgrade(settings.state_dir)}

    if arguments.command == "campaigns":
        if settings.campaign_feed_url is None:
            raise ValueError("BITCAST_X_CAMPAIGN_FEED_URL is not configured")
        client = CampaignFeedClient(
            settings.campaign_feed_url,
            cache_path=settings.state_dir / "campaign-feed.json",
            timeout=settings.request_timeout_seconds,
            max_response_bytes=settings.max_response_bytes,
        )
        try:
            feed = await client.fetch()
        finally:
            await client.close()
        return feed.model_dump(mode="json")

    if arguments.command == "_run-validator":
        await ValidatorService(settings).run()
        return None
    if arguments.command == "run-validator":
        source_root = find_source_root()
        if auto_update_enabled(settings):
            if source_root is None:
                raise RuntimeError("automatic updates require a Git source checkout")
            await run_validator_supervised(settings, source_root)
        else:
            await ValidatorService(settings).run()
        return None

    if arguments.command == "run-miner-api":
        await run_miner_api(settings)
        return None

    chain, sdk = await build_sdk(settings)
    if arguments.command == "run-miner":
        await ReferenceMiner(settings, chain, sdk).run()
        return None
    try:
        if arguments.command == "resume-history":
            if arguments.confirm_hotkey != sdk.engine.miner_hotkey:
                raise ValueError("--confirm-hotkey does not match the configured signing hotkey")
            history_id = await sdk.engine.resume_history()
            return {
                "hotkey": sdk.engine.miner_hotkey,
                "history_id": history_id,
            }
        if arguments.command == "claim":
            claim_id = sdk.create_claim(
                campaign_id=arguments.campaign_id,
                creator_x_id=arguments.creator_x_id,
                draft=arguments.draft,
            )
            await sdk.engine.commit_ready(force=True)
            status = sdk.claim_status(claim_id)
            return {"claim_id": claim_id, "status": status.value if status else None}
        if arguments.command == "claim-status":
            status = sdk.claim_status(arguments.claim_id)
            return {"claim_id": arguments.claim_id, "status": status.value if status else None}
        if arguments.command == "submit":
            submission_id = sdk.submit_tweet(
                campaign_id=arguments.campaign_id,
                tweet_id=arguments.tweet_id,
                claim_id=arguments.claim_id,
                creator_x_id=arguments.creator_x_id,
            )
            await sdk.engine.commit_ready(force=True)
            status = sdk.submission_status(submission_id)
            return {
                "submission_id": submission_id,
                "status": status.value if status else None,
            }
        if arguments.command == "submission-status":
            status = sdk.submission_status(arguments.submission_id)
            return {
                "submission_id": arguments.submission_id,
                "status": status.value if status else None,
            }
        if arguments.command == "qualification":
            return await sdk.qualification_status()
        raise ValueError(f"unsupported command: {arguments.command}")
    finally:
        await chain.close()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the reference CLI and render one JSON result when applicable."""

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_output=settings.log_format == "json",
    )
    arguments = build_parser().parse_args(argv)
    result = asyncio.run(run_command(arguments, settings))
    if result is not None:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))  # noqa: T201


if __name__ == "__main__":
    main()
