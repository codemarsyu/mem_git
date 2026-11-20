"""Atomic Pre-Write Backup & Rollback (`memgit undo`) for safe Tier-1 / Tier-2 agent mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional
import uuid

from memgit.paths import memgit_backups_dir


@dataclass
class BackupEntry:
    target_path: str
    existed: bool
    is_dir: bool = False
    backup_relpath: Optional[str] = None


@dataclass
class BackupTransaction:
    """Tracks all files touched by a single MemGit mutation so it can be rolled back cleanly."""
    op_id: str
    operation: str
    backup_dir: Path
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    entries: List[BackupEntry] = field(default_factory=list)
    _seen_paths: Dict[str, int] = field(default_factory=dict, repr=False)

    def snapshot(self, path: Path) -> None:
        """Record the pre-mutation state of `path` (idempotent per transaction)."""
        resolved = str(path.resolve())
        if resolved in self._seen_paths:
            return
        idx = len(self.entries)
        self._seen_paths[resolved] = idx
        files_dir = self.backup_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        if path.exists():
            is_dir = path.is_dir()
            rel = f"files/{idx}.bak"
            dest = self.backup_dir / rel
            if is_dir:
                shutil.copytree(path, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(path, dest)
            self.entries.append(
                BackupEntry(
                    target_path=resolved,
                    existed=True,
                    is_dir=is_dir,
                    backup_relpath=rel,
                )
            )
        else:
            self.entries.append(
                BackupEntry(
                    target_path=resolved,
                    existed=False,
                    is_dir=False,
                    backup_relpath=None,
                )
            )

    def commit(self) -> Path:
        """Persist `manifest.json` atomically into the backup directory."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "op_id": self.op_id,
            "operation": self.operation,
            "created_at": self.created_at,
            "reverted": False,
            "metadata": self.metadata,
            "entries": [
                {
                    "target_path": e.target_path,
                    "existed": e.existed,
                    "is_dir": e.is_dir,
                    "backup_relpath": e.backup_relpath,
                }
                for e in self.entries
            ],
        }
        manifest_path = self.backup_dir / "manifest.json"
        tmp_path = self.backup_dir / "manifest.json.tmp"
        tmp_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp_path.replace(manifest_path)
        return manifest_path


class BackupManager:
    """Manages `~/.memgit/backups/<op_id>/` snapshots and `memgit undo` restoration."""

    def __init__(self, user_home: Path) -> None:
        self.root = memgit_backups_dir(user_home.resolve())

    def begin(self, operation: str, metadata: Optional[Dict[str, Any]] = None) -> BackupTransaction:
        now = datetime.now(timezone.utc)
        op_id = f"op_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        backup_dir = self.root / op_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        return BackupTransaction(
            op_id=op_id,
            operation=operation,
            backup_dir=backup_dir,
            created_at=now.isoformat(),
            metadata=metadata or {},
        )

    def list_backups(self, include_reverted: bool = True) -> List[Dict[str, Any]]:
        if not self.root.exists():
            return []
        manifests: List[Dict[str, Any]] = []
        for entry in self.root.iterdir():
            mpath = entry / "manifest.json"
            if not mpath.is_file():
                continue
            try:
                data = json.loads(mpath.read_text(encoding="utf-8"))
                if not include_reverted and data.get("reverted"):
                    continue
                manifests.append(data)
            except (OSError, json.JSONDecodeError):
                continue
        manifests.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return manifests

    def undo(self, op_id: Optional[str] = None) -> Dict[str, Any]:
        """Restore files from a backup operation (defaults to most recent non-reverted op)."""
        if op_id:
            mpath = self.root / op_id / "manifest.json"
            if not mpath.exists():
                raise FileNotFoundError(f"Backup operation '{op_id}' not found under {self.root}")
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        else:
            candidates = self.list_backups(include_reverted=False)
            if not candidates:
                raise FileNotFoundError(f"No pending backups found under {self.root} to undo.")
            manifest = candidates[0]
            op_id = manifest["op_id"]
            mpath = self.root / op_id / "manifest.json"

        backup_dir = self.root / op_id
        restored: List[str] = []
        removed: List[str] = []
        errors: List[str] = []

        # Replay entries in reverse order so newly created child files are cleaned before parents
        for item in reversed(manifest.get("entries", [])):
            target = Path(item["target_path"])
            existed = bool(item.get("existed"))
            is_dir = bool(item.get("is_dir"))
            rel = item.get("backup_relpath")
            try:
                if existed and rel:
                    src = backup_dir / rel
                    if not src.exists():
                        errors.append(f"Backup payload missing for {target}")
                        continue
                    if is_dir:
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(src, target)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        tmp = target.with_name(f".{target.name}.memgit_restore.tmp")
                        shutil.copy2(src, tmp)
                        tmp.replace(target)
                    restored.append(str(target))
                else:
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                        removed.append(str(target))
            except OSError as exc:
                errors.append(f"{target}: {exc}")

        manifest["reverted"] = True
        manifest["reverted_at"] = datetime.now(timezone.utc).isoformat()
        tmp_m = backup_dir / "manifest.json.tmp"
        tmp_m.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp_m.replace(mpath)

        return {
            "op_id": op_id,
            "operation": manifest.get("operation"),
            "restored_files": restored,
            "removed_files": removed,
            "errors": errors,
        }
