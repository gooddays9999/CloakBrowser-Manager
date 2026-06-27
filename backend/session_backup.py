"""Local profile session backup helpers.

Backs up and restores only the WhatsApp login state — the LevelDB stores under
the profile's ``Default/`` directory — never the regenerable browser cache.

Why this exists: the WhatsApp bridge uses LocalAuth, so the only copy of a
profile's logged-in session lives in its on-disk IndexedDB (a LevelDB, which is
easily corrupted by hard kills / OOM / I/O errors). A corrupt store forces a QR
re-scan even though the WhatsApp server still trusts the linked device. Keeping
one known-good snapshot lets us restore and reconnect without re-scanning.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import database as db

logger = logging.getLogger("cloakbrowser.manager.backup")

BACKUP_ROOT = Path(os.environ.get("SESSION_BACKUP_DIR") or db.DATA_DIR / "session_backups")

# Directories under the profile's ``Default/`` that hold the WhatsApp login
# state. This is an allow-list: only these are backed up / restored. Everything
# else (caches, cookies, preferences, …) is regenerable and left untouched.
SESSION_DIR_NAMES: tuple[str, ...] = ("IndexedDB", "Local Storage", "Service Worker")

# The ``Default/IndexedDB`` store is the irreplaceable core; without it there is
# no session to back up.
CORE_SESSION_DIR = "IndexedDB"

# Regenerable cache that lives *inside* a backed-up session dir. Skipped to keep
# backups lean (CacheStorage holds the WhatsApp web-app assets, not login state).
SESSION_DIR_EXCLUDES: dict[str, set[str]] = {
    "Service Worker": {"CacheStorage"},
}


class NoSessionDataError(Exception):
    """Raised when a profile has no WhatsApp login state worth backing up."""


class NoBackupError(Exception):
    """Raised when no session backup exists to restore."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _has_files(path: Path) -> bool:
    """True if ``path`` is a directory containing at least one regular file."""
    if not path.is_dir():
        return False
    return any(item.is_file() for item in path.rglob("*"))


def _make_root_ignore(root: Path, excludes: set[str]) -> Callable[[str, list[str]], set[str]]:
    """Ignore the named direct children of ``root`` only (not deeper matches)."""

    def _ignore(src: str, names: list[str]) -> set[str]:
        if Path(src) == root:
            return {name for name in names if name in excludes}
        return set()

    return _ignore


def _cleanup_stale(profile_backup_dir: Path) -> list[str]:
    removed: list[str] = []
    if not profile_backup_dir.exists():
        return removed
    for child in profile_backup_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(("last_good.tmp-", "last_good.old-")):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as exc:
                logger.warning("Failed to remove stale backup dir %s: %s", child, exc)
    return removed


def _cleanup_restore_stale(live_default: Path) -> list[str]:
    """Remove leftover temp dirs from a prior interrupted restore."""
    removed: list[str] = []
    if not live_default.exists():
        return removed
    for child in live_default.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(".restore-tmp-"):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as exc:
                logger.warning("Failed to remove stale restore dir %s: %s", child, exc)
    return removed


def backup_profile_session(profile: dict[str, Any]) -> dict[str, Any]:
    """Create or replace the single last-good backup for a stopped profile.

    Only the WhatsApp login dirs under ``Default/`` are copied. Refuses to make
    an empty backup when the profile has never logged in (no IndexedDB), since
    restoring an empty backup would still force a QR re-scan.
    """
    profile_id = str(profile["id"])
    profile_root = Path(str(profile["user_data_dir"]))
    default_dir = profile_root / "Default"

    if not _has_files(default_dir / CORE_SESSION_DIR):
        raise NoSessionDataError(
            "Profile has no WhatsApp session to back up "
            f"({CORE_SESSION_DIR} missing or empty under {default_dir})"
        )

    profile_backup_dir = BACKUP_ROOT / profile_id
    profile_backup_dir.mkdir(parents=True, exist_ok=True)
    removed_stale = _cleanup_stale(profile_backup_dir)

    tmp = profile_backup_dir / f"last_good.tmp-{uuid.uuid4().hex}"
    old = profile_backup_dir / f"last_good.old-{uuid.uuid4().hex}"
    last_good = profile_backup_dir / "last_good"
    created_at = _now()

    try:
        tmp_default = tmp / "Default"
        tmp_default.mkdir(parents=True)
        backed_up: list[str] = []
        for name in SESSION_DIR_NAMES:
            src = default_dir / name
            if not src.is_dir():
                continue
            excludes = SESSION_DIR_EXCLUDES.get(name, set())
            shutil.copytree(
                src,
                tmp_default / name,
                symlinks=True,
                ignore=_make_root_ignore(src, excludes),
            )
            backed_up.append(name)

        metadata = {
            "profile_id": profile_id,
            "profile_name": profile.get("name"),
            "source": str(default_dir),
            "created_at": created_at,
            "mode": "login_state_whitelist_single_last_good",
            "session_dirs": list(SESSION_DIR_NAMES),
            "backed_up": backed_up,
            "session_dir_excludes": {k: sorted(v) for k, v in SESSION_DIR_EXCLUDES.items()},
        }
        (tmp / "backup_meta.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        replaced = last_good.exists()
        if replaced:
            last_good.rename(old)
        try:
            tmp.rename(last_good)
        except Exception:
            if old.exists() and not last_good.exists():
                old.rename(last_good)
            raise

        if old.exists():
            try:
                shutil.rmtree(old)
            except OSError as exc:
                logger.warning("Failed to delete old backup dir %s: %s", old, exc)

        size_bytes = _dir_size_bytes(last_good)
        return {
            "ok": True,
            "profile_id": profile_id,
            "backup_path": str(last_good),
            "created_at": created_at,
            "size_bytes": size_bytes,
            "replaced": replaced,
            "backed_up": backed_up,
            "removed_stale": removed_stale,
        }
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise


def restore_profile_session(profile: dict[str, Any]) -> dict[str, Any]:
    """Overlay the last-good login dirs back onto a stopped profile.

    Only the backed-up login dirs are replaced inside the live ``Default/``;
    every other file in the profile is preserved. Each live dir being replaced
    is moved aside to ``<profile>/.corrupt/<name>`` first, so a corrupt store
    can be inspected after the fact instead of being lost.
    """
    profile_id = str(profile["id"])
    profile_root = Path(str(profile["user_data_dir"]))
    backup = BACKUP_ROOT / profile_id / "last_good"
    backup_default = backup / "Default"
    if not backup.is_dir() or not backup_default.is_dir():
        raise NoBackupError("Session backup not found")

    live_default = profile_root / "Default"
    live_default.mkdir(parents=True, exist_ok=True)
    removed_stale = _cleanup_restore_stale(live_default)

    corrupt_root = profile_root / ".corrupt"
    restored_at = _now()
    restored: list[str] = []
    replaced = False

    session_dirs = sorted(child.name for child in backup_default.iterdir() if child.is_dir())
    for name in session_dirs:
        src = backup_default / name
        dst = live_default / name
        tmp = live_default / f".restore-tmp-{uuid.uuid4().hex}-{name}"
        try:
            shutil.copytree(src, tmp, symlinks=True)
            if dst.exists():
                replaced = True
                corrupt_root.mkdir(parents=True, exist_ok=True)
                aside = corrupt_root / name
                if aside.exists():
                    shutil.rmtree(aside, ignore_errors=True)
                dst.rename(aside)
            tmp.rename(dst)
            restored.append(name)
        except Exception:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            raise

    size_bytes = _dir_size_bytes(backup)
    return {
        "ok": True,
        "profile_id": profile_id,
        "backup_path": str(backup),
        "restored_path": str(live_default),
        "restored_at": restored_at,
        "size_bytes": size_bytes,
        "replaced": replaced,
        "restored": restored,
        "removed_stale": removed_stale,
    }
