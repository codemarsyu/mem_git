"""Google Antigravity Memory Adapter and Artifact-Centric Startup Simulator.

Models Google Antigravity's Three-Tier Architecture:
1. Static Instruction Layer: `GEMINI.md`, `AGENTS.md`
2. Dynamic Auto-Memory: `~/.gemini/antigravity/knowledge/<ki_slug>/` (`metadata.json` + `artifacts/*.md`)
3. Session & Checkpoint Artifacts: `~/.gemini/antigravity/brain/<conv_id>/` (`task.md`, `implementation_plan.md`)
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


class AntigravityAdapter(AgentAdapter):
    agent_type = AgentType.ANTIGRAVITY.value

    def extract_checkpoint(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        author: str,
        source_model: str,
        host_fingerprint: HostFingerprint,
        message: str = "Checkpoint from Google Antigravity",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        stats = SanitizationStats()
        confirmed_facts: List[ProvenanceNode] = []
        negative_memories: List[NegativeMemoryItem] = []
        user_deferrals: List[UserDeferralLock] = []
        topic_memories: List[TopicMemoryFile] = []
        execution_stack = ExecutionStack(active_goal="Continue task from Antigravity")
        now = utc_now_iso()

        # 1. Extract Knowledge Items from ~/.gemini/antigravity/knowledge/
        ki_root = user_home / ".gemini" / "antigravity" / "knowledge"
        if ki_root.exists():
            for ki_dir in sorted(ki_root.iterdir()):
                if not ki_dir.is_dir():
                    continue
                meta_file = ki_dir / "metadata.json"
                overview_file = ki_dir / "artifacts" / "overview.md"
                if meta_file.exists() and overview_file.exists():
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    raw_md = overview_file.read_text(encoding="utf-8")
                    clean_md, s_cnt = strip_thinking_blocks(raw_md)
                    stats.stripped_thinking_blocks += s_cnt
                    norm_md, p_cnt = normalize_paths_to_ir(clean_md, host_fingerprint)
                    stats.normalized_host_paths += p_cnt
                    topic_memories.append(
                        TopicMemoryFile(
                            topic_slug=ki_dir.name,
                            title=meta.get("title", ki_dir.name),
                            summary=meta.get("summary", ki_dir.name),
                            content=norm_md,
                            scope=classify_memory_scope(norm_md),
                            tags=meta.get("tags", ["antigravity-ki"]),
                        )
                    )

        # 2. Extract facts and deferrals from .agent-context/HANDOFF.md or GEMINI.md
        handoff_path = workspace_root / ".agent-context" / "HANDOFF.md"
        if handoff_path.exists():
            text = handoff_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                m_fact = re.search(
                    r"^-\s*`([^`]+)`\s*\[(SHARED|PRIVATE)\]\s*\(([^)]+)\):\s*(.+?)\s*\*\(by\s+",
                    line,
                )
                if m_fact:
                    ekey = m_fact.group(1)
                    scope = MemoryScope.PRIVATE.value if m_fact.group(2) == "PRIVATE" else MemoryScope.SHARED.value
                    vstat = m_fact.group(3)
                    stmt, p_cnt = normalize_paths_to_ir(m_fact.group(4).strip(), host_fingerprint)
                    stats.normalized_host_paths += p_cnt
                    confirmed_facts.append(
                        ProvenanceNode(
                            fact_id=f"fact_{ekey.replace('.', '_')}",
                            entity_key=ekey,
                            category="architecture",
                            statement=stmt,
                            scope=scope,
                            author=author,
                            source_agent=self.agent_type,
                            source_model=source_model,
                            timestamp=now,
                            parent_hash=parent_checkpoint_id,
                            verification_status=vstat,
                        )
                    )
                m_def = re.search(
                    r"\*\*\[LOCKED DEFERRAL `([^`]+)`\]\s*([^*]+)\*\*:\s*(.+)", line
                )
                if m_def:
                    user_deferrals.append(
                        UserDeferralLock(
                            deferral_id=m_def.group(1),
                            feature_or_option=m_def.group(2).strip(),
                            directive=m_def.group(3).strip(),
                            author=author,
                        )
                    )

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
            negative_memories=negative_memories,
            user_deferrals=user_deferrals,
            execution_stack=execution_stack,
            topic_memories=topic_memories,
            git_wip=git_wip,
            sanitization_stats=stats,
        )

    def simulate_boot_and_load(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        model_name: str = "gemini-4-argon",
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        """Simulate Google Antigravity startup memory & artifact loading:
        1. Loads `GEMINI.md` and `AGENTS.md` (<150 lines routing index)
        2. Indexes Knowledge Items in `~/.gemini/antigravity/knowledge/*/metadata.json` + `artifacts/overview.md`
        3. Mounts session Artifacts (`task.md`, `implementation_plan.md`) from `~/.gemini/antigravity/brain/<id>/`
        """
        report = AgentBootSimulationReport(
            agent_type=self.agent_type,
            model_name=model_name,
            workspace_root=str(workspace_root),
            user_home=str(user_home),
        )
        chunks: List[str] = []

        for p in (
            workspace_root / "GEMINI.md",
            workspace_root / "AGENTS.md",
            workspace_root / ".agent-context" / "HANDOFF.md",
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

        # Load Knowledge Items
        ki_root = user_home / ".gemini" / "antigravity" / "knowledge"
        if ki_root.exists():
            for ki_dir in sorted(ki_root.iterdir()):
                if ki_dir.is_dir():
                    meta_p = ki_dir / "metadata.json"
                    art_p = ki_dir / "artifacts" / "overview.md"
                    for p in (meta_p, art_p):
                        if p.exists():
                            report.loaded_files.append(str(p))
                            chunks.append(p.read_text(encoding="utf-8"))

        # Load Brain Artifacts (task.md, implementation_plan.md)
        brain_root = user_home / ".gemini" / "antigravity" / "brain"
        if brain_root.exists():
            for conv_dir in sorted(brain_root.iterdir()):
                if conv_dir.is_dir():
                    for p in (conv_dir / "task.md", conv_dir / "implementation_plan.md"):
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
