"""Cursor (Agent) Memory Adapter (.cursor/rules/*.mdc + SQLite state.vscdb)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import List, Optional

from memgit.adapters.base import AgentAdapter, AgentBootSimulationReport, AgentSessionSummary
from memgit.paths import cursor_storage_dir
from memgit.compiler import CompiledMemoryPackage
from memgit.git_wip import capture_git_wip_bundle
from memgit.models import (
    AgentType,
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    ProvenanceNode,
    SanitizationStats,
    utc_now_iso,
)
from memgit.sanitizer import normalize_paths_to_ir


class CursorAdapter(AgentAdapter):
    agent_type = AgentType.CURSOR.value

    def list_sessions(
        self,
        user_home: Path,
        workspace_root: Optional[Path] = None,
    ) -> List[AgentSessionSummary]:
        summaries: List[AgentSessionSummary] = []
        home = user_home.resolve()
        db_candidates = [
            home / ".cursor" / "User" / "workspaceStorage" / "state.vscdb",
            cursor_storage_dir(home) / "state.vscdb",
        ]
        for db_path in db_candidates:
            if not db_path.exists():
                continue
            try:
                conn = sqlite3.connect(str(db_path), timeout=3.0)
                try:
                    has_tbl = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='ItemTable'"
                    ).fetchone()
                    if not has_tbl:
                        continue
                    rows = conn.execute(
                        "SELECT key FROM ItemTable WHERE key LIKE 'memgit.%' OR key LIKE '%composer%' LIMIT 20"
                    ).fetchall()
                    ts = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc).isoformat()
                    for (k,) in rows:
                        summaries.append(
                            AgentSessionSummary(
                                session_id=str(k),
                                agent_type=self.agent_type,
                                title=str(k),
                                updated_at=ts,
                                turn_count=0,
                                workspace_path=str(workspace_root) if workspace_root else "",
                                source_path=str(db_path),
                            )
                        )
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
        return summaries

    def install_compiled_package(
        self,
        pkg: CompiledMemoryPackage,
        workspace_root: Path,
        user_home: Path,
    ) -> List[str]:
        written = super().install_compiled_package(pkg, workspace_root, user_home)
        if pkg.sqlite_memories:
            db_dir = user_home / ".cursor" / "User" / "workspaceStorage"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)"
                )
                for row in pkg.sqlite_memories:
                    conn.execute(
                        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                        (row["key"], row["value"]),
                    )
                conn.commit()
            finally:
                conn.close()
            written.append(str(db_path))
        return written

    def extract_checkpoint(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        author: str,
        source_model: str,
        host_fingerprint: HostFingerprint,
        message: str = "Checkpoint from Cursor",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        stats = SanitizationStats()
        confirmed_facts: List[ProvenanceNode] = []
        now = utc_now_iso()

        db_path = user_home / ".cursor" / "User" / "workspaceStorage" / "state.vscdb"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.execute(
                    "SELECT value FROM ItemTable WHERE key LIKE 'memgit.fact.%'"
                )
                for (val_str,) in cur.fetchall():
                    data = json.loads(val_str)
                    stmt, p_cnt = normalize_paths_to_ir(data["statement"], host_fingerprint)
                    stats.normalized_host_paths += p_cnt
                    data["statement"] = stmt
                    confirmed_facts.append(ProvenanceNode.from_dict(data))
            finally:
                conn.close()

        git_wip = capture_git_wip_bundle(workspace_root)
        chk_id = MemoryCheckpointIR.generate_id(
            repo_name, author, message, now, parent_checkpoint_id
        )
        return MemoryCheckpointIR(
            checkpoint_id=chk_id,
            repo_name=repo_name,
            branch=branch,
            parent_checkpoint_id=parent_checkpoint_id,
            author=author,
            source_agent=self.agent_type,
            source_model=source_model,
            source_host=host_fingerprint,
            created_at=now,
            message=message,
            confirmed_facts=confirmed_facts,
            execution_stack=ExecutionStack(active_goal="Continue task from Cursor"),
            git_wip=git_wip,
            sanitization_stats=stats,
        )

    def simulate_boot_and_load(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        model_name: str = "cursor-claude-sonnet",
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        report = AgentBootSimulationReport(
            agent_type=self.agent_type,
            model_name=model_name,
            workspace_root=str(workspace_root),
            user_home=str(user_home),
        )
        chunks: List[str] = []
        mdc_dir = workspace_root / ".cursor" / "rules"
        if mdc_dir.exists():
            for mdc in sorted(mdc_dir.glob("*.mdc")):
                report.loaded_files.append(str(mdc))
                chunks.append(mdc.read_text(encoding="utf-8"))

        db_path = user_home / ".cursor" / "User" / "workspaceStorage" / "state.vscdb"
        if db_path.exists():
            report.loaded_files.append(str(db_path))
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'memgit.fact.%'")
                for k, v in cur.fetchall():
                    chunks.append(f"{k}: {v}")
            finally:
                conn.close()

        full_context = "\n\n".join(chunks)
        report.context_window_bytes = len(full_context.encode("utf-8"))
        report.memory_index_lines = len(full_context.splitlines())
        report.assembled_system_context_preview = full_context[:600]

        for bad_path in forbidden_source_paths or []:
            if bad_path and bad_path in full_context:
                report.source_path_leaks += full_context.count(bad_path)

        for line in full_context.splitlines():
            if line.startswith("- `") and ("(verified_pass)" in line or "(unverified)" in line):
                report.active_constraints_loaded.append(line.strip())
            elif "[LOCKED DEFERRAL" in line:
                report.user_deferrals_enforced.append(line.strip())
            m_step = re.search(r"\*\*\[(PARTIALLY_DONE|NOT_DONE)\]\*\*\s*`([^`]+)`:\s*(.+)", line)
            if m_step and report.resumed_step_id is None:
                report.resumed_step_id = m_step.group(2)
                report.resumed_step_title = m_step.group(3).strip()

        return report
