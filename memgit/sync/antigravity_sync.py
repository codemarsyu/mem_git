"""Tier-2 guarded synchronization between `agy` CLI (`~/.gemini/antigravity-cli/`) and
Antigravity Desktop App (`~/.gemini/antigravity/`) with WAL merge, schema validation,
protobuf rewriting, and optional atomic backup integration.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from memgit.backup import BackupTransaction
from memgit.sync.protobuf_wire import (
    pb_patch_raw_summary_standalone,
    pb_patch_trajectory_metadata_standalone,
    sync_agyhub_summaries_proto,
)


def backup_sqlite_if_nonempty(src_db: Path, dest_db: Path) -> int:
    """Safely copy a WAL-mode SQLite trajectory DB to `dest_db` via `sqlite3.backup()`
    (merging any pending `-wal` frames) ONLY if its `steps` table has > 0 rows.
    Returns the step count copied (or 0 if missing/empty/unreadable).
    """
    if not src_db.exists():
        return 0
    try:
        src_conn = sqlite3.connect(str(src_db), timeout=5.0)
    except sqlite3.Error:
        return 0
    try:
        row = src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='steps'"
        ).fetchone()
        if not row:
            return 0
        step_count = int(src_conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0])
        if step_count <= 0:
            return 0
        dest_db.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest_db.with_suffix(".db.tmp")
        if tmp_dest.exists():
            tmp_dest.unlink()
        dst_conn = sqlite3.connect(str(tmp_dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        tmp_dest.replace(dest_db)
        return step_count
    except sqlite3.Error:
        return 0
    finally:
        src_conn.close()


def patch_trajectory_db_standalone(
    db_path: Path,
    conv_id: Optional[str] = None,
    set_app_source: bool = False,
) -> Optional[str]:
    """Patch a trajectory SQLite DB's `trajectory_metadata_blob` (and optionally
    `trajectory_meta.source`) so Antigravity Desktop App never re-classifies a standalone
    conversation back into `default-cli-project` ("CLI Project").
    Returns an optional warning string on schema mismatch/error (never raises).
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            has_blob = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob'"
            ).fetchone()
            if has_blob:
                rows = conn.execute("SELECT id, data FROM trajectory_metadata_blob").fetchall()
                if rows:
                    for row_id, blob in rows:
                        blob_bytes = bytes(blob) if isinstance(blob, (bytes, bytearray)) else b""
                        patched = pb_patch_trajectory_metadata_standalone(blob_bytes, conv_id=conv_id)
                        if patched != blob_bytes:
                            conn.execute(
                                "UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?",
                                (patched, row_id),
                            )
                else:
                    patched = pb_patch_trajectory_metadata_standalone(b"", conv_id=conv_id)
                    conn.execute(
                        "INSERT OR REPLACE INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
                        (patched,),
                    )
            if set_app_source:
                has_meta = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_meta'"
                ).fetchone()
                if has_meta:
                    conn.execute(
                        "UPDATE trajectory_meta SET source = 21 WHERE trajectory_type = 4 AND source = 17"
                    )
            conn.commit()
            return None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"Tier-2 trajectory DB patch skipped for {db_path.name}: {exc}"


def _summary_table_has_columns(conn: sqlite3.Connection, required: Tuple[str, ...]) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()}
    return all(col in cols for col in required)


def sync_antigravity_cli_session_to_app(
    home_resolved: Path,
    before_convs: Dict[str, float],
    conversation_title: Optional[str] = None,
    ws_resolved: Optional[Path] = None,
    create_project: bool = False,
    default_cli_proj_existed_before: bool = True,
    backup_tx: Optional[BackupTransaction] = None,
    warnings_out: Optional[List[str]] = None,
) -> Tuple[Optional[str], List[str], int, Optional[str]]:
    """Copy the newly created `agy` CLI conversation trajectory (`.db` merged cleanly from WAL,
    or `.pb` fallback), `brain/<cid>`, `annotations/<cid>.pbtxt`, and summary metadata from
    `~/.gemini/antigravity-cli/` into `~/.gemini/antigravity/`.

    Guarded by Tier-2 schema checks and optional `BackupTransaction` snapshots so schema drift
    degrades cleanly to Tier-1 artifacts without corrupting Desktop App state.
    """
    cli_conv_dir = home_resolved / ".gemini" / "antigravity-cli" / "conversations"
    app_conv_dir = home_resolved / ".gemini" / "antigravity" / "conversations"
    cli_brain_dir = home_resolved / ".gemini" / "antigravity-cli" / "brain"
    app_brain_dir = home_resolved / ".gemini" / "antigravity" / "brain"
    app_ann_dir = home_resolved / ".gemini" / "antigravity" / "annotations"
    if not cli_conv_dir.exists():
        return None, [], 0, None

    candidates: Dict[str, float] = {}
    for pattern in ("*.db", "*.pb"):
        for fpath in cli_conv_dir.glob(pattern):
            mtime = fpath.stat().st_mtime
            prev = before_convs.get(fpath.name)
            if prev is None or mtime > prev:
                cid = fpath.stem
                if cid not in candidates or mtime > candidates[cid]:
                    candidates[cid] = mtime
    if not candidates:
        return None, [], 0, None

    conv_id = max(candidates.items(), key=lambda item: item[1])[0]
    synced: List[str] = []
    app_conv_dir.mkdir(parents=True, exist_ok=True)

    src_db = cli_conv_dir / f"{conv_id}.db"
    src_pb = cli_conv_dir / f"{conv_id}.pb"
    dest_db = app_conv_dir / f"{conv_id}.db"
    dest_pb = app_conv_dir / f"{conv_id}.pb"

    if backup_tx is not None:
        backup_tx.snapshot(dest_db)
        backup_tx.snapshot(dest_pb)

    synced_steps = backup_sqlite_if_nonempty(src_db, dest_db)
    if synced_steps > 0:
        if not create_project:
            w1 = patch_trajectory_db_standalone(dest_db, conv_id=conv_id, set_app_source=True)
            w2 = patch_trajectory_db_standalone(src_db, conv_id=conv_id, set_app_source=False)
            if warnings_out is not None:
                for w in (w1, w2):
                    if w:
                        warnings_out.append(w)
        synced.append(str(dest_db))
    elif src_pb.exists() and src_pb.stat().st_size > 0:
        if dest_db.exists():
            dest_db.unlink()
        shutil.copy2(src_pb, dest_pb)
        synced.append(str(dest_pb))

    src_brain = cli_brain_dir / conv_id
    dest_brain = app_brain_dir / conv_id
    if src_brain.exists() and src_brain.is_dir():
        if backup_tx is not None:
            backup_tx.snapshot(dest_brain)
        shutil.copytree(src_brain, dest_brain, dirs_exist_ok=True)
        synced.append(str(dest_brain))

    if conversation_title:
        app_ann_dir.mkdir(parents=True, exist_ok=True)
        escaped_title = conversation_title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        now_sec = int(time.time())
        ann_path = app_ann_dir / f"{conv_id}.pbtxt"
        if backup_tx is not None:
            backup_tx.snapshot(ann_path)
        ann_path.write_text(
            f'title:"{escaped_title}"  last_user_view_time:{{seconds:{now_sec}  nanos:0}}',
            encoding="utf-8",
        )
        synced.append(str(ann_path))

    project_id: Optional[str] = None
    workspace_uris: Optional[str] = None
    cli_summary_db = home_resolved / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
    if create_project:
        if cli_summary_db.exists():
            try:
                cconn = sqlite3.connect(str(cli_summary_db), timeout=5.0)
                try:
                    if _summary_table_has_columns(cconn, ("conversation_id", "project_id", "workspace_uris")):
                        crow = cconn.execute(
                            "SELECT project_id, workspace_uris FROM conversation_summaries WHERE conversation_id = ?",
                            (conv_id,),
                        ).fetchone()
                        if crow:
                            project_id = crow[0] or None
                            workspace_uris = crow[1] or None
                finally:
                    cconn.close()
            except sqlite3.Error as exc:
                if warnings_out is not None:
                    warnings_out.append(f"Tier-2 CLI summary read skipped: {exc}")
        if not workspace_uris and ws_resolved is not None:
            workspace_uris = json.dumps([f"file://{ws_resolved}"])
    else:
        project_id = "outside-of-project"
        workspace_uris = ""

    app_summary_db = home_resolved / ".gemini" / "antigravity" / "conversation_summaries.db"
    agyhub_proto = home_resolved / ".gemini" / "antigravity" / "agyhub_summaries_proto.pb"
    summary_targets = [
        (app_summary_db, "antigravity", True),
    ]
    if not create_project:
        summary_targets.append((cli_summary_db, "antigravity-cli", False))

    if backup_tx is not None:
        if app_summary_db.exists():
            backup_tx.snapshot(app_summary_db)
        if agyhub_proto.exists():
            backup_tx.snapshot(agyhub_proto)

    required_cols = ("conversation_id", "title", "project_id", "workspace_uris", "source", "app_data_dir")

    def _apply_summary_sync() -> Optional[bytes]:
        latest_patched_raw: Optional[bytes] = None
        for summary_db, data_dir_name, allow_insert in summary_targets:
            if not summary_db.exists():
                continue
            try:
                sconn = sqlite3.connect(str(summary_db), timeout=5.0)
                try:
                    if not _summary_table_has_columns(sconn, required_cols):
                        if warnings_out is not None:
                            warnings_out.append(
                                f"Tier-2 schema mismatch in {summary_db}; skipping summary DB update (Tier-1 files preserved)."
                            )
                        continue
                    has_raw = _summary_table_has_columns(sconn, ("raw_summary",))
                    select_cols = (
                        "title, project_id, workspace_uris, raw_summary"
                        if has_raw
                        else "title, project_id, workspace_uris"
                    )
                    existing = sconn.execute(
                        f"SELECT {select_cols} FROM conversation_summaries WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()
                    if existing is not None:
                        new_title = conversation_title or existing[0] or ""
                        new_proj = project_id if project_id is not None else (existing[1] or "outside-of-project")
                        new_ws = workspace_uris if workspace_uris is not None else (existing[2] or "")
                        raw_blob = existing[3] if has_raw else None
                        if isinstance(raw_blob, (bytes, bytearray)) and not create_project:
                            patched = pb_patch_raw_summary_standalone(
                                bytes(raw_blob), title=new_title, conv_id=conv_id
                            )
                            if data_dir_name == "antigravity":
                                latest_patched_raw = patched
                            elif latest_patched_raw is None:
                                latest_patched_raw = patched
                            sconn.execute(
                                "UPDATE conversation_summaries "
                                "SET title = ?, project_id = ?, workspace_uris = ?, source = '', "
                                "app_data_dir = ?, raw_summary = ? "
                                "WHERE conversation_id = ?",
                                (new_title, new_proj, new_ws, data_dir_name, patched, conv_id),
                            )
                        else:
                            sconn.execute(
                                "UPDATE conversation_summaries "
                                "SET title = ?, project_id = ?, workspace_uris = ?, source = '', app_data_dir = ? "
                                "WHERE conversation_id = ?",
                                (new_title, new_proj, new_ws, data_dir_name, conv_id),
                            )
                    elif allow_insert:
                        now_sql = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
                        sconn.execute(
                            "INSERT OR REPLACE INTO conversation_summaries "
                            "(conversation_id, title, step_count, last_modified_time, workspace_uris, "
                            "status, source, project_id, last_user_input_time, last_user_input_step_index, app_data_dir) "
                            "VALUES (?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', '', ?, ?, 0, 'antigravity')",
                            (
                                conv_id,
                                conversation_title or "",
                                synced_steps,
                                now_sql,
                                workspace_uris or "",
                                project_id or "outside-of-project",
                                now_sql,
                            ),
                        )
                    sconn.commit()
                finally:
                    sconn.close()
            except sqlite3.Error as exc:
                if warnings_out is not None:
                    warnings_out.append(f"Tier-2 summary sync warning ({summary_db.name}): {exc}")
        return latest_patched_raw

    patched_raw = _apply_summary_sync()
    if app_summary_db.exists():
        for wait_s in (1.0, 2.0):
            time.sleep(wait_s)
            patched_raw_next = _apply_summary_sync()
            if patched_raw_next is not None:
                patched_raw = patched_raw_next

    if patched_raw is not None and agyhub_proto.exists():
        if sync_agyhub_summaries_proto(
            agyhub_proto,
            conv_id=conv_id,
            raw_summary=patched_raw,
            strip_cli_marker=not create_project,
        ):
            synced.append(str(agyhub_proto))

    if not create_project:
        default_cli_proj = home_resolved / ".gemini" / "config" / "projects" / "default-cli-project.json"
        if default_cli_proj.exists():
            in_use = False
            if app_summary_db.exists():
                try:
                    cconn = sqlite3.connect(str(app_summary_db), timeout=5.0)
                    try:
                        if _summary_table_has_columns(cconn, ("project_id",)):
                            cnt = cconn.execute(
                                "SELECT COUNT(*) FROM conversation_summaries WHERE project_id = 'default-cli-project'"
                            ).fetchone()[0]
                            in_use = int(cnt) > 0
                    finally:
                        cconn.close()
                except sqlite3.Error:
                    in_use = default_cli_proj_existed_before
            if not in_use:
                try:
                    if backup_tx is not None:
                        backup_tx.snapshot(default_cli_proj)
                    default_cli_proj.unlink()
                except OSError:
                    pass

    if create_project and project_id and project_id != "outside-of-project":
        proj_json = home_resolved / ".gemini" / "config" / "projects" / f"{project_id}.json"
        if proj_json.exists():
            try:
                os.utime(proj_json, None)
            except OSError:
                pass

    return conv_id, synced, synced_steps, project_id
