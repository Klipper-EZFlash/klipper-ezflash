"""
Startup update check and in-app update. The check is notify-only: it never fetches
objects or touches the working tree. apply_update() runs only when the user
asks for it. Both only work for installer-managed installs
(<base>/py venv next to a <base>/src git checkout).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .settings import _get_settings_path

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/Klipper-EZFlash/klipper-ezflash"
BRANCH = "main"
CHECK_INTERVAL = 7 * 24 * 60 * 60  # weekly network check while up to date
GIT_TIMEOUT = 3  # seconds; also bounds how long quitting can be delayed
UPDATE_TIMEOUT = 60  # seconds, for fetch/merge during an explicit update
PIP_TIMEOUT = 300  # seconds, for reinstalling when dependencies changed
ENV_SKIP = "EZFLASH_NO_UPDATE_CHECK"


@dataclass
class UpdateInfo:
    local: str
    remote: str
    checkout: Path

    @property
    def _display_path(self) -> str:
        path, home = str(self.checkout), str(Path.home())
        if path.startswith(home):
            path = "~" + path[len(home):]
        return path

    @property
    def fallback_command(self) -> str:
        """Manual recovery when a fast-forward update can't be applied
        (discards local edits in the checkout)."""
        p = self._display_path
        return f"git -C {p} fetch origin {BRANCH} && git -C {p} reset --hard FETCH_HEAD"

    @property
    def changes_url(self) -> str:
        return f"{REPO_URL}/compare/{self.local[:7]}...{self.remote[:7]}"


def _state_path() -> Path:
    return _get_settings_path().with_name("update_check.json")


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=4))
    except OSError:
        pass


def is_enabled() -> bool:
    """The stored toggle (the env var is checked in check_for_update)."""
    return bool(_read_state().get("enabled", True))


def set_enabled(value: bool) -> None:
    state = _read_state()
    state["enabled"] = value
    _write_state(state)


def _managed_checkout() -> Optional[Path]:
    checkout = Path(__file__).resolve().parent.parent
    if not (checkout / ".git").exists():
        return None
    if (Path(sys.prefix).resolve().parent / "src").resolve() != checkout:
        return None
    return checkout


def _git(checkout: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


def _check(force: bool) -> tuple[str, Optional[UpdateInfo]]:
    """Returns ("update", info), ("current", None) or ("unavailable", None)."""
    checkout = _managed_checkout()
    if checkout is None:
        return "unavailable", None
    try:
        local = _git(checkout, "rev-parse", "HEAD")
        state = _read_state()
        remote = state.get("remote")
        fresh = time.time() - state.get("checked_at", 0) < CHECK_INTERVAL
        if fresh and remote == local and not force:
            return "current", None
        line = _git(checkout, "ls-remote", "origin", f"refs/heads/{BRANCH}")
        remote = line.split()[0] if line else None
        if not remote:
            return "unavailable", None
        state.update(checked_at=time.time(), remote=remote)
        _write_state(state)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        logger.debug("update check failed: %s", e)
        return "unavailable", None
    if remote != local:
        return "update", UpdateInfo(local, remote, checkout)
    return "current", None


def check_for_update() -> Optional[UpdateInfo]:
    """Startup path: honours the env var and the stored toggle."""
    if os.environ.get(ENV_SKIP, "") not in ("", "0") or not is_enabled():
        return None
    return _check(False)[1]


def check_now() -> tuple[str, Optional[UpdateInfo]]:
    """Menu path: explicit user action, bypasses cache and toggle."""
    return _check(True)


def _failure_detail(e: subprocess.CalledProcessError) -> str:
    return (e.stderr or e.stdout or "").strip() or str(e)


def apply_update(info: UpdateInfo) -> Optional[str]:
    """Blocking; run in a thread. Fast-forwards the checkout to origin/main
    and reinstalls into the venv only if pyproject.toml changed. Never
    discards local changes: a checkout that can't be fast-forwarded is
    reported, not forced. Returns None on success, else an error message."""
    checkout = info.checkout
    try:
        old = _git(checkout, "rev-parse", "HEAD")
        _git(checkout, "fetch", "origin", BRANCH, timeout=UPDATE_TIMEOUT)
        _git(checkout, "merge", "--ff-only", "FETCH_HEAD", timeout=UPDATE_TIMEOUT)
        new = _git(checkout, "rev-parse", "HEAD")
        deps_changed = bool(
            _git(checkout, "diff", "--name-only", old, new, "--", "pyproject.toml")
        )
    except subprocess.CalledProcessError as e:
        return (
            f"{_failure_detail(e)}\n\n"
            "If you edited files in the install, discard those edits and retry with:\n"
            f"  {info.fallback_command}"
        )
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)

    if deps_changed:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-e", str(checkout)],
                capture_output=True,
                text=True,
                timeout=PIP_TIMEOUT,
                check=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as e:
            return (
                "The code was updated, but installing its dependencies failed:\n\n"
                f"{_failure_detail(e)}\n\nRe-run install.sh to repair the install."
            )
        except (OSError, subprocess.SubprocessError) as e:
            return (
                "The code was updated, but installing its dependencies failed:\n\n"
                f"{e}\n\nRe-run install.sh to repair the install."
            )

    state = _read_state()
    state.update(checked_at=time.time(), remote=new)
    _write_state(state)
    return None
