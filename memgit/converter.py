"""Dedicated Cross-Agent Checkpoint Converter (MemoryCheckpointIR -> Claude Code / Codex CLI / Antigravity).

Handles all required target-specific conversion & installation operations so that when
the user opens the target agent app (e.g. Claude Code or Codex CLI), the project is
already registered, pre-trusted, and ready to resume:
1. Exact target discovery path encoding (Claude Code's `-Users-...` encoded workspace key)
2. Native memory schema formatting:
   - Claude Code 2.1+: YAML frontmatter topic files (`name`, `description`, `metadata.type: project|feedback|reference`,
     `**Why:**`, `**How to apply:**`, `[[wiki-links]]`) + <=200-line `MEMORY.md` index + `CLAUDE.md`
   - OpenAI Codex CLI: Cascading `~/.codex/AGENTS.md` + repo `AGENTS.md` (<=32 KiB) + `~/.codex/memories/memory_summary.md`
3. App Project Registration & Session Bootstrap:
   - Updates `~/.claude.json` (`projects[workspace_root]`) with `hasTrustDialogAccepted: true` and `lastSessionId`
   - Synthesizes a native resumable session transcript (`~/.claude/projects/<encoded-key>/<session-id>.jsonl`)
     and registers it in `~/.claude/history.jsonl` so `claude --continue` / `claude --resume` / Claude app opens directly into the checkpoint!
4. Host Path Re-Hydration (`$WORKSPACE_ROOT`, `$USER_HOME`) & OS runtime adaptation (macOS vs Linux commands)
5. Non-destructive delimited upsert (`<!-- MEMGIT:BEGIN --> ... <!-- MEMGIT:END -->`) for existing workspace files
6. Post-write load verification
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from memgit.adapters import AgentBootSimulationReport, get_adapter
from memgit.backup import BackupManager, BackupTransaction
from memgit.compiler import CompiledMemoryPackage, compile_checkpoint
from memgit.models import (
    AgentType,
    HostFingerprint,
    MemoryCheckpointIR,
    TaskLockStatus,
    VerificationStatus,
)
from memgit.sync import (
    backup_sqlite_if_nonempty,
    patch_trajectory_db_standalone,
    pb_encode_bytes_field,
    pb_encode_varint,
    pb_encode_varint_field,
    pb_iter_fields,
    pb_patch_raw_summary_standalone,
    pb_patch_trajectory_metadata_standalone,
    pb_read_varint,
    sync_agyhub_summaries_proto,
    sync_antigravity_cli_session_to_app,
)
from memgit.sanitizer import filter_checkpoint_for_target

MEMGIT_BEGIN_MARKER = "<!-- MEMGIT:BEGIN -->"
MEMGIT_END_MARKER = "<!-- MEMGIT:END -->"
CODEX_TOTAL_AGENTS_BUDGET_BYTES = 32 * 1024  # 32 KiB cascading limit


def encode_claude_project_key(workspace_root: Path) -> str:
    """Encode an absolute workspace path using Claude Code's `~/.claude/projects/<key>` convention.

    Example:
        /Users/ruoyuli/Documents/blender_scene/3d_scene
        -> -Users-ruoyuli-Documents-blender-scene-3d-scene
    """
    resolved = str(workspace_root.resolve())
    return re.sub(r"[^a-zA-Z0-9-]", "-", resolved)


def adapt_runtime_command(cmd: str, target_host: HostFingerprint) -> str:
    """Adapt OS-specific binary invocations (e.g. macOS Blender.app -> Linux `blender`)."""
    if target_host.os_name.lower() == "linux":
        cmd = cmd.replace("/Applications/Blender.app/Contents/MacOS/Blender", "blender")
        cmd = cmd.replace("/opt/homebrew/bin/python3", "python3")
    return cmd


def upsert_delimited_section(existing_text: str, new_block: str) -> str:
    """Non-destructively replace or append a MemGit-managed section in CLAUDE.md / AGENTS.md."""
    wrapped = f"{MEMGIT_BEGIN_MARKER}\n{new_block.strip()}\n{MEMGIT_END_MARKER}\n"
    if MEMGIT_BEGIN_MARKER in existing_text and MEMGIT_END_MARKER in existing_text:
        pattern = re.compile(
            re.escape(MEMGIT_BEGIN_MARKER) + r".*?" + re.escape(MEMGIT_END_MARKER) + r"\n?",
            re.DOTALL,
        )
        return pattern.sub(wrapped, existing_text, count=1)
    if existing_text.strip():
        return existing_text.rstrip() + "\n\n" + wrapped
    return wrapped


def _deterministic_uuid(seed: str) -> str:
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return str(uuid.UUID(h[:32]))


@dataclass
class BootstrapSessionReport:
    """Result of automatically invoking a destination agent CLI (e.g. `agy` or `gemini`)
    to create a native resumable session so the conversation appears immediately in the app."""
    agent: str
    cli_binary: str
    command: List[str] = field(default_factory=list)
    executed: bool = False
    succeeded: bool = False
    return_code: Optional[int] = None
    conversation_id: Optional[str] = None
    conversation_url: Optional[str] = None
    project_id: Optional[str] = None
    synced_steps: Optional[int] = None
    synced_files: List[str] = field(default_factory=list)
    stdout_preview: str = ""
    stderr_preview: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "cli_binary": self.cli_binary,
            "command": self.command,
            "executed": self.executed,
            "succeeded": self.succeeded,
            "return_code": self.return_code,
            "conversation_id": self.conversation_id,
            "conversation_url": self.conversation_url,
            "project_id": self.project_id,
            "synced_steps": self.synced_steps,
            "synced_files": self.synced_files,
            "stdout_preview": self.stdout_preview,
            "stderr_preview": self.stderr_preview,
            "note": self.note,
        }


@dataclass
class ConversionResult:
    """Detailed report of a checkpoint conversion, installation, app registration, and load verification."""
    checkpoint_id: str
    source_agent: str
    source_model: str
    target_agent: str
    target_model: str
    encoded_project_key: str
    session_id: Optional[str]
    workspace_root: str
    user_home: str
    written_files: List[str] = field(default_factory=list)
    operations_performed: List[str] = field(default_factory=list)
    boot_verification: Optional[AgentBootSimulationReport] = None
    bootstrap_session: Optional[BootstrapSessionReport] = None
    tier1_succeeded: bool = True
    tier2_status: str = "skipped"  # "applied" | "skipped" | "fallback"
    tier2_warnings: List[str] = field(default_factory=list)
    backup_id: Optional[str] = None
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "source_agent": self.source_agent,
            "source_model": self.source_model,
            "target_agent": self.target_agent,
            "target_model": self.target_model,
            "encoded_project_key": self.encoded_project_key,
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "user_home": self.user_home,
            "written_files": self.written_files,
            "operations_performed": self.operations_performed,
            "boot_verification": self.boot_verification.to_dict() if self.boot_verification else None,
            "bootstrap_session": self.bootstrap_session.to_dict() if self.bootstrap_session else None,
            "tier1_succeeded": self.tier1_succeeded,
            "tier2_status": self.tier2_status,
            "tier2_warnings": self.tier2_warnings,
            "backup_id": self.backup_id,
            "dry_run": self.dry_run,
        }


class CheckpointConverter:
    """Converts a MemoryCheckpointIR into authentic target-agent memory structures, registers the project, and installs sessions."""

    @staticmethod
    def load_checkpoint_file(checkpoint_path: Path) -> MemoryCheckpointIR:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return MemoryCheckpointIR.from_dict(data)

    def convert_to_package(
        self,
        raw_checkpoint: MemoryCheckpointIR,
        target_agent: str,
        target_model: str,
        target_host: HostFingerprint,
        include_private: bool = True,
    ) -> CompiledMemoryPackage:
        """Transform a MemoryCheckpointIR into a CompiledMemoryPackage tailored for the target agent."""
        agent_norm = target_agent.lower().strip()
        if agent_norm in ("claude", AgentType.CLAUDE_CODE.value):
            return self._convert_for_claude_code(
                raw_checkpoint, target_model, target_host, include_private
            )
        if agent_norm in ("codex", "codex-cli", AgentType.CODEX_CLI.value):
            return self._convert_for_codex_cli(
                raw_checkpoint, target_model, target_host, include_private
            )
        return compile_checkpoint(
            raw_checkpoint=raw_checkpoint,
            target_agent=agent_norm,
            target_model=target_model,
            target_host=target_host,
            include_private=include_private,
        )

    def _convert_for_claude_code(
        self,
        raw_checkpoint: MemoryCheckpointIR,
        target_model: str,
        target_host: HostFingerprint,
        include_private: bool,
    ) -> CompiledMemoryPackage:
        """Build authentic Claude Code 2.1+ memory files:
        - ~/.claude/projects/<encoded-abs-path>/memory/MEMORY.md (<= 200-line index)
        - ~/.claude/projects/<encoded-abs-path>/memory/*.md (YAML frontmatter with metadata.type, Why, How to apply, [[links]])
        - <workspace_root>/CLAUDE.md (<= 150-line routing & continuity instructions)
        - <workspace_root>/.agent-context/HANDOFF.md (full declarative handoff state)
        """
        chk = filter_checkpoint_for_target(
            raw_checkpoint, target_host=target_host, include_private=include_private
        )
        base_pkg = compile_checkpoint(
            raw_checkpoint=raw_checkpoint,
            target_agent=AgentType.CLAUDE_CODE.value,
            target_model=target_model,
            target_host=target_host,
            include_private=include_private,
        )
        ws_path = Path(target_host.workspace_root)
        encoded_key = encode_claude_project_key(ws_path)
        mem_rel_base = f".claude/projects/{encoded_key}/memory"

        active_facts = [
            f for f in chk.confirmed_facts
            if f.verification_status != VerificationStatus.SUPERSEDED.value
        ]
        superseded_facts = [
            f for f in chk.confirmed_facts
            if f.verification_status == VerificationStatus.SUPERSEDED.value
        ]

        home_files: Dict[str, str] = {}
        index_entries: List[str] = []

        # 1. project_architecture.md (metadata.type: project)
        arch_lines = [
            "---",
            "name: project-architecture",
            f"description: Verified architectural decisions and structure for {chk.repo_name} from MemGit checkpoint {chk.checkpoint_id}",
            "type: project",
            "metadata:",
            "  type: project",
            "---",
            "",
            f"# {chk.repo_name} — Confirmed Architecture (Checkpoint `{chk.checkpoint_id}`)",
            "",
        ]
        for f in active_facts:
            arch_lines.append(f"- **`{f.entity_key}`** ({f.verification_status}): {f.statement}")
        if superseded_facts:
            arch_lines.extend(["", "## Superseded Assumptions (Do Not Revert)"])
            for sf in superseded_facts:
                arch_lines.append(f"- ~~`{sf.entity_key}`: {sf.statement}~~ (superseded by `{sf.superseded_by}`)")
        arch_lines.extend([
            "",
            f"**Why:** Imported from `{chk.source_agent}` (`{chk.source_model}`) at `{chk.created_at}` to maintain cross-agent architectural continuity.",
            "**How to apply:** Preserve these collection names, module boundaries, and verified parameters when modifying files in [[project-execution-stack]] while respecting [[feedback-guardrails]].",
        ])
        home_files[f"{mem_rel_base}/project_architecture.md"] = "\n".join(arch_lines) + "\n"
        index_entries.append(
            f"- [Architecture & Verified Facts](project_architecture.md) — {len(active_facts)} verified architectural facts from `{chk.checkpoint_id}`"
        )

        # 2. feedback_guardrails.md (metadata.type: feedback)
        if chk.negative_memories or chk.user_deferrals:
            fb_lines = [
                "---",
                "name: feedback-guardrails",
                f"description: Known failed approaches to avoid repeating and explicit user deferrals for {chk.repo_name}",
                "type: feedback",
                "metadata:",
                "  type: feedback",
                "---",
                "",
                "# Guardrails & Failed Paths (Negative Memory)",
                "",
            ]
            for nm in chk.negative_memories:
                files_str = ", ".join(f"`{p}`" for p in nm.files_involved) if nm.files_involved else "general"
                fb_lines.append(f"- **Avoid (`{nm.attempt_id}`)**: {nm.hypothesis} ({files_str})")
                fb_lines.append(f"  - **Why:** {nm.failure_reason}")
            if chk.user_deferrals:
                fb_lines.extend(["", "# Explicit User Deferral Locks (FORBIDDEN UNLESS RE-AUTHORIZED)"])
                for ud in chk.user_deferrals:
                    fb_lines.append(f"- **[LOCKED DEFERRAL `{ud.deferral_id}`] {ud.feature_or_option}**: {ud.directive}")
                    fb_lines.append(f"  - **Unlock Condition**: {ud.reauthorize_condition}")
            fb_lines.extend([
                "",
                "**Why:** These dead-ends caused errors or visual regressions during prior agent execution.",
                "**How to apply:** Check these guardrails before running commands or editing files listed in [[project-execution-stack]].",
            ])
            home_files[f"{mem_rel_base}/feedback_guardrails.md"] = "\n".join(fb_lines) + "\n"
            index_entries.append(
                f"- [Guardrails & Negative Memory](feedback_guardrails.md) — {len(chk.negative_memories)} known dead-ends & {len(chk.user_deferrals)} user deferral locks"
            )

        # 3. project_execution_stack.md (metadata.type: project)
        stack = chk.execution_stack
        exec_lines = [
            "---",
            "name: project-execution-stack",
            f"description: Current task goal, modified files, verified commands, and DONE/NOT_DONE step locks for {chk.repo_name}",
            "type: project",
            "metadata:",
            "  type: project",
            "---",
            "",
            f"# Execution Stack (`{chk.checkpoint_id}`)",
            f"- **Active Goal**: {stack.active_goal}",
            f"- **Current Status**: {stack.current_hypothesis}",
            f"- **Modified Files**: {', '.join(f'`{m}`' for m in stack.modified_files) or 'None'}",
            "",
            "## Verified Commands",
        ]
        for cmd in stack.passing_test_commands:
            exec_lines.append(f"- `{adapt_runtime_command(cmd, target_host)}`")
        exec_lines.extend(["", "## Task Step Locks"])
        for s in stack.steps:
            exec_lines.append(f"- **[{s.status}]** `{s.step_id}`: {s.title}")
            if s.verification_cmd:
                exec_lines.append(f"  - Verify: `{adapt_runtime_command(s.verification_cmd, target_host)}`")
        exec_lines.extend([
            "",
            "**Why:** Tracks completed milestones vs pending work across agent handoffs.",
            "**How to apply:** Do not re-run `[DONE]` steps; resume directly at the first `[PARTIALLY_DONE]` or `[NOT_DONE]` step using [[project-architecture]] and [[feedback-guardrails]].",
        ])
        home_files[f"{mem_rel_base}/project_execution_stack.md"] = "\n".join(exec_lines) + "\n"
        index_entries.append(
            f"- [Active Execution Stack](project_execution_stack.md) — Goal, {len(stack.modified_files)} files, and {len(stack.steps)} locked task steps"
        )

        # 4. Reference topic files (metadata.type: reference)
        for topic in chk.topic_memories:
            ref_name = f"reference_{topic.topic_slug}.md"
            kebab_slug = topic.topic_slug.replace("_", "-")
            ref_md = (
                "---\n"
                f"name: reference-{kebab_slug}\n"
                f"description: {topic.summary[:160]}\n"
                "type: reference\n"
                "metadata:\n"
                "  type: reference\n"
                "---\n\n"
                f"{topic.content.strip()}\n"
            )
            home_files[f"{mem_rel_base}/{ref_name}"] = ref_md
            index_entries.append(f"- [{topic.title}]({ref_name}) — {topic.summary[:100]}")

        # 5. MEMORY.md pure index (strictly <= 200 lines & <= 25 KB)
        mem_index_lines = [
            f"# {chk.repo_name} — Claude Code Auto-Memory Index",
            "## Memory Files",
            *index_entries,
            "",
            "## Quick Resumption Pointer",
            f"- Full Handoff State: `{target_host.workspace_root}/.agent-context/HANDOFF.md`",
            f"- Target Environment: `{target_host.os_name}/{target_host.arch}` (GPU: `{target_host.gpu_device}`)",
        ]
        capped_index = mem_index_lines[:195]
        home_files[f"{mem_rel_base}/MEMORY.md"] = "\n".join(capped_index) + "\n"

        workspace_files = {
            ".agent-context/HANDOFF.md": base_pkg.workspace_files[".agent-context/HANDOFF.md"],
            ".agent-context/CHAT_HISTORY.md": base_pkg.workspace_files.get(".agent-context/CHAT_HISTORY.md", ""),
            "CLAUDE.md": base_pkg.workspace_files["CLAUDE.md"],
        }
        for k, v in base_pkg.workspace_files.items():
            if k.startswith(".claude/rules/"):
                workspace_files[k] = v

        return CompiledMemoryPackage(
            checkpoint_id=chk.checkpoint_id,
            target_agent=AgentType.CLAUDE_CODE.value,
            target_model=target_model,
            target_host=target_host,
            include_private=include_private,
            workspace_files=workspace_files,
            home_files=home_files,
            compilation_notes=[
                f"Encoded Claude project key: `{encoded_key}`",
                f"Generated {len(home_files)} auto-memory files under `~/{mem_rel_base}/` (MEMORY.md: {len(capped_index)}/200 lines).",
            ],
        )

    def _register_claude_project_and_session(
        self,
        checkpoint: MemoryCheckpointIR,
        pkg: CompiledMemoryPackage,
        ws_resolved: Path,
        home_resolved: Path,
        encoded_key: str,
        target_model: str,
    ) -> Tuple[str, List[str], List[str]]:
        """Register the project in `~/.claude.json`, synthesize a resumable session `.jsonl` in
        `~/.claude/projects/<encoded_key>/<session_id>.jsonl`, and record it in `~/.claude/history.jsonl`.
        """
        written: List[str] = []
        ops: List[str] = []

        session_id = _deterministic_uuid(f"memgit-session|{checkpoint.checkpoint_id}|{ws_resolved}")
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        epoch_ms = int(time.time() * 1000)

        proj_dir = home_resolved / ".claude" / "projects" / encoded_key
        proj_dir.mkdir(parents=True, exist_ok=True)

        # Remove any broken apiErrorStatus=401 session transcripts so `claude --continue` always picks the clean session
        for old_jsonl in proj_dir.glob("*.jsonl"):
            if old_jsonl.name != f"{session_id}.jsonl":
                try:
                    raw_old = old_jsonl.read_text(encoding="utf-8")
                    if '"apiErrorStatus": 401' in raw_old or '"apiErrorStatus":401' in raw_old:
                        old_jsonl.unlink()
                        ops.append(f"Cleaned up failed auth session transcript {old_jsonl.name}")
                except OSError:
                    pass

        # 1. Synthesize native Claude Code multi-turn session transcript `<session_id>.jsonl`
        next_steps = [
            s for s in checkpoint.execution_stack.steps
            if s.status in (TaskLockStatus.PARTIALLY_DONE.value, TaskLockStatus.NOT_DONE.value)
        ]
        next_step_str = (
            f"`{next_steps[0].step_id}` — {next_steps[0].title}"
            if next_steps
            else "All steps marked DONE; ready for new instructions."
        )
        asst_summary = (
            f"**[MemGit Checkpoint `{checkpoint.checkpoint_id}` Resumed in Claude]**\n\n"
            f"- **Source Agent / Model**: `{checkpoint.source_agent}` (`{checkpoint.source_model}`)\n"
            f"- **Active Goal**: {checkpoint.execution_stack.active_goal}\n"
            f"- **Preserved Conversation Turns**: `{len(checkpoint.conversation_history)}` turns imported from source session (raw `<thinking>` blocks stripped)\n"
            f"- **Verified Architectural Facts**: {len(checkpoint.confirmed_facts)} loaded into `MEMORY.md` & `project_architecture.md`\n"
            f"- **Negative Memory Guardrails**: {len(checkpoint.negative_memories)} active in `feedback_guardrails.md`\n"
            f"- **Modified Workspace Files**: {', '.join(f'`{m}`' for m in checkpoint.execution_stack.modified_files) or 'Clean'}\n"
            f"- **Immediate Next Step**: {next_step_str}\n\n"
            "You can continue the conversation seamlessly from here."
        )
        title = self._resolve_conversation_title(checkpoint)
        user_prompt_text = f"{title} — Resume session from {checkpoint.source_agent} ({checkpoint.source_model})."

        # Transform source conversation_history into strictly alternating user <-> assistant pairs
        # with unbroken parentUuid chain (no intermediate attachment nodes!)
        paired_turns: List[Tuple[str, str, str]] = []  # (role, text, iso_ts)
        for turn in checkpoint.conversation_history:
            role = "user" if turn.role == "user" else "assistant"
            txt = turn.content.strip()
            if not txt:
                continue
            if turn.tool_summary and role == "assistant":
                txt = f"*(Executed: {turn.tool_summary})*\n\n{txt}"
            ts = turn.timestamp if turn.timestamp else now_iso
            if paired_turns and paired_turns[-1][0] == role:
                # Merge consecutive same-role turns so user -> assistant alternation is strict
                prev_role, prev_txt, _ = paired_turns[-1]
                paired_turns[-1] = (prev_role, f"{prev_txt}\n\n---\n\n{txt}", ts)
            else:
                paired_turns.append((role, txt, ts))

        # Ensure conversation starts with a user turn
        if not paired_turns or paired_turns[0][0] != "user":
            paired_turns.insert(0, ("user", user_prompt_text, now_iso))

        # Append final MemGit handoff status exchange or merge into last turn
        if paired_turns[-1][0] == "assistant":
            paired_turns.append(("user", user_prompt_text, now_iso))
            paired_turns.append(("assistant", asst_summary, now_iso))
        else:
            paired_turns.append(("assistant", asst_summary, now_iso))

        records: List[Dict[str, Any]] = []
        prev_uuid: Optional[str] = None
        last_user_text: str = user_prompt_text
        for idx, (role, txt, ts) in enumerate(paired_turns):
            msg_uuid = _deterministic_uuid(f"turn-{idx}|{role}|{session_id}|{checkpoint.checkpoint_id}")
            if role == "user":
                last_user_text = txt
                prompt_uuid = _deterministic_uuid(f"prompt-{idx}|{session_id}")
                rec: Dict[str, Any] = {
                    "parentUuid": prev_uuid,
                    "isSidechain": False,
                    "promptId": prompt_uuid,
                    "type": "user",
                    "uuid": msg_uuid,
                    "session_id": session_id,
                    "sessionId": session_id,
                    "parent_tool_use_id": None,
                    "timestamp": ts,
                    "permissionMode": "default",
                    "userType": "external",
                    "entrypoint": "cli",
                    "client_platform": "desktop_app",
                    "cwd": str(ws_resolved),
                    "version": "2.1.263",
                    "gitBranch": checkpoint.branch,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": txt}],
                    },
                }
            else:
                rec = {
                    "parentUuid": prev_uuid,
                    "isSidechain": False,
                    "type": "assistant",
                    "uuid": msg_uuid,
                    "session_id": session_id,
                    "sessionId": session_id,
                    "parent_tool_use_id": None,
                    "timestamp": ts,
                    "userType": "external",
                    "entrypoint": "cli",
                    "cwd": str(ws_resolved),
                    "version": "2.1.263",
                    "gitBranch": checkpoint.branch,
                    "message": {
                        "id": f"msg_{checkpoint.checkpoint_id}_{idx}",
                        "model": target_model,
                        "role": "assistant",
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "type": "message",
                        "usage": {
                            "input_tokens": 512,
                            "output_tokens": max(64, len(txt) // 4),
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                        "content": [{"type": "text", "text": txt}],
                    },
                }
            records.append(rec)
            prev_uuid = msg_uuid

        records.append({
            "type": "last-prompt",
            "lastPrompt": last_user_text[:400],
            "leafUuid": prev_uuid,
            "sessionId": session_id,
            "session_id": session_id,
        })

        session_file = proj_dir / f"{session_id}.jsonl"
        session_file.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        written.append(str(session_file))
        ops.append(f"Synthesized resumable Claude session transcript `{session_file.name}`")

        # 2. Register project in `~/.claude.json` with `hasTrustDialogAccepted: true` and `lastSessionId`
        claude_json_path = home_resolved / ".claude.json"
        claude_cfg: Dict[str, Any] = {}
        if claude_json_path.exists():
            try:
                claude_cfg = json.loads(claude_json_path.read_text(encoding="utf-8"))
            except Exception:
                claude_cfg = {}

        projects_map = claude_cfg.setdefault("projects", {})
        ws_key = str(ws_resolved)
        proj_entry = projects_map.get(ws_key, {})
        proj_entry.update({
            "allowedTools": proj_entry.get("allowedTools", []),
            "mcpContextUris": proj_entry.get("mcpContextUris", []),
            "mcpServers": proj_entry.get("mcpServers", {}),
            "enabledMcpjsonServers": proj_entry.get("enabledMcpjsonServers", []),
            "disabledMcpjsonServers": proj_entry.get("disabledMcpjsonServers", []),
            "hasTrustDialogAccepted": True,
            "hasClaudeMdExternalIncludesApproved": True,
            "hasClaudeMdExternalIncludesWarningShown": True,
            "lastGracefulShutdown": True,
            "lastVersionBase": "2.1.263",
            "lastStartTime": epoch_ms,
            "lastSessionId": session_id,
        })
        projects_map[ws_key] = proj_entry
        claude_json_path.write_text(json.dumps(claude_cfg, indent=2) + "\n", encoding="utf-8")
        written.append(str(claude_json_path))
        ops.append(
            f"Registered project `{ws_key}` in `~/.claude.json` (hasTrustDialogAccepted=true, lastSessionId={session_id})"
        )

        # 3. Append session entry to `~/.claude/history.jsonl` (if not already recorded for this sessionId)
        history_path = home_resolved / ".claude" / "history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        existing_lines = (
            history_path.read_text(encoding="utf-8").splitlines()
            if history_path.exists()
            else []
        )
        already_in_history = any(f'"{session_id}"' in ln for ln in existing_lines)
        if not already_in_history:
            hist_entry = {
                "display": user_prompt_text,
                "pastedContents": {},
                "timestamp": epoch_ms,
                "project": ws_key,
                "sessionId": session_id,
            }
            with history_path.open("a", encoding="utf-8") as hf:
                hf.write(json.dumps(hist_entry) + "\n")
            written.append(str(history_path))
            ops.append(f"Registered session `{session_id}` in `~/.claude/history.jsonl`")

        # 4. Also register in Claude Desktop App (`~/Library/Application Support/Claude/`) if present
        desktop_written, desktop_ops = self._register_claude_desktop_app_stores(
            checkpoint=checkpoint,
            pkg=pkg,
            ws_resolved=ws_resolved,
            home_resolved=home_resolved,
            encoded_key=encoded_key,
            session_id=session_id,
            records=records,
            target_model=target_model,
            epoch_ms=epoch_ms,
        )
        written.extend(desktop_written)
        ops.extend(desktop_ops)

        return session_id, written, ops

    def _register_claude_desktop_app_stores(
        self,
        checkpoint: MemoryCheckpointIR,
        pkg: CompiledMemoryPackage,
        ws_resolved: Path,
        home_resolved: Path,
        encoded_key: str,
        session_id: str,
        records: List[Dict[str, Any]],
        target_model: str,
        epoch_ms: int,
    ) -> Tuple[List[str], List[str]]:
        """Register the imported project and session inside macOS Claude Desktop App (`Claude.app`) stores:
        1. `local-agent-mode-sessions/<accountUuid>/<orgUuid>/spaces.json` + `spaces/<space_id>/memory/` (Projects tab)
        2. `claude-code-sessions/<accountUuid>/<orgUuid>/local_<session_id>.json` (Code tab)
        3. `local-agent-mode-sessions/<accountUuid>/<orgUuid>/local_<cowork_uuid>.json` (Chats / Cowork / Tasks tab)
        """
        written: List[str] = []
        ops: List[str] = []

        app_support = home_resolved / "Library" / "Application Support" / "Claude"
        if not app_support.exists():
            return written, ops

        code_root = app_support / "claude-code-sessions"
        lasm_root = app_support / "local-agent-mode-sessions"
        if not code_root.exists() and not lasm_root.exists():
            return written, ops

        # Discover active (accountUuid, orgUuid) pairs
        tenancies: List[Tuple[str, str]] = []
        uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
        for root in (lasm_root, code_root):
            if not root.exists():
                continue
            for acct_dir in sorted(root.iterdir()):
                if not acct_dir.is_dir() or not uuid_re.match(acct_dir.name):
                    continue
                for org_dir in sorted(acct_dir.iterdir()):
                    if not org_dir.is_dir() or not uuid_re.match(org_dir.name):
                        continue
                    pair = (acct_dir.name, org_dir.name)
                    if pair not in tenancies:
                        tenancies.append(pair)

        if not tenancies:
            return written, ops

        space_id = _deterministic_uuid(f"memgit-space|{checkpoint.repo_name}|{ws_resolved}")
        cowork_uuid = _deterministic_uuid(f"memgit-cowork-session|{checkpoint.checkpoint_id}|{ws_resolved}")
        cowork_cli_uuid = _deterministic_uuid(f"memgit-cowork-cli|{checkpoint.checkpoint_id}|{ws_resolved}")
        ws_str = str(ws_resolved)
        title_str = self._resolve_conversation_title(checkpoint)
        claude_md_content = pkg.workspace_files.get("CLAUDE.md", "")

        for acct_id, org_id in tenancies:
            # A. Register Project Space in `local-agent-mode-sessions/<acct>/<org>/spaces.json`
            lasm_tenancy_dir = lasm_root / acct_id / org_id
            lasm_tenancy_dir.mkdir(parents=True, exist_ok=True)
            spaces_json_path = lasm_tenancy_dir / "spaces.json"
            spaces_list: List[Dict[str, Any]] = []
            if spaces_json_path.exists():
                try:
                    existing_spaces_obj = json.loads(spaces_json_path.read_text(encoding="utf-8"))
                    if isinstance(existing_spaces_obj.get("spaces"), list):
                        spaces_list = [
                            s for s in existing_spaces_obj["spaces"]
                            if isinstance(s, dict) and s.get("id") != space_id
                        ]
                except Exception:
                    spaces_list = []

            space_entry = {
                "id": space_id,
                "name": f"{checkpoint.repo_name} ({checkpoint.checkpoint_id})",
                "description": (
                    f"MemGit checkpoint {checkpoint.checkpoint_id} imported from "
                    f"{checkpoint.source_agent} ({checkpoint.source_model}). "
                    f"Goal: {checkpoint.execution_stack.active_goal[:240]}"
                ),
                "folders": [{"path": ws_str}],
                "projects": [],
                "links": [],
                "instructions": claude_md_content,
                "ccdFolderPath": ws_str,
                "origin": "user",
                "autoMountFolders": True,
                "createdAt": epoch_ms,
                "updatedAt": epoch_ms,
            }
            spaces_list.insert(0, space_entry)
            spaces_json_path.write_text(
                json.dumps({"spaces": spaces_list}, indent=2) + "\n",
                encoding="utf-8",
            )
            written.append(str(spaces_json_path))

            # Write spaces-present marker (`spaces-present/<acct[:8]>/<org[:8]>/present`)
            present_marker = app_support / "spaces-present" / acct_id[:8] / org_id[:8] / "present"
            present_marker.parent.mkdir(parents=True, exist_ok=True)
            present_marker.write_text("1\n", encoding="utf-8")

            # Write Space Auto-Memory (`spaces/<space_id>/memory/*`)
            space_mem_dir = lasm_tenancy_dir / "spaces" / space_id / "memory"
            space_mem_dir.mkdir(parents=True, exist_ok=True)
            mem_prefix = f".claude/projects/{encoded_key}/memory/"
            for rel_p, content in pkg.home_files.items():
                if rel_p.startswith(mem_prefix):
                    fname = rel_p[len(mem_prefix):]
                    target_f = space_mem_dir / fname
                    target_f.parent.mkdir(parents=True, exist_ok=True)
                    target_f.write_text(content, encoding="utf-8")
            written.append(str(space_mem_dir / "MEMORY.md"))
            ops.append(f"Registered Claude Desktop Project Space `{space_entry['name']}` ({space_id}) in `spaces.json`")

            # B. Register Code Tab Session in `claude-code-sessions/<acct>/<org>/local_<session_id>.json`
            code_tenancy_dir = code_root / acct_id / org_id
            code_tenancy_dir.mkdir(parents=True, exist_ok=True)
            code_session_key = f"local_{session_id}"
            code_record = {
                "sessionId": code_session_key,
                "cliSessionId": session_id,
                "cwd": ws_str,
                "originCwd": ws_str,
                "createdAt": epoch_ms,
                "lastActivityAt": epoch_ms,
                "lastFocusedAt": epoch_ms,
                "indexedAt": epoch_ms,
                "isArchived": False,
                "title": title_str,
                "titleSource": "user",
                "permissionMode": "default",
                "model": target_model,
                "spaceId": space_id,
                "adoptedFromOtherSurface": True,
                "fromCliTranscript": True,
                "alwaysAllowedReasons": [],
                "sessionPermissionUpdates": [],
            }
            code_record_path = code_tenancy_dir / f"{code_session_key}.json"
            code_record_path.write_text(json.dumps(code_record, indent=2) + "\n", encoding="utf-8")
            written.append(str(code_record_path))
            ops.append(f"Registered Claude Desktop Code Session `{code_session_key}.json`")

            # C. Register Cowork / Tasks / Chats Tab Session in `local-agent-mode-sessions/<acct>/<org>/local_<cowork_uuid>.json`
            cowork_session_key = f"local_{cowork_uuid}"
            cowork_short_dir = lasm_tenancy_dir / cowork_uuid[:8]
            cowork_outputs_dir = cowork_short_dir / "outputs"
            cowork_proj_dir = cowork_short_dir / ".claude" / "projects" / encoded_key
            cowork_outputs_dir.mkdir(parents=True, exist_ok=True)
            cowork_proj_dir.mkdir(parents=True, exist_ok=True)

            # Write cowork CLI transcript `<cowork_cli_uuid>.jsonl`
            cowork_records = []
            for r in records:
                rc = dict(r)
                rc["sessionId"] = cowork_cli_uuid
                rc["session_id"] = cowork_cli_uuid
                cowork_records.append(rc)
            # Clean any stale jsonl in cowork_proj_dir
            for stale_jsonl in cowork_proj_dir.glob("*.jsonl"):
                if stale_jsonl.name != f"{cowork_cli_uuid}.jsonl":
                    try:
                        stale_jsonl.unlink()
                    except OSError:
                        pass
            cowork_transcript_path = cowork_proj_dir / f"{cowork_cli_uuid}.jsonl"
            cowork_transcript_path.write_text(
                "\n".join(json.dumps(r) for r in cowork_records) + "\n",
                encoding="utf-8",
            )

            cowork_record = {
                "sessionId": cowork_session_key,
                "cliSessionId": cowork_cli_uuid,
                "processName": f"memgit-{checkpoint.repo_name.lower().replace('_', '-')}",
                "vmProcessName": f"memgit-{checkpoint.repo_name.lower().replace('_', '-')}",
                "cwd": str(cowork_outputs_dir),
                "hostLoopMode": True,
                "title": title_str,
                "spaceId": space_id,
                "spaceIdSetBy": "user",
                "createdAt": epoch_ms,
                "lastActivityAt": epoch_ms,
                "isArchived": False,
                "userSelectedFolders": [ws_str],
                "folderMountNames": {},
                "resolvedFolderKinds": [
                    {"kind": "local", "display": ws_str, "canonical": ws_str}
                ],
                "pendingNotifications": [],
            }
            cowork_record_path = lasm_tenancy_dir / f"{cowork_session_key}.json"
            cowork_record_path.write_text(json.dumps(cowork_record, indent=2) + "\n", encoding="utf-8")
            written.append(str(cowork_record_path))
            ops.append(f"Registered Claude Desktop Cowork/Chat Session `{cowork_session_key}.json`")

        return written, ops

    def _convert_for_codex_cli(
        self,
        raw_checkpoint: MemoryCheckpointIR,
        target_model: str,
        target_host: HostFingerprint,
        include_private: bool,
    ) -> CompiledMemoryPackage:
        """Build authentic OpenAI Codex CLI memory files with strict 32 KiB cascading AGENTS.md budget."""
        pkg = compile_checkpoint(
            raw_checkpoint=raw_checkpoint,
            target_agent=AgentType.CODEX_CLI.value,
            target_model=target_model,
            target_host=target_host,
            include_private=include_private,
        )
        for k in list(pkg.workspace_files.keys()):
            pkg.workspace_files[k] = adapt_runtime_command(pkg.workspace_files[k], target_host)
        for k in list(pkg.home_files.keys()):
            pkg.home_files[k] = adapt_runtime_command(pkg.home_files[k], target_host)

        global_bytes = len(pkg.home_files.get(".codex/AGENTS.md", "").encode("utf-8"))
        repo_agents = pkg.workspace_files.get("AGENTS.md", "")
        max_repo_bytes = CODEX_TOTAL_AGENTS_BUDGET_BYTES - global_bytes - 256
        if len(repo_agents.encode("utf-8")) > max_repo_bytes:
            repo_agents = (
                repo_agents.encode("utf-8")[:max_repo_bytes].decode("utf-8", errors="ignore")
                + "\n...(trimmed to fit Codex 32 KiB cascading AGENTS.md budget)\n"
            )
            pkg.workspace_files["AGENTS.md"] = repo_agents

        total_bytes = global_bytes + len(pkg.workspace_files.get("AGENTS.md", "").encode("utf-8"))
        pkg.compilation_notes.append(
            f"Verified Codex CLI cascading AGENTS.md total: {total_bytes}/{CODEX_TOTAL_AGENTS_BUDGET_BYTES} bytes."
        )
        return pkg

    @staticmethod
    def _resolve_conversation_title(checkpoint: MemoryCheckpointIR) -> str:
        """Return the original conversation title from the source agent memory without any MemGit prefix."""
        generic = {"memgit checkpoint", "general development", ""}
        for candidate in (
            checkpoint.message,
            checkpoint.execution_stack.active_goal,
            checkpoint.repo_name,
        ):
            text = (candidate or "").strip()
            if text and text.lower() not in generic:
                return text
        return checkpoint.repo_name or checkpoint.checkpoint_id

    def _build_bootstrap_prompt(
        self,
        checkpoint: MemoryCheckpointIR,
        target_agent: str,
    ) -> str:
        """Build a concise startup prompt for the destination CLI (`agy` or `gemini`)
        starting with the original source conversation title (no MemGit prefix) so the
        app sidebar displays the original conversation title cleanly."""
        title = self._resolve_conversation_title(checkpoint)
        turn_count = len(checkpoint.conversation_history)
        agent_norm = target_agent.lower().strip()
        extra_history = (
            f"~/.gemini/antigravity/brain/{checkpoint.checkpoint_id}/chat_history.md"
            if agent_norm in (AgentType.ANTIGRAVITY.value, "agy")
            else "~/.gemini/memories/chat_history.md"
        )
        return (
            f"{title}\n\n"
            f"Read GEMINI.md, .agent-context/HANDOFF.md, and .agent-context/CHAT_HISTORY.md "
            f"(also mirrored at {extra_history}) which contain {turn_count} restored conversation turns "
            f"from {checkpoint.source_agent}/{checkpoint.source_model} (checkpoint {checkpoint.checkpoint_id}). "
            f"In your reply, display a clean 'Restored Conversation History' section listing each previous "
            f"User and Assistant turn concisely so the user can see the resumed chat history directly in the UI, "
            f"followed by 'Current State & Next Step', and then pause for the user's next instruction."
        )

    # Thin backwards-compatible delegators to modular `memgit.sync` helpers
    _backup_sqlite_if_nonempty = staticmethod(backup_sqlite_if_nonempty)
    _pb_encode_varint = staticmethod(pb_encode_varint)
    _pb_read_varint = staticmethod(pb_read_varint)
    _pb_iter_fields = staticmethod(pb_iter_fields)
    _pb_encode_bytes_field = staticmethod(pb_encode_bytes_field)
    _pb_encode_varint_field = staticmethod(pb_encode_varint_field)
    _pb_patch_trajectory_metadata_standalone = staticmethod(pb_patch_trajectory_metadata_standalone)
    _pb_patch_raw_summary_standalone = staticmethod(pb_patch_raw_summary_standalone)
    _sync_agyhub_summaries_proto = staticmethod(sync_agyhub_summaries_proto)
    _patch_trajectory_db_standalone = staticmethod(patch_trajectory_db_standalone)

    def _sync_antigravity_cli_session_to_app(
        self,
        home_resolved: Path,
        before_convs: Dict[str, float],
        conversation_title: Optional[str] = None,
        ws_resolved: Optional[Path] = None,
        create_project: bool = False,
        default_cli_proj_existed_before: bool = True,
        backup_tx: Optional[BackupTransaction] = None,
        warnings_out: Optional[List[str]] = None,
    ) -> Tuple[Optional[str], List[str], int, Optional[str]]:
        return sync_antigravity_cli_session_to_app(
            home_resolved=home_resolved,
            before_convs=before_convs,
            conversation_title=conversation_title,
            ws_resolved=ws_resolved,
            create_project=create_project,
            default_cli_proj_existed_before=default_cli_proj_existed_before,
            backup_tx=backup_tx,
            warnings_out=warnings_out,
        )

    def _bootstrap_target_cli_session(
        self,
        checkpoint: MemoryCheckpointIR,
        target_agent: str,
        ws_resolved: Path,
        home_resolved: Path,
        bootstrap_bin: Optional[str] = None,
        timeout_s: int = 120,
        create_project: bool = False,
        backup_tx: Optional[BackupTransaction] = None,
        warnings_out: Optional[List[str]] = None,
    ) -> BootstrapSessionReport:
        """Automatically invoke the destination agent's official CLI to create a native
        resumable session so reopening/refreshing the app shows the resumed conversation:
        - Antigravity App (`antigravity` / `agy`) -> `agy -p "<prompt>"`
        - Gemini App / CLI (`gemini-cli` / `gemini` / `gemini-app`) -> `gemini -p "<prompt>"`
        """
        agent_norm = target_agent.lower().strip()
        if agent_norm in (AgentType.ANTIGRAVITY.value, "agy"):
            cli_name = "agy"
            canonical_agent = AgentType.ANTIGRAVITY.value
        elif agent_norm in (AgentType.GEMINI_CLI.value, "gemini", "gemini-app"):
            cli_name = "gemini"
            canonical_agent = AgentType.GEMINI_CLI.value
        else:
            return BootstrapSessionReport(
                agent=agent_norm,
                cli_binary="",
                executed=False,
                succeeded=False,
                note=f"CLI bootstrap is not configured for target agent '{target_agent}'.",
            )

        prompt = self._build_bootstrap_prompt(checkpoint, canonical_agent)
        resolved_bin = bootstrap_bin or shutil.which(cli_name)
        if not resolved_bin:
            local_cand = home_resolved / ".local" / "bin" / cli_name
            if local_cand.exists() and os.access(local_cand, os.X_OK):
                resolved_bin = str(local_cand)
        if not resolved_bin:
            return BootstrapSessionReport(
                agent=canonical_agent,
                cli_binary=cli_name,
                command=[cli_name, "-p", prompt],
                executed=False,
                succeeded=False,
                note=(
                    f"CLI binary '{cli_name}' was not found on PATH. "
                    f"Memory & chat history files are installed; install '{cli_name}' and run "
                    f"`cd {ws_resolved} && {cli_name} -p \"...\"` to seed a native session."
                ),
            )

        if canonical_agent == AgentType.ANTIGRAVITY.value:
            if create_project:
                cmd = [resolved_bin, "--new-project", "-p", prompt]
            else:
                # Pass --project outside-of-project so agy attaches the session directly to
                # Antigravity's top-level Conversations section without creating "CLI Project".
                cmd = [resolved_bin, "--project", "outside-of-project", "-p", prompt]
        else:
            cmd = [resolved_bin, "-p", prompt]
        env = os.environ.copy()
        env["HOME"] = str(home_resolved)
        env["MEMGIT_CHECKPOINT_ID"] = checkpoint.checkpoint_id
        env["MEMGIT_TARGET_AGENT"] = canonical_agent

        before_convs: Dict[str, float] = {}
        default_cli_proj_existed_before = True
        if canonical_agent == AgentType.ANTIGRAVITY.value:
            cli_conv_dir = home_resolved / ".gemini" / "antigravity-cli" / "conversations"
            if cli_conv_dir.exists():
                for pattern in ("*.db", "*.pb"):
                    for fpath in cli_conv_dir.glob(pattern):
                        before_convs[fpath.name] = fpath.stat().st_mtime
            default_cli_proj_existed_before = (
                home_resolved / ".gemini" / "config" / "projects" / "default-cli-project.json"
            ).exists()

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ws_resolved),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if (
                proc.returncode != 0
                and canonical_agent == AgentType.ANTIGRAVITY.value
                and not create_project
                and "--project" in cmd
            ):
                err_text = f"{proc.stderr or ''} {proc.stdout or ''}".lower()
                if "project" in err_text or "not found" in err_text or "outside-of-project" in err_text:
                    cmd = [resolved_bin, "-p", prompt]
                    proc = subprocess.run(
                        cmd,
                        cwd=str(ws_resolved),
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                    )
            ok = proc.returncode == 0
            conv_id: Optional[str] = None
            project_id: Optional[str] = None
            synced_files: List[str] = []
            synced_steps: Optional[int] = None
            if ok and canonical_agent == AgentType.ANTIGRAVITY.value:
                conv_id, synced_files, synced_steps, project_id = self._sync_antigravity_cli_session_to_app(
                    home_resolved=home_resolved,
                    before_convs=before_convs,
                    conversation_title=self._resolve_conversation_title(checkpoint),
                    ws_resolved=ws_resolved,
                    create_project=create_project,
                    default_cli_proj_existed_before=default_cli_proj_existed_before,
                    backup_tx=backup_tx,
                    warnings_out=warnings_out,
                )
            sync_note = (
                f" Synced session {conv_id} ({synced_steps} SQLite steps, project={project_id or 'none'}) into ~/.gemini/antigravity/ for Desktop App sidebar."
                if conv_id and synced_files and (synced_steps or 0) > 0
                else (
                    f" Synced session {conv_id} (.pb fallback) into ~/.gemini/antigravity/."
                    if conv_id and synced_files
                    else ""
                )
            )
            return BootstrapSessionReport(
                agent=canonical_agent,
                cli_binary=resolved_bin,
                command=cmd,
                executed=True,
                succeeded=ok,
                return_code=proc.returncode,
                conversation_id=conv_id,
                conversation_url=f"conversation://{conv_id}" if conv_id else None,
                project_id=project_id,
                synced_steps=synced_steps,
                synced_files=synced_files,
                stdout_preview=(proc.stdout or "")[:500].strip(),
                stderr_preview=(proc.stderr or "")[:500].strip(),
                note=(
                    f"Bootstrapped native {canonical_agent} session via '{cli_name}' CLI.{sync_note}"
                    if ok
                    else f"'{cli_name}' CLI exited with code {proc.returncode}."
                ),
            )
        except subprocess.TimeoutExpired:
            return BootstrapSessionReport(
                agent=canonical_agent,
                cli_binary=resolved_bin,
                command=cmd,
                executed=True,
                succeeded=False,
                note=f"'{cli_name}' CLI timed out after {timeout_s}s.",
            )
        except OSError as exc:
            return BootstrapSessionReport(
                agent=canonical_agent,
                cli_binary=resolved_bin,
                command=cmd,
                executed=False,
                succeeded=False,
                note=f"Failed to execute '{cli_name}' CLI: {exc}",
            )

    def convert_and_install(
        self,
        checkpoint: MemoryCheckpointIR,
        target_agent: str,
        target_model: str,
        workspace_root: Path,
        user_home: Path,
        target_host: Optional[HostFingerprint] = None,
        include_private: bool = True,
        verify: bool = True,
        bootstrap_session: bool = False,
        bootstrap_bin: Optional[str] = None,
        bootstrap_timeout_s: int = 120,
        create_project: bool = False,
        dry_run: bool = False,
        enable_backup: bool = True,
    ) -> ConversionResult:
        """Convert a checkpoint, write it non-destructively to the target agent's folders,
        register the project & session so the app opens ready-to-use, and verify loading.
        """
        ws_resolved = workspace_root.resolve()
        home_resolved = user_home.resolve()
        if target_host is None:
            target_host = HostFingerprint(
                os_name=platform.system(),
                arch=platform.machine(),
                workspace_root=str(ws_resolved),
                user_home=str(home_resolved),
                shell="zsh" if platform.system() == "Darwin" else "bash",
                cuda_available=False,
                gpu_device="Apple M-Series MPS" if platform.system() == "Darwin" else "CPU",
                hostname=platform.node() or "localhost",
            )

        pkg = self.convert_to_package(
            raw_checkpoint=checkpoint,
            target_agent=target_agent,
            target_model=target_model,
            target_host=target_host,
            include_private=include_private,
        )

        is_claude = target_agent.lower() in ("claude", AgentType.CLAUDE_CODE.value)
        encoded_key = (
            encode_claude_project_key(ws_resolved)
            if is_claude
            else checkpoint.repo_name
        )

        written: List[str] = []
        ops = list(pkg.compilation_notes)
        tier2_warnings: List[str] = []
        tier2_status = "skipped"

        if dry_run:
            for rel_path in pkg.workspace_files:
                written.append(str(ws_resolved / rel_path))
            for rel_path in pkg.home_files:
                written.append(str(home_resolved / rel_path))
            ops.append(f"[DRY RUN] Would install {len(written)} Tier-1 file(s) for {target_agent} ({target_model}).")
            if bootstrap_session:
                ops.append(f"[DRY RUN] Would bootstrap native {target_agent} CLI session (create_project={create_project}).")
            return ConversionResult(
                checkpoint_id=checkpoint.checkpoint_id,
                source_agent=checkpoint.source_agent,
                source_model=checkpoint.source_model,
                target_agent=target_agent,
                target_model=target_model,
                encoded_project_key=encoded_key,
                session_id=None,
                workspace_root=str(ws_resolved),
                user_home=str(home_resolved),
                written_files=written,
                operations_performed=ops,
                boot_verification=None,
                bootstrap_session=None,
                tier1_succeeded=True,
                tier2_status="skipped",
                tier2_warnings=[],
                backup_id=None,
                dry_run=True,
            )

        backup_tx: Optional[BackupTransaction] = None
        if enable_backup:
            backup_tx = BackupManager(home_resolved).begin(
                operation="convert_and_install",
                metadata={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "target_agent": target_agent,
                    "target_model": target_model,
                    "workspace_root": str(ws_resolved),
                },
            )

        # Tier-1: Write workspace files (non-destructive delimited upsert for CLAUDE.md / AGENTS.md / GEMINI.md)
        for rel_path, content in pkg.workspace_files.items():
            dest = ws_resolved / rel_path
            if backup_tx is not None:
                backup_tx.snapshot(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rel_path in ("CLAUDE.md", "AGENTS.md", "GEMINI.md") and dest.exists():
                existing = dest.read_text(encoding="utf-8")
                final_text = upsert_delimited_section(existing, content)
                dest.write_text(final_text, encoding="utf-8")
                ops.append(f"Non-destructively upserted MemGit block into existing {dest}")
            else:
                if rel_path in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
                    content = upsert_delimited_section("", content)
                dest.write_text(content, encoding="utf-8")
            written.append(str(dest))

        # Tier-1: Write user home files (e.g. ~/.claude/projects/<encoded-key>/memory/* or ~/.codex/*)
        for rel_path, content in pkg.home_files.items():
            dest = home_resolved / rel_path
            if backup_tx is not None:
                backup_tx.snapshot(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            written.append(str(dest))

        session_id: Optional[str] = None
        if is_claude:
            if backup_tx is not None:
                backup_tx.snapshot(home_resolved / ".claude.json")
                backup_tx.snapshot(home_resolved / ".claude" / "history.jsonl")
            try:
                session_id, reg_written, reg_ops = self._register_claude_project_and_session(
                    checkpoint=checkpoint,
                    pkg=pkg,
                    ws_resolved=ws_resolved,
                    home_resolved=home_resolved,
                    encoded_key=encoded_key,
                    target_model=target_model,
                )
                written.extend(reg_written)
                ops.extend(reg_ops)
                tier2_status = "applied"
            except Exception as exc:
                tier2_status = "fallback"
                tier2_warnings.append(f"Claude Tier-2 session registration fell back to Tier-1 files: {exc}")

        bootstrap_report: Optional[BootstrapSessionReport] = None
        if bootstrap_session:
            bootstrap_report = self._bootstrap_target_cli_session(
                checkpoint=checkpoint,
                target_agent=target_agent,
                ws_resolved=ws_resolved,
                home_resolved=home_resolved,
                bootstrap_bin=bootstrap_bin,
                timeout_s=bootstrap_timeout_s,
                create_project=create_project,
                backup_tx=backup_tx,
                warnings_out=tier2_warnings,
            )
            ops.append(bootstrap_report.note)
            if bootstrap_report.synced_files:
                written.extend(bootstrap_report.synced_files)
                tier2_status = "fallback" if tier2_warnings else "applied"
            elif not bootstrap_report.succeeded:
                tier2_status = "fallback"
            if session_id is None and bootstrap_report.conversation_id:
                session_id = bootstrap_report.conversation_id

        backup_id: Optional[str] = None
        if backup_tx is not None:
            backup_tx.commit()
            backup_id = backup_tx.op_id

        boot_report: Optional[AgentBootSimulationReport] = None
        if verify:
            adapter = get_adapter(target_agent)
            forbidden_paths: List[str] = []
            src_ws = checkpoint.source_host.workspace_root.rstrip("/")
            if src_ws and src_ws != str(ws_resolved).rstrip("/"):
                forbidden_paths.append(src_ws)
            boot_report = adapter.simulate_boot_and_load(
                workspace_root=ws_resolved,
                user_home=home_resolved,
                repo_name=checkpoint.repo_name,
                model_name=target_model,
                forbidden_source_paths=forbidden_paths,
            )

        return ConversionResult(
            checkpoint_id=checkpoint.checkpoint_id,
            source_agent=checkpoint.source_agent,
            source_model=checkpoint.source_model,
            target_agent=target_agent,
            target_model=target_model,
            encoded_project_key=encoded_key,
            session_id=session_id,
            workspace_root=str(ws_resolved),
            user_home=str(home_resolved),
            written_files=written,
            operations_performed=ops,
            boot_verification=boot_report,
            bootstrap_session=bootstrap_report,
            tier1_succeeded=True,
            tier2_status=tier2_status,
            tier2_warnings=tier2_warnings,
            backup_id=backup_id,
            dry_run=False,
        )
