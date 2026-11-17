"""Server-side Versioned Storage Layer (SQLite + Content-Addressed Object Store)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from memgit.compiler import CompiledMemoryPackage, compile_checkpoint
from memgit.merge import (
    SemanticMergeReport,
    generate_cognitive_blame,
    semantic_merge_checkpoints,
)
from memgit.models import HostFingerprint, MemoryCheckpointIR


class ServerStorage:
    """Persistent server-side repository store for Memory IR checkpoints, branches, and Provenance DAG."""

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self.objects_dir = storage_dir / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = storage_dir / "memgit_server.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    repo_name TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    author TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    source_model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    object_path TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS branch_refs (
                    repo_name TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    head_checkpoint_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repo_name, branch)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_checkpoint(self, chk: MemoryCheckpointIR) -> Dict[str, Any]:
        """Persist a MemoryCheckpointIR object and advance the branch HEAD pointer."""
        obj_file = self.objects_dir / f"{chk.checkpoint_id}.json"
        obj_file.write_text(chk.to_json(indent=2), encoding="utf-8")

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints (
                    checkpoint_id, repo_name, branch, parent_checkpoint_id,
                    author, source_agent, source_model, created_at, message, object_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chk.checkpoint_id,
                    chk.repo_name,
                    chk.branch,
                    chk.parent_checkpoint_id,
                    chk.author,
                    chk.source_agent,
                    chk.source_model,
                    chk.created_at,
                    chk.message,
                    str(obj_file),
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO branch_refs (
                    repo_name, branch, head_checkpoint_id, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (chk.repo_name, chk.branch, chk.checkpoint_id, chk.created_at),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "status": "saved",
            "checkpoint_id": chk.checkpoint_id,
            "repo_name": chk.repo_name,
            "branch": chk.branch,
            "facts_count": len(chk.confirmed_facts),
            "git_wip_dirty": chk.git_wip.is_dirty,
        }

    def get_checkpoint(
        self,
        repo_name: str,
        ref: str = "main",
    ) -> MemoryCheckpointIR:
        """Load a checkpoint by explicit checkpoint_id (`chk_...`) or branch name (`main`)."""
        conn = self._connect()
        try:
            if ref.startswith("chk_"):
                cur = conn.execute(
                    "SELECT object_path FROM checkpoints WHERE repo_name = ? AND checkpoint_id = ?",
                    (repo_name, ref),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT c.object_path
                    FROM branch_refs r
                    JOIN checkpoints c ON r.head_checkpoint_id = c.checkpoint_id
                    WHERE r.repo_name = ? AND r.branch = ?
                    """,
                    (repo_name, ref),
                )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Reference '{ref}' not found in repository '{repo_name}'")
            obj_path = Path(row[0])
            data = json.loads(obj_path.read_text(encoding="utf-8"))
            return MemoryCheckpointIR.from_dict(data)
        finally:
            conn.close()

    def list_checkpoints(self, repo_name: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT checkpoint_id, branch, parent_checkpoint_id, author,
                       source_agent, source_model, created_at, message
                FROM checkpoints
                WHERE repo_name = ?
                ORDER BY created_at DESC
                """,
                (repo_name,),
            )
            rows = cur.fetchall()
            return [
                {
                    "checkpoint_id": r[0],
                    "branch": r[1],
                    "parent_checkpoint_id": r[2],
                    "author": r[3],
                    "source_agent": r[4],
                    "source_model": r[5],
                    "created_at": r[6],
                    "message": r[7],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def transform_for_destination(
        self,
        repo_name: str,
        ref: str,
        target_agent: str,
        target_model: str,
        target_host: HostFingerprint,
        include_private: bool = True,
    ) -> Tuple[MemoryCheckpointIR, CompiledMemoryPackage]:
        """Server-side transformation engine: loads checkpoint IR and compiles it
        specifically for the destination node's agent harness, LLM model, and OS/paths.
        """
        chk = self.get_checkpoint(repo_name, ref)
        pkg = compile_checkpoint(
            raw_checkpoint=chk,
            target_agent=target_agent,
            target_model=target_model,
            target_host=target_host,
            include_private=include_private,
        )
        return chk, pkg

    def merge_refs(
        self,
        repo_name: str,
        base_ref: str,
        incoming_ref: str,
        author: str = "team-merger",
        target_branch: str = "main",
        strip_private: bool = True,
    ) -> Tuple[MemoryCheckpointIR, SemanticMergeReport]:
        """Run server-side Provenance DAG semantic merge and save the resulting commit."""
        base_chk = self.get_checkpoint(repo_name, base_ref)
        incoming_chk = self.get_checkpoint(repo_name, incoming_ref)
        merged_chk, report = semantic_merge_checkpoints(
            base_chk=base_chk,
            incoming_chk=incoming_chk,
            author=author,
            target_branch=target_branch,
            strip_private=strip_private,
        )
        self.save_checkpoint(merged_chk)
        return merged_chk, report

    def blame(self, repo_name: str, ref: str = "main") -> List[Dict[str, Any]]:
        chk = self.get_checkpoint(repo_name, ref)
        return generate_cognitive_blame(chk)
