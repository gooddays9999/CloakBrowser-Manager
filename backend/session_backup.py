"""Local profile session backup helpers."""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import database as db

logger = logging.getLogger("cloakbrowser.manager.backup")

BACKUP_ROOT = Path(os.environ.get("SESSION_BACKUP_DIR") or db.DATA_DIR / "session_backups")

EXCLUDED_DIR_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "Crash Reports",
}

EXCLUDED_RELATIVE_DIRS = {
    ("Default", "Service Worker", "CacheStorage"),
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _should_ignore(src: str, names: list[str], profile_root: Path) -> set[str]:
    src_path = Path(src)
    ignored: set[str] = set()
    for name in names:
        path = src_path / name
        if not path.is_dir():
            continue
        if name in EXCLUDED_DIR_NAMES:
            ignored.add(name)
            continue
        try:
            relative = path.relative_to(profile_root)
        except ValueError:
            continue
        if tuple(relative.parts) in EXCLUDED_RELATIVE_DIRS:
            ignored.add(name)
    return ignored


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _cleanup_stale(profile_backup_dir: Path) -> list[str]:
    removed: list[str] = []
    if not profile_backup_dir.exists():
        return removed
    for child in profile_backup_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("last_good.tmp-") or child.name.startswith("last_good.old-"):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as exc:
                logger.warning("Failed to remove stale backup dir %s: %s", child, exc)
    return removed


def _cleanup_restore_stale(profile_dir: Path) -> list[str]:
    removed: list[str] = []
    parent = profile_dir.parent
    if not parent.exists():
        return removed
    prefixes = (f"{profile_dir.name}.restore-tmp-", f"{profile_dir.name}.restore-old-")
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith(prefixes):
            continue
        try:
            shutil.rmtree(child)
            removed.append(child.name)
        except OSError as exc:
            logger.warning("Failed to remove stale restore dir %s: %s", child, exc)
    return removed


def backup_profile_session(profile: dict[str, Any]) -> dict[str, Any]:
    """Create or replace the single last-good backup for a stopped profile."""
    profile_id = str(profile["id"])
    source = Path(str(profile["user_data_dir"]))
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"Profile data directory not found: {source}")

    profile_backup_dir = BACKUP_ROOT / profile_id
    profile_backup_dir.mkdir(parents=True, exist_ok=True)
    removed_stale = _cleanup_stale(profile_backup_dir)

    tmp = profile_backup_dir / f"last_good.tmp-{uuid.uuid4().hex}"
    old = profile_backup_dir / f"last_good.old-{uuid.uuid4().hex}"
    last_good = profile_backup_dir / "last_good"
    created_at = _now()

    try:
        shutil.copytree(
            source,
            tmp,
            symlinks=True,
            ignore=lambda src, names: _should_ignore(src, names, source),
        )
        metadata = {
            "profile_id": profile_id,
            "profile_name": profile.get("name"),
            "source": str(source),
            "created_at": created_at,
            "mode": "cache_stripped_single_last_good",
            "excluded_dir_names": sorted(EXCLUDED_DIR_NAMES),
            "excluded_relative_dirs": ["/".join(parts) for parts in sorted(EXCLUDED_RELATIVE_DIRS)],
        }
        (tmp / "backup_meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

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
            "removed_stale": removed_stale,
        }
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise


def restore_profile_session(profile: dict[str, Any]) -> dict[str, Any]:
    """Replace a stopped profile with its single last-good backup."""
    profile_id = str(profile["id"])
    target = Path(str(profile["user_data_dir"]))
    backup = BACKUP_ROOT / profile_id / "last_good"
    if not backup.exists() or not backup.is_dir():
        raise FileNotFoundError("Session backup not found")

    target.parent.mkdir(parents=True, exist_ok=True)
    removed_stale = _cleanup_restore_stale(target)

    tmp = target.parent / f"{target.name}.restore-tmp-{uuid.uuid4().hex}"
    old = target.parent / f"{target.name}.restore-old-{uuid.uuid4().hex}"
    restored_at = _now()

    try:
        shutil.copytree(backup, tmp, symlinks=True)
        replaced = target.exists()
        if replaced:
            target.rename(old)
        try:
            tmp.rename(target)
        except Exception:
            if old.exists() and not target.exists():
                old.rename(target)
            raise

        if old.exists():
            try:
                shutil.rmtree(old)
            except OSError as exc:
                logger.warning("Failed to delete old profile dir after restore %s: %s", old, exc)

        size_bytes = _dir_size_bytes(target)
        return {
            "ok": True,
            "profile_id": profile_id,
            "backup_path": str(backup),
            "restored_path": str(target),
            "restored_at": restored_at,
            "size_bytes": size_bytes,
            "replaced": replaced,
            "removed_stale": removed_stale,
        }
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise
