"""Claude Code Memory Adapter, Mock Seed Generator, and Startup Loading Simulator.

Models Claude Code's Four-Tier Progressive Hierarchy:
1. Static Instruction Layer: `CLAUDE.md`, `AGENTS.md`, `.claude/rules/*.md` (path-scoped YAML)
2. Dynamic Auto-Memory: `~/.claude/projects/<proj>/memory/MEMORY.md` (first 200 lines / 25KB loaded on start)
   and linked topic files (`topics/*.md`).
3. Episodic Session Transcripts: `~/.claude/projects/<proj>/*.jsonl` (containing <thinking> blocks,
   cryptographic signatures, tool_use / tool_result payloads).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from memgit.adapters.base import AgentAdapter, AgentBootSimulationReport, AgentSessionSummary
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


class ClaudeCodeAdapter(AgentAdapter):
    agent_type = AgentType.CLAUDE_CODE.value

    def list_sessions(
        self,
        user_home: Path,
        workspace_root: Optional[Path] = None,
    ) -> List[AgentSessionSummary]:
        projects_dir = user_home.resolve() / ".claude" / "projects"
        if not projects_dir.exists():
            return []
        summaries: List[AgentSessionSummary] = []
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jf in proj_dir.glob("*.jsonl"):
                title = jf.stem
                turns = 0
                ws = ""
                try:
                    for line in jf.read_text(encoding="utf-8", errors="replace").splitlines():
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        if not ws and isinstance(obj.get("cwd"), str):
                            ws = obj["cwd"]
                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                        role = msg.get("role") or obj.get("type")
                        if role in ("user", "assistant"):
                            turns += 1
                            if role == "user" and title == jf.stem:
                                content = msg.get("content", "")
                                if isinstance(content, list):
                                    text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                                    content = " ".join(text_parts)
                                if isinstance(content, str) and content.strip():
                                    first_line = content.strip().splitlines()[0]
                                    title = (first_line[:77] + "...") if len(first_line) > 80 else first_line
                except (OSError, json.JSONDecodeError):
                    continue
                ts = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc).isoformat()
                summaries.append(
                    AgentSessionSummary(
                        session_id=jf.stem,
                        agent_type=self.agent_type,
                        title=title,
                        updated_at=ts,
                        turn_count=turns,
                        workspace_path=ws,
                        source_path=str(jf),
                    )
                )
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    @staticmethod
    def seed_mock_claude_session(
        workspace_root: Path,
        user_home: Path,
        repo_name: str = "payment-engine",
    ) -> None:
        """Create a realistic mock Claude Code workspace, ~/.claude memory tree, and JSONL transcript."""
        workspace_root.mkdir(parents=True, exist_ok=True)
        user_home.mkdir(parents=True, exist_ok=True)
        proj_slug = repo_name.replace("/", "-")

        # 1. Static Instruction Layer: CLAUDE.md + .claude/rules/api_safety.md
        claude_md = (
            f"# CLAUDE.md — {repo_name}\n\n"
            "## Project Rules\n"
            f"- [fact:db.pool_size] Database connection pool in `{workspace_root}/src/db.py` MUST cap `max_overflow=10`.\n"
            "- [fact:module_x.concurrency] Module X (`src/worker.py`) has a memory leak under thread pool; disable concurrency (`workers=1`).\n"
            "- [fact:user.lang_pref] [PRIVATE] Always respond in Chinese (Mandarin) and use personal SSH key `~/.ssh/id_ed25519_personal`.\n"
        )
        (workspace_root / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

        rules_dir = workspace_root / ".claude" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "idempotency.md").write_text(
            "---\npaths:\n  - \"src/payment/**/*.py\"\n---\n"
            "# Payment Idempotency Rule\n"
            "All payment charge endpoints in `src/payment/` must require an `Idempotency-Key` header.\n",
            encoding="utf-8",
        )

        # 2. Dynamic Auto-Memory: ~/.claude/projects/<proj>/memory/MEMORY.md + topics/
        mem_dir = user_home / ".claude" / "projects" / proj_slug / "memory"
        topics_dir = mem_dir / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)

        (mem_dir / "MEMORY.md").write_text(
            f"# Auto-Memory Index ({repo_name})\n"
            f"- Workspace path observed: `{workspace_root}`\n"
            "- See [Retry & Backoff Architecture](./topics/retry_architecture.md)\n"
            "- See [Ledger Schema Notes](./topics/ledger_schema.md)\n",
            encoding="utf-8",
        )
        (topics_dir / "retry_architecture.md").write_text(
            "# Retry & Backoff Architecture\n"
            f"Inspect `{workspace_root}/src/retry.py`. Exponential jitter backoff uses base=0.5s, cap=8.0s.\n"
            "Never retry HTTP 422 Unprocessable Entity responses.\n",
            encoding="utf-8",
        )
        (topics_dir / "ledger_schema.md").write_text(
            "# Ledger Schema Notes\n"
            "All monetary amounts are stored as integer cents (`amount_cents: int`), never IEEE-754 floats.\n",
            encoding="utf-8",
        )

        # 3. Episodic Session Transcript: ~/.claude/projects/<proj>/session_001.jsonl
        # Includes Claude <thinking> blocks with cryptographic signatures, tool calls, failed paths, and user deferrals!
        proj_dir = user_home / ".claude" / "projects" / proj_slug
        events = [
            {
                "type": "user",
                "timestamp": "2026-11-17T08:00:00Z",
                "message": (
                    "Refactor the payment settlement worker in src/worker.py. "
                    "IMPORTANT: Skip Option B (Redis distributed lock) for now — do not implement Redis caching in v1!"
                ),
                "memgit_deferral": {
                    "deferral_id": "def_redis_v1",
                    "feature_or_option": "Option B: Redis distributed lock",
                    "directive": "Do NOT implement Redis distributed lock or Redis caching in v1; keep local SQLite/in-memory lock.",
                },
            },
            {
                "type": "assistant",
                "model": "claude-3-7-sonnet",
                "timestamp": "2026-11-17T08:05:00Z",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": (
                            f"<thinking>Let me inspect {workspace_root}/src/worker.py using /opt/homebrew/bin/python3. "
                            "Wait, trying multiprocessing.Pool with lambda closures failed with PicklingError.</thinking>"
                        ),
                        "signature": "ErUBCkYICBgCIkD9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0==",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Bash",
                        "input": {"command": f"/opt/homebrew/bin/python3 -m unittest {workspace_root}/tests/test_worker.py"},
                    },
                ],
                "memgit_negative_memory": {
                    "attempt_id": "neg_mp_pickle",
                    "hypothesis": "Use multiprocessing.Pool with inline closure callbacks in src/worker.py",
                    "failure_reason": f"Failed in {workspace_root}/src/worker.py with AttributeError/PicklingError on macOS spawn context",
                    "files_involved": ["src/worker.py"],
                },
            },
            {
                "type": "checkpoint_state",
                "timestamp": "2026-11-17T08:15:00Z",
                "execution_stack": {
                    "active_goal": "Complete deterministic payment batch settlement and fix batch rounding edge-case",
                    "current_hypothesis": f"In {workspace_root}/src/worker.py, integer cent remainder distribution needs largest-remainder method",
                    "modified_files": ["src/worker.py", "src/retry.py"],
                    "passing_test_commands": ["python3 -m unittest tests/test_ledger.py"],
                    "failing_test_commands": ["python3 -m unittest tests/test_worker.py"],
                    "stack_trace_digest": (
                        f"File \"{workspace_root}/tests/test_worker.py\", line 42, in test_split_settlement\n"
                        "AssertionError: 99 != 100 (lost 1 cent during 3-way split in src/worker.py)"
                    ),
                    "steps": [
                        {
                            "step_id": "step_1",
                            "title": "Implement integer-cents ledger model and idempotency check",
                            "status": TaskLockStatus.DONE.value,
                            "relative_files": ["src/ledger.py"],
                            "verification_cmd": "python3 -m unittest tests/test_ledger.py",
                            "notes": "Verified passing on source machine.",
                        },
                        {
                            "step_id": "step_2",
                            "title": "Implement largest-remainder cent allocation in settle_batch()",
                            "status": TaskLockStatus.PARTIALLY_DONE.value,
                            "relative_files": ["src/worker.py"],
                            "verification_cmd": "python3 -m unittest tests/test_worker.py",
                            "notes": f"Started editing {workspace_root}/src/worker.py; uncommitted WIP changes in working tree.",
                        },
                        {
                            "step_id": "step_3",
                            "title": "Add exponential backoff integration test for HTTP 503 vs HTTP 422",
                            "status": TaskLockStatus.NOT_DONE.value,
                            "relative_files": ["src/retry.py", "tests/test_worker.py"],
                            "verification_cmd": "python3 -m unittest discover -s tests",
                            "notes": "Run full suite after step_2 passes.",
                        },
                    ],
                },
            },
        ]
        transcript_path = proj_dir / "session_001.jsonl"
        with transcript_path.open("w", encoding="utf-8") as f:
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
        message: str = "Checkpoint from Claude Code",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        stats = SanitizationStats()
        confirmed_facts: List[ProvenanceNode] = []
        negative_memories: List[NegativeMemoryItem] = []
        user_deferrals: List[UserDeferralLock] = []
        topic_memories: List[TopicMemoryFile] = []
        conversation_history: List[ChatTurn] = []
        execution_stack = ExecutionStack(active_goal="Continue development task")

        proj_slug = repo_name.replace("/", "-")
        now = utc_now_iso()

        # 1. Parse CLAUDE.md for explicit facts
        claude_md_path = workspace_root / "CLAUDE.md"
        if claude_md_path.exists():
            raw_claude = claude_md_path.read_text(encoding="utf-8")
            cleaned_claude, stripped_cnt = strip_thinking_blocks(raw_claude)
            stats.stripped_thinking_blocks += stripped_cnt
            for line in cleaned_claude.splitlines():
                m = re.search(r"-\s*\[fact:([a-zA-Z0-9_.-]+)\]\s*(.+)", line)
                if m:
                    entity_key = m.group(1)
                    raw_stmt = m.group(2).strip()
                    scope = classify_memory_scope(raw_stmt)
                    norm_stmt, path_cnt = normalize_paths_to_ir(raw_stmt, host_fingerprint)
                    stats.normalized_host_paths += path_cnt
                    if scope == MemoryScope.PRIVATE.value:
                        stats.private_facts_count += 1
                    else:
                        stats.shared_facts_count += 1
                    node = ProvenanceNode(
                        fact_id=f"fact_{entity_key.replace('.', '_')}",
                        entity_key=entity_key,
                        category="architecture",
                        statement=norm_stmt,
                        scope=scope,
                        author=author,
                        source_agent=self.agent_type,
                        source_model=source_model,
                        timestamp=now,
                        parent_hash=parent_checkpoint_id,
                        verification_status=VerificationStatus.VERIFIED_PASS.value,
                    )
                    confirmed_facts.append(node)

        # 2. Parse .claude/rules/*.md (path-scoped YAML frontmatter rules)
        rules_dir = workspace_root / ".claude" / "rules"
        if rules_dir.exists():
            for rule_file in sorted(rules_dir.glob("*.md")):
                raw_rule = rule_file.read_text(encoding="utf-8")
                path_glob = None
                m_glob = re.search(r'paths:\s*\n\s*-\s*"?([^"\n]+)"?', raw_rule)
                if m_glob:
                    path_glob = m_glob.group(1).strip()
                body = re.sub(r"^---.*?---\s*", "", raw_rule, flags=re.DOTALL).strip()
                first_para = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]
                stmt = " ".join(first_para) if first_para else body
                norm_stmt, path_cnt = normalize_paths_to_ir(stmt, host_fingerprint)
                stats.normalized_host_paths += path_cnt
                stats.shared_facts_count += 1
                confirmed_facts.append(
                    ProvenanceNode(
                        fact_id=f"rule_{rule_file.stem}",
                        entity_key=f"rule.{rule_file.stem}",
                        category="path_rule",
                        statement=norm_stmt,
                        scope=MemoryScope.SHARED.value,
                        author=author,
                        source_agent=self.agent_type,
                        source_model=source_model,
                        timestamp=now,
                        parent_hash=parent_checkpoint_id,
                        verification_status=VerificationStatus.VERIFIED_PASS.value,
                        path_glob=path_glob,
                    )
                )

        # 3. Parse ~/.claude/projects/<proj>/memory/topics/*.md
        topics_dir = user_home / ".claude" / "projects" / proj_slug / "memory" / "topics"
        if topics_dir.exists():
            for tf in sorted(topics_dir.glob("*.md")):
                raw_tf = tf.read_text(encoding="utf-8")
                clean_tf, st_cnt = strip_thinking_blocks(raw_tf)
                stats.stripped_thinking_blocks += st_cnt
                norm_tf, p_cnt = normalize_paths_to_ir(clean_tf, host_fingerprint)
                stats.normalized_host_paths += p_cnt
                lines = [ln.strip() for ln in norm_tf.splitlines() if ln.strip()]
                title = lines[0].lstrip("#").strip() if lines else tf.stem
                summary = lines[1] if len(lines) > 1 else title
                topic_memories.append(
                    TopicMemoryFile(
                        topic_slug=tf.stem,
                        title=title,
                        summary=summary[:120],
                        content=norm_tf,
                        scope=classify_memory_scope(norm_tf),
                        tags=["claude-auto-memory"],
                    )
                )

        # 4. Parse ~/.claude/projects/<proj>/*.jsonl transcripts (Pitfall 1, 2, 4 sanitization)
        proj_dir = user_home / ".claude" / "projects" / proj_slug
        if proj_dir.exists():
            for jsonl_file in sorted(proj_dir.glob("*.jsonl")):
                for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    # Count and strip thinking blocks & tool_use schemas, while extracting user/assistant chat turns
                    ev_type = ev.get("type", "")
                    ev_ts = ev.get("timestamp", now)
                    text_parts: List[str] = []
                    tool_parts: List[str] = []

                    raw_blocks = ev.get("content", [])
                    if not isinstance(raw_blocks, list) and isinstance(ev.get("message"), dict):
                        msg_content = ev["message"].get("content", "")
                        if isinstance(msg_content, list):
                            raw_blocks = msg_content
                        elif isinstance(msg_content, str) and msg_content.strip():
                            text_parts.append(msg_content.strip())
                    elif isinstance(ev.get("message"), str) and ev["message"].strip():
                        text_parts.append(ev["message"].strip())

                    for block in raw_blocks if isinstance(raw_blocks, list) else []:
                        if isinstance(block, dict):
                            if block.get("type") == "thinking":
                                stats.stripped_thinking_blocks += 1
                            elif block.get("type") == "tool_use":
                                stats.stripped_tool_call_schemas += 1
                                tname = block.get("name", "tool")
                                tool_parts.append(f"`{tname}`")
                            elif block.get("type") == "text" and block.get("text"):
                                text_parts.append(str(block["text"]).strip())

                    if ev_type in ("user", "assistant") and (text_parts or tool_parts):
                        joined_text = "\n\n".join(text_parts) if text_parts else f"Executed {', '.join(tool_parts)}."
                        clean_txt, s_cnt = strip_thinking_blocks(joined_text)
                        stats.stripped_thinking_blocks += s_cnt
                        norm_txt, p_cnt = normalize_paths_to_ir(clean_txt, host_fingerprint)
                        stats.normalized_host_paths += p_cnt
                        conversation_history.append(
                            ChatTurn(
                                turn_id=f"turn_{ev_type}_{len(conversation_history)+1}",
                                role=ev_type,
                                content=norm_txt,
                                timestamp=ev_ts,
                                tool_summary=", ".join(tool_parts) if tool_parts else None,
                            )
                        )
                        stats.conversation_turns_captured += 1

                    if "memgit_deferral" in ev:
                        d_raw = ev["memgit_deferral"]
                        norm_dir, p_cnt = normalize_paths_to_ir(d_raw["directive"], host_fingerprint)
                        stats.normalized_host_paths += p_cnt
                        stats.isolated_user_deferrals += 1
                        user_deferrals.append(
                            UserDeferralLock(
                                deferral_id=d_raw["deferral_id"],
                                feature_or_option=d_raw["feature_or_option"],
                                directive=norm_dir,
                                author=author,
                            )
                        )

                    if "memgit_negative_memory" in ev:
                        n_raw = ev["memgit_negative_memory"]
                        norm_hyp, p1 = normalize_paths_to_ir(n_raw["hypothesis"], host_fingerprint)
                        norm_fail, p2 = normalize_paths_to_ir(n_raw["failure_reason"], host_fingerprint)
                        stats.normalized_host_paths += p1 + p2
                        negative_memories.append(
                            NegativeMemoryItem(
                                attempt_id=n_raw["attempt_id"],
                                hypothesis=norm_hyp,
                                failure_reason=norm_fail,
                                files_involved=n_raw.get("files_involved", []),
                                source_model=source_model,
                            )
                        )

                    if ev.get("type") == "checkpoint_state" and "execution_stack" in ev:
                        es_raw = ev["execution_stack"]
                        raw_json_str = json.dumps(es_raw)
                        norm_json_str, p_cnt = normalize_paths_to_ir(raw_json_str, host_fingerprint)
                        stats.normalized_host_paths += p_cnt
                        execution_stack = ExecutionStack.from_dict(json.loads(norm_json_str))

        # 5. Capture Dual-Stream Git WIP Patch
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
            conversation_history=conversation_history,
            git_wip=git_wip,
            sanitization_stats=stats,
        )

    def simulate_boot_and_load(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        model_name: str = "claude-3-7-sonnet",
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        """Simulate Claude Code's startup context assembly:
        - Reads CLAUDE.md & AGENTS.md
        - Reads .claude/rules/*.md
        - Reads first 200 lines (~25 KB max) of ~/.claude/projects/<proj>/memory/MEMORY.md
        - Reads .agent-context/HANDOFF.md
        """
        report = AgentBootSimulationReport(
            agent_type=self.agent_type,
            model_name=model_name,
            workspace_root=str(workspace_root),
            user_home=str(user_home),
        )
        assembled_chunks: List[str] = []
        proj_slug = repo_name.replace("/", "-")

        candidates = [
            workspace_root / "CLAUDE.md",
            workspace_root / "AGENTS.md",
            workspace_root / ".agent-context" / "HANDOFF.md",
        ]
        rules_dir = workspace_root / ".claude" / "rules"
        if rules_dir.exists():
            candidates.extend(sorted(rules_dir.glob("*.md")))

        for path in candidates:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                report.loaded_files.append(str(path))
                assembled_chunks.append(text)

        encoded_key = re.sub(r"[^a-zA-Z0-9-]", "-", str(workspace_root.resolve()))
        mem_dir = user_home / ".claude" / "projects" / encoded_key / "memory"
        if not (mem_dir / "MEMORY.md").exists():
            mem_dir = user_home / ".claude" / "projects" / proj_slug / "memory"

        mem_index = mem_dir / "MEMORY.md"
        if mem_index.exists():
            raw_mem = mem_index.read_text(encoding="utf-8")
            lines = raw_mem.splitlines()
            report.memory_index_lines = len(lines)
            # Claude Code truncates MEMORY.md after 200 lines / 25 KB on start
            if len(lines) > 200 or len(raw_mem.encode("utf-8")) > 25600:
                report.within_budget_limits = False
            loaded_mem = "\n".join(lines[:200])
            report.loaded_files.append(str(mem_index))
            assembled_chunks.append(loaded_mem)

            # Simulate on-demand resolution of linked topic files from MEMORY.md
            for m_link in re.finditer(r"\]\(\.?/?([a-zA-Z0-9_./-]+\.md)\)", loaded_mem):
                topic_p = mem_dir / m_link.group(1)
                if topic_p.exists() and str(topic_p) not in report.loaded_files:
                    report.loaded_files.append(str(topic_p))
                    assembled_chunks.append(topic_p.read_text(encoding="utf-8"))

        full_context = "\n\n".join(assembled_chunks)
        report.context_window_bytes = len(full_context.encode("utf-8"))
        report.assembled_system_context_preview = full_context[:600]

        # Check for leaked raw <thinking> signatures
        if "<thinking>" in full_context and "Initialize a fresh <thinking> chain" not in full_context:
            report.foreign_thinking_leaks += 1
        if '"signature": "ErUB' in full_context:
            report.foreign_thinking_leaks += 1

        # Check for leaked source machine paths
        for bad_path in forbidden_source_paths or []:
            if bad_path and bad_path in full_context:
                report.source_path_leaks += full_context.count(bad_path)

        # Parse active constraints, deferrals, and next step from assembled context
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
