"""Safe source-checkout updates supervised outside the validator process."""

import asyncio
import json
import logging
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from shutil import which

import httpx

from bitcast_x.config import Settings
from bitcast_x.miner.store import MinerStore
from bitcast_x.sqlite import backup_database
from bitcast_x.validator.store import ValidatorStore

LOGGER = logging.getLogger(__name__)
_ACTIVE_FILE = "active.json"


@dataclass(frozen=True, slots=True)
class Release:
    """One prepared source release and its isolated Python environment."""

    commit: str
    root: Path
    python: Path


def find_source_root(start: Path | None = None) -> Path | None:
    """Find the Git checkout containing the installed package without changing it."""

    candidate = (start or Path(__file__)).resolve()
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return parent
    return None


def auto_update_enabled(settings: Settings) -> bool:
    """Return whether the operator explicitly enabled source updates."""

    return settings.auto_update


def verify_automatic_upgrade(state_dir: Path) -> dict[str, int]:
    """Exercise candidate migrations and reject versioned schema changes."""

    versions: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="bitcast-x-upgrade-check-") as raw_temp:
        temporary = Path(raw_temp)
        for name, factory in (
            ("miner.sqlite3", lambda path: MinerStore(path)),
            ("validator.sqlite3", lambda path: ValidatorStore(path, start_block=0)),
        ):
            source = state_dir / name
            if not source.exists():
                continue
            before = _schema_version(source)
            copied = temporary / name
            backup_database(source, copied)
            factory(copied)
            after = _schema_version(copied)
            if after != before:
                raise RuntimeError(
                    f"automatic update requires a manual schema upgrade for {name}: "
                    f"{before} -> {after}"
                )
            versions[name] = before
    return versions


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


class SourceUpdateManager:
    """Prepare releases in isolated worktrees without mutating code or validator state."""

    def __init__(self, source_root: Path, settings: Settings) -> None:
        self.source_root = source_root.resolve()
        self.settings = settings
        self.update_dir = settings.auto_update_dir.expanduser().resolve()
        state_dir = settings.state_dir.expanduser().resolve()
        if self.update_dir.is_relative_to(state_dir) or state_dir.is_relative_to(self.update_dir):
            raise ValueError("auto-update directory and validator state directory must not overlap")
        git = which("git")
        uv = which("uv")
        if git is None or uv is None:
            raise RuntimeError("automatic updates require git and uv on PATH")
        self.git: str = git
        self.uv: str = uv

    def current_release(self) -> Release:
        """Return a previously activated release or the invoking checkout."""

        active_path = self.update_dir / _ACTIVE_FILE
        if active_path.exists():
            try:
                payload = json.loads(active_path.read_text(encoding="utf-8"))
                release = Release(
                    commit=str(payload["commit"]),
                    root=Path(payload["root"]).resolve(),
                    python=Path(payload["python"]).resolve(),
                )
                releases_dir = (self.update_dir / "releases").resolve()
                if (
                    release.root.is_relative_to(releases_dir)
                    and release.python.is_relative_to(release.root)
                    and release.root.is_dir()
                    and release.python.is_file()
                ):
                    return release
            except (KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
                LOGGER.warning("ignoring invalid auto-update activation record")
        return Release(self._git("rev-parse", "HEAD"), self.source_root, Path(sys.executable))

    def prepare(self, active: Release) -> Release | None:
        """Fetch and validate a newer fast-forward release without touching live state."""

        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("refusing automatic update from a checkout with tracked changes")
        self._git("fetch", "--quiet", "origin")
        target = self._git("rev-parse", self.settings.auto_update_ref)
        if target == active.commit:
            return None
        ancestry = subprocess.run(  # noqa: S603 - executable and commits are validated locally
            [self.git, "merge-base", "--is-ancestor", active.commit, target],
            cwd=self.source_root,
            check=False,
        )
        if ancestry.returncode != 0:
            raise RuntimeError(
                f"refusing non-fast-forward update {active.commit[:12]} -> {target[:12]}"
            )
        release_root = self.update_dir / "releases" / target
        if not release_root.exists():
            release_root.parent.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "--detach", str(release_root), target)
        elif self._git("rev-parse", "HEAD", cwd=release_root) != target:
            raise RuntimeError(f"prepared release directory has unexpected commit: {release_root}")
        self._run(self.uv, "sync", "--frozen", "--no-dev", "--project", str(release_root))
        python = release_root / ".venv" / "bin" / "python"
        if not python.is_file():
            raise RuntimeError(f"candidate Python environment is missing: {python}")
        self._run(str(python), "-c", "import bitcast_x")
        self._run(
            str(python),
            "-m",
            "bitcast_x.main",
            "_auto-update-check",
            env={**os.environ, "BITCAST_X_STATE_DIR": str(self.settings.state_dir)},
        )
        return Release(target, release_root, python)

    def activate(self, release: Release) -> None:
        """Atomically record a release only after its validator survives startup."""

        self.update_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.update_dir / f".{_ACTIVE_FILE}.{os.getpid()}"
        temporary.write_text(
            json.dumps(
                {
                    "commit": release.commit,
                    "root": str(release.root),
                    "python": str(release.python),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.update_dir / _ACTIVE_FILE)

    def _git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(  # noqa: S603 - fixed executable with internal arguments only
            [self.git, *arguments],
            cwd=cwd or self.source_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
        return str(result.stdout).strip()

    @staticmethod
    def _run(*arguments: str, env: dict[str, str] | None = None) -> None:
        result = subprocess.run(  # noqa: S603 - candidate commands are constructed internally
            arguments, check=False, env=env
        )
        if result.returncode != 0:
            raise RuntimeError(f"candidate check failed ({arguments[0]} exit {result.returncode})")


async def run_validator_supervised(settings: Settings, source_root: Path) -> None:
    """Run the validator child and switch only to fully prepared source releases."""

    manager = SourceUpdateManager(source_root, settings)
    active = manager.current_release()
    child = await _start_validator(active)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(name, stopping.set)
    try:
        while not stopping.is_set():
            delay = settings.auto_update_interval_seconds + secrets.randbelow(60)
            stop_wait = asyncio.create_task(stopping.wait())
            child_wait = asyncio.create_task(child.wait())
            done, pending = await asyncio.wait(
                {stop_wait, child_wait}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            if stop_wait in done and stop_wait.result():
                break
            if child_wait in done:
                raise RuntimeError(f"validator exited unexpectedly with status {child.returncode}")
            try:
                candidate = await asyncio.to_thread(manager.prepare, active)
            except Exception:
                LOGGER.exception("automatic update check failed; current validator remains active")
                continue
            if candidate is None:
                continue
            replacement = await _replace_validator(child, candidate, settings)
            if replacement is None:
                LOGGER.error("candidate failed startup; restoring previous validator release")
                child = await _start_validator(active)
                continue
            child = replacement
            active = candidate
            manager.activate(active)
            LOGGER.info("activated validator update commit=%s", active.commit)
    finally:
        await _stop_validator(child, timeout_seconds=settings.auto_update_shutdown_timeout_seconds)


async def _start_validator(release: Release) -> asyncio.subprocess.Process:
    LOGGER.info("starting validator release commit=%s", release.commit)
    return await asyncio.create_subprocess_exec(
        str(release.python), "-m", "bitcast_x.main", "_run-validator"
    )


async def _replace_validator(
    current: asyncio.subprocess.Process,
    candidate: Release,
    settings: Settings,
) -> asyncio.subprocess.Process | None:
    await _stop_validator(current, timeout_seconds=settings.auto_update_shutdown_timeout_seconds)
    replacement = await _start_validator(candidate)
    deadline = asyncio.get_running_loop().time() + settings.auto_update_startup_grace_seconds
    url = f"http://127.0.0.1:{settings.ops_port}/health"
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
        while asyncio.get_running_loop().time() < deadline:
            if replacement.returncode is not None:
                return None
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return replacement
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    await _stop_validator(replacement)
    return None


async def _stop_validator(
    child: asyncio.subprocess.Process, *, timeout_seconds: float = 30.0
) -> None:
    if child.returncode is not None:
        return
    child.terminate()
    try:
        await asyncio.wait_for(child.wait(), timeout=timeout_seconds)
    except TimeoutError:
        child.kill()
        await child.wait()
