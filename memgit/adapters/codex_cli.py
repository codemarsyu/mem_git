"""OpenAI Codex CLI Memory Adapter, Mock Session Seeder, and Startup Loading Simulator.

Models OpenAI Codex CLI's Three-Tier Architecture:
1. Static Instruction Layer: `~/.codex/AGENTS.md` & cascading repo-level `AGENTS.md` (up to 32 KiB limit)
2. Dynamic Auto-Memory: `~/.codex/memories/memory_summary.md` & `~/.codex/memories/MEMORY.md`
3. Episodic Execution Logs: `~/.codex/sessions/<session_id>/execution.jsonl`
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
    TaskLockStatus,
    TaskStep,
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

CODEX_MAX_AGENTS_BYTES = 32 * 1024  # 32 KiB cascading limit


class CodexCLIAdapter(AgentAdapter):
    agent_type = AgentType.CODEX_CLI.value

    @staticmethod
    def seed_mock_codex_update(
        workspace_root: Path,
        user_home: Path,
        repo_name: str = "payment-engine",
    ) -> None:
        """Simulate Codex CLI completing Step 2 and fixing Module X concurrency on Machine B."""
        workspace_root.mkdir(parents=True, exist_ok=True)
        codex_mem_dir = user_home / ".codex" / "memories"
        codex_sess_dir = user_home / ".codex" / "sessions" / "sess_codex_01"
        codex_mem_dir.mkdir(parents=True, exist_ok=True)
        codex_sess_dir.mkdir(parents=True, exist_ok=True)

        # Codex updates repo AGENTS.md and execution.jsonl after fixing Module X leak & step_2
        events = [
            {
                "type": "reasoning",
                "id": "rs_codex_99",
                "summary": f"Analyzed {workspace_root}/src/worker.py on Linux GPU worker; fixed thread-local buffer leak.",
            },
            {
                "type": "fact_update",
                "fact_id": "fact_module_x_concurrency_v2",
                "entity_key": "module_x.concurrency",
                "category": "architecture",
                "statement": (
                    f"Fixed Module X memory leak in `{workspace_root}/src/worker.py` using contextvars; "
                    "full concurrency enabled (`workers=8`)."
                ),
                "verification_status": VerificationStatus.VERIFIED_PASS.value,
            },
            {
                "type": "fact_update",
                "fact_id": "fact_settlement_rounding",
                "entity_key": "settlement.rounding",
                "category": "architecture",
                "statement": "Batch cent allocation uses largest-remainder algorithm with deterministic tie-breaking by account_id.",
                "verification_status": VerificationStatus.VERIFIED_PASS.value,
            },
            {
                "type": "checkpoint_state",
                "execution_stack": {
                    "active_goal": "Complete deterministic payment batch settlement and fix batch rounding edge-case",
                    "current_hypothesis": "Step 2 verified passing; ready for Step 3 backoff integration test",
                    "modified_files": ["src/worker.py"],
                    "passing_test_commands": [
                        "python3 -m unittest tests/test_ledger.py",
                        "python3 -m unittest tests/test_worker.py",
                    ],
                    "failing_test_commands": [],
                    "stack_trace_digest": "",
                    "steps": [
                        {
                            "step_id": "step_1",
                            "title": "Implement integer-cents ledger model and idempotency check",
                            "status": TaskLockStatus.DONE.value,
                            "relative_files": ["src/ledger.py"],
                            "verification_cmd": "python3 -m unittest tests/test_ledger.py",
                            "notes": "Completed on Machine A (Claude Code).",
                        },
                        {
                            "step_id": "step_2",
                            "title": "Implement largest-remainder cent allocation in settle_batch()",
                            "status": TaskLockStatus.DONE.value,
                            "relative_files": ["src/worker.py"],
                            "verification_cmd": "python3 -m unittest tests/test_worker.py",
                            "notes": "Completed and verified passing on Machine B (OpenAI Codex CLI).",
                        },
                        {
                            "step_id": "step_3",
                            "title": "Add exponential backoff integration test for HTTP 503 vs HTTP 422",
                            "status": TaskLockStatus.NOT_DONE.value,
                            "relative_files": ["src/retry.py", "tests/test_worker.py"],
                            "verification_cmd": "python3 -m unittest discover -s tests",
                            "notes": "Ready for next agent to execute.",
                        },
                    ],
                },
            },
        ]
        with (codex_sess_dir / "execution.jsonl").open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def extract_checkpoint(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        author: str,
        source_model: str,
        host_fingerprint: HostFingerprint,
        message: str = "Checkpoint from OpenAI Codex CLI",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        stats = SanitizationStats()
        confirmed_facts: List[ProvenanceNode] = []
        negative_memories: List[NegativeMemoryItem] = []
        user_deferrals: List[UserDeferralLock] = []
        topic_memories: List[TopicMemoryFile] = []
        execution_stack = ExecutionStack(active_goal="Continue task from Codex CLI")
        now = utc_now_iso()

        # 1. Read existing .agent-context/HANDOFF.md if present to preserve inherited deferrals/negatives
        handoff_path = workspace_root / ".agent-context" / "HANDOFF.md"
        if handoff_path.exists():
            handoff_text = handoff_path.read_text(encoding="utf-8")
            for line in handoff_text.splitlines():
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

        # 2. Parse ~/.codex/sessions/*/execution.jsonl
        sess_root = user_home / ".codex" / "sessions"
        if sess_root.exists():
            for jsonl_file in sorted(sess_root.glob("*/execution.jsonl")):
                for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    if ev.get("type") == "reasoning":
                        stats.stripped_thinking_blocks += 1
                    elif ev.get("type") == "fact_update":
                        norm_stmt, p_cnt = normalize_paths_to_ir(ev["statement"], host_fingerprint)
                        stats.normalized_host_paths += p_cnt
                        scope = classify_memory_scope(norm_stmt)
                        if scope == MemoryScope.PRIVATE.value:
                            stats.private_facts_count += 1
                        else:
                            stats.shared_facts_count += 1
                        confirmed_facts.append(
                            ProvenanceNode(
                                fact_id=ev["fact_id"],
                                entity_key=ev["entity_key"],
                                category=ev.get("category", "architecture"),
                                statement=norm_stmt,
                                scope=scope,
                                author=author,
                                source_agent=self.agent_type,
                                source_model=source_model,
                                timestamp=now,
                                parent_hash=parent_checkpoint_id,
                                verification_status=ev.get(
                                    "verification_status", VerificationStatus.VERIFIED_PASS.value
                                ),
                            )
                        )
                    elif ev.get("type") == "checkpoint_state" and "execution_stack" in ev:
                        raw_str = json.dumps(ev["execution_stack"])
                        norm_str, p_cnt = normalize_paths_to_ir(raw_str, host_fingerprint)
                        stats.normalized_host_paths += p_cnt
                        execution_stack = ExecutionStack.from_dict(json.loads(norm_str))

        # 3. Parse .agent-context/memory/*.md topics if present
        mem_topics_dir = workspace_root / ".agent-context" / "memory"
        if mem_topics_dir.exists():
            for tf in sorted(mem_topics_dir.glob("*.md")):
                raw_tf = tf.read_text(encoding="utf-8")
                clean_tf, s_cnt = strip_thinking_blocks(raw_tf)
                stats.stripped_thinking_blocks += s_cnt
                norm_tf, p_cnt = normalize_paths_to_ir(clean_tf, host_fingerprint)
                stats.normalized_host_paths += p_cnt
                lines = [ln.strip() for ln in norm_tf.splitlines() if ln.strip()]
                title = lines[0].lstrip("#").strip() if lines else tf.stem
                topic_memories.append(
                    TopicMemoryFile(
                        topic_slug=tf.stem,
                        title=title,
                        summary=title,
                        content=norm_tf,
                        scope=classify_memory_scope(norm_tf),
                        tags=["codex-memory"],
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
        model_name: str = "gpt-5-codex",
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        """Simulate OpenAI Codex CLI startup loading:
        1. Cascading concatenation of `~/.codex/AGENTS.md` + `<workspace_root>/AGENTS.md` (32 KiB limit)
        2. Auto-Memory loading from `~/.codex/memories/memory_summary.md` & `MEMORY.md`
        """
        report = AgentBootSimulationReport(
            agent_type=self.agent_type,
            model_name=model_name,
            workspace_root=str(workspace_root),
            user_home=str(user_home),
        )

        cascade_chunks: List[str] = []
        global_agents = user_home / ".codex" / "AGENTS.md"
        repo_agents = workspace_root / "AGENTS.md"

        for p in (global_agents, repo_agents):
            if p.exists():
                report.loaded_files.append(str(p))
                cascade_chunks.append(p.read_text(encoding="utf-8"))

        cascaded_agents_bytes = len("\n".join(cascade_chunks).encode("utf-8"))
        if cascaded_agents_bytes > CODEX_MAX_AGENTS_BYTES:
            report.within_budget_limits = False

        # Also load ~/.codex/memories/memory_summary.md and .agent-context/HANDOFF.md
        mem_summary = user_home / ".codex" / "memories" / "memory_summary.md"
        mem_full = user_home / ".codex" / "memories" / "MEMORY.md"
        for p in (mem_summary, mem_full):
            if p.exists():
                report.loaded_files.append(str(p))
                cascade_chunks.append(p.read_text(encoding="utf-8"))

        full_context = "\n\n".join(cascade_chunks)
        report.context_window_bytes = len(full_context.encode("utf-8"))
        report.memory_index_lines = len(full_context.splitlines())
        report.assembled_system_context_preview = full_context[:600]

        if "<thinking>" in full_context or '"signature": "ErUB' in full_context:
            report.foreign_thinking_leaks += 1

        for bad_path in forbidden_source_paths or []:
            if bad_path and bad_path in full_context:
                report.source_path_leaks += full_context.count(bad_path)

        for line in full_context.splitlines():
            if line.startswith("- `") and ("(verified_pass)" in line or "(unverified)" in line):
                report.active_constraints_loaded.append(line.strip())
            elif "SUPERSEDED BY" in line:
                report.superseded_constraints_blocked.append(line.strip())
            elif "[LOCKED DEFERRAL" in line or "FORBIDDEN (" in line:
                report.user_deferrals_enforced.append(line.strip())
            elif "Failed Attempt (" in line:
                report.negative_memories_loaded.append(line.strip())

            m_step = re.search(r"\*\*\[(PARTIALLY_DONE|NOT_DONE)\]\*\*\s*`([^`]+)`:\s*(.+)", line)
            if m_step and report.resumed_step_id is None:
                report.resumed_step_id = m_step.group(2)
                report.resumed_step_title = m_step.group(3).strip()

        return report
