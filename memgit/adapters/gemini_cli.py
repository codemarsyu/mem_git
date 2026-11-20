"""Gemini CLI / Gemini App Memory Adapter and Startup Simulator.

Models Gemini CLI's distinct storage layout (separate from Google Antigravity):
1. Workspace Instruction Layer: `GEMINI.md`, `AGENTS.md`, `.agent-context/HANDOFF.md`, `.agent-context/CHAT_HISTORY.md`
2. Global Gemini Home Layer: `~/.gemini/GEMINI.md`, `~/.gemini/memories/memory_summary.md`, `~/.gemini/memories/chat_history.md`
3. Native Gemini CLI Session Store: `~/.gemini/tmp/<project_hash>/chats/session-*.json`
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import List, Optional

from memgit.adapters.base import AgentAdapter, AgentBootSimulationReport
from memgit.git_wip import capture_git_wip_bundle
from memgit.models import (
    AgentType,
    ChatTurn,
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    MemoryScope,
    NegativeMemoryItem,
    ProvenanceNode,
    SanitizationStats,
    TopicMemoryFile,
    UserDeferralLock,
    VerificationStatus,
    utc_now_iso,
)
from memgit.sanitizer import (
    classify_memory_scope,
    normalize_paths_to_ir,
    strip_thinking_blocks,
)


class GeminiCLIAdapter(AgentAdapter):
    agent_type = AgentType.GEMINI_CLI.value

    def extract_checkpoint(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        author: str,
        source_model: str,
        host_fingerprint: HostFingerprint,
        message: str = "Checkpoint from Gemini CLI",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        stats = SanitizationStats()
        confirmed_facts: List[ProvenanceNode] = []
        negative_memories: List[NegativeMemoryItem] = []
        user_deferrals: List[UserDeferralLock] = []
        topic_memories: List[TopicMemoryFile] = []
        conversation_history: List[ChatTurn] = []
        execution_stack = ExecutionStack(active_goal="Continue task from Gemini CLI")
        now = utc_now_iso()

        # 1. Extract from ~/.gemini/tmp/*/chats/session-*.json if present
        tmp_root = user_home / ".gemini" / "tmp"
        if tmp_root.exists():
            session_files = sorted(
                tmp_root.glob("*/chats/session-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if session_files:
                try:
                    raw_session = json.loads(session_files[0].read_text(encoding="utf-8"))
                    msgs = raw_session.get("messages", [])
                    for idx, msg in enumerate(msgs):
                        role_raw = str(msg.get("type", msg.get("role", ""))).lower()
                        role = "user" if role_raw in ("user", "human") else "assistant"
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = "\n".join(
                                str(c.get("text", "")) if isinstance(c, dict) else str(c)
                                for c in content
                            )
                        clean_text, s_cnt = strip_thinking_blocks(str(content))
                        stats.stripped_thinking_blocks += s_cnt
                        norm_text, p_cnt = normalize_paths_to_ir(clean_text, host_fingerprint)
                        stats.normalized_host_paths += p_cnt
                        if norm_text.strip():
                            conversation_history.append(
                                ChatTurn(
                                    turn_id=f"turn_{idx + 1:03d}",
                                    role=role,
                                    content=norm_text.strip(),
                                    timestamp=msg.get("timestamp", now),
                                )
                            )
                except (json.JSONDecodeError, OSError):
                    pass

        # 2. Extract from workspace GEMINI.md & ~/.gemini/memories/
        for slug, title, path in (
            ("gemini_workspace_md", "Workspace GEMINI.md", workspace_root / "GEMINI.md"),
            ("gemini_global_summary", "Global Gemini Memory Summary", user_home / ".gemini" / "memories" / "memory_summary.md"),
        ):
            if path.exists():
                raw_md = path.read_text(encoding="utf-8")
                clean_md, s_cnt = strip_thinking_blocks(raw_md)
                stats.stripped_thinking_blocks += s_cnt
                norm_md, p_cnt = normalize_paths_to_ir(clean_md, host_fingerprint)
                stats.normalized_host_paths += p_cnt
                topic_memories.append(
                    TopicMemoryFile(
                        topic_slug=slug,
                        title=title,
                        summary=f"Extracted from {path.name}",
                        content=norm_md,
                        scope=classify_memory_scope(norm_md),
                        tags=["gemini-cli"],
                    )
                )

        if conversation_history:
            for turn in reversed(conversation_history):
                if turn.role == "user":
                    execution_stack.active_goal = turn.content[:240]
                    break

        git_wip = capture_git_wip_bundle(workspace_root)
        chk_id = MemoryCheckpointIR.generate_id(
            repo_name=repo_name,
            author=author,
            message=message,
            timestamp=now,
            parent=parent_checkpoint_id,
        )
        chk = MemoryCheckpointIR(
            checkpoint_id=chk_id,
            parent_checkpoint_id=parent_checkpoint_id,
            repo_name=repo_name,
            branch=branch,
            created_at=now,
            author=author,
            source_agent=self.agent_type,
            source_model=source_model,
            source_host=host_fingerprint,
            message=message,
            confirmed_facts=confirmed_facts,
            negative_memories=negative_memories,
            user_deferrals=user_deferrals,
            execution_stack=execution_stack,
            topic_memories=topic_memories,
            conversation_history=conversation_history,
            git_wip=git_wip,
            sanitization_stats=stats,
        )
        return chk

    def simulate_boot_and_load(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        model_name: str,
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        report = AgentBootSimulationReport(
            agent_type=self.agent_type,
            model_name=model_name,
            workspace_root=str(workspace_root),
            user_home=str(user_home),
        )
        chunks: List[str] = []

        # 1. Workspace GEMINI.md, AGENTS.md, .agent-context/HANDOFF.md, .agent-context/CHAT_HISTORY.md
        for p in (
            workspace_root / "GEMINI.md",
            workspace_root / "AGENTS.md",
            workspace_root / ".agent-context" / "HANDOFF.md",
            workspace_root / ".agent-context" / "CHAT_HISTORY.md",
        ):
            if p.exists():
                report.loaded_files.append(str(p))
                chunks.append(p.read_text(encoding="utf-8"))

        # Check AGENTS.md routing index line count (< 150 lines Pitfall 3 rule)
        agents_p = workspace_root / "AGENTS.md"
        if agents_p.exists():
            lines_cnt = len(agents_p.read_text(encoding="utf-8").splitlines())
            report.memory_index_lines = lines_cnt
            if lines_cnt > 150:
                report.within_budget_limits = False

        # 2. Global Gemini CLI home files (~/.gemini/GEMINI.md, ~/.gemini/memories/*)
        for p in (
            user_home / ".gemini" / "GEMINI.md",
            user_home / ".gemini" / "memories" / "memory_summary.md",
            user_home / ".gemini" / "memories" / "chat_history.md",
        ):
            if p.exists():
                report.loaded_files.append(str(p))
                chunks.append(p.read_text(encoding="utf-8"))

        full_context = "\n\n".join(chunks)
        report.context_window_bytes = len(full_context.encode("utf-8"))
        report.assembled_system_context_preview = full_context[:600]

        if "<thinking>" in full_context or '"signature": "ErUB' in full_context:
            report.foreign_thinking_leaks += 1

        for bad_path in forbidden_source_paths or []:
            if bad_path and bad_path in full_context:
                report.source_path_leaks += full_context.count(bad_path)

        for line in full_context.splitlines():
            if line.startswith("- `") and ("(verified_pass)" in line or "(unverified)" in line):
                if line.strip() not in report.active_constraints_loaded:
                    report.active_constraints_loaded.append(line.strip())
            elif "SUPERSEDED BY" in line:
                if line.strip() not in report.superseded_constraints_blocked:
                    report.superseded_constraints_blocked.append(line.strip())
            elif "[LOCKED DEFERRAL" in line or "FORBIDDEN (" in line:
                if line.strip() not in report.user_deferrals_enforced:
                    report.user_deferrals_enforced.append(line.strip())
            elif "Failed Attempt (" in line:
                if line.strip() not in report.negative_memories_loaded:
                    report.negative_memories_loaded.append(line.strip())

            m_step = re.search(r"\*\*\[(PARTIALLY_DONE|NOT_DONE)\]\*\*\s*`([^`]+)`:\s*(.+)", line)
            if m_step and report.resumed_step_id is None:
                report.resumed_step_id = m_step.group(2)
                report.resumed_step_title = m_step.group(3).strip()

        return report
