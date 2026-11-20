"""Cross-Model Cognitive Compiler (Memory IR -> Target Agent & Model Native Artifacts).

Transforms a provider-agnostic MemoryCheckpointIR into target-specific memory files,
adapting to:
1. Target Agent Harness (Claude Code, OpenAI Codex CLI, Google Antigravity, Cursor)
2. Target Model Profile (Claude, GPT/Codex, Gemini/Antigravity)
3. Target Host Environment Fingerprint (OS, arch, CUDA availability, workspace paths)
4. Privacy Sandboxing (M_shared vs M_private)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List

from memgit.models import (
    AgentType,
    HostFingerprint,
    MemoryCheckpointIR,
    ProvenanceNode,
    TaskLockStatus,
    VerificationStatus,
)
from memgit.sanitizer import filter_checkpoint_for_target


@dataclass
class CompiledMemoryPackage:
    """Compiled artifact bundle ready to be materialized on a target node."""
    checkpoint_id: str
    target_agent: str
    target_model: str
    target_host: HostFingerprint
    include_private: bool
    workspace_files: Dict[str, str] = field(default_factory=dict)  # rel to workspace_root
    home_files: Dict[str, str] = field(default_factory=dict)       # rel to user_home
    sqlite_memories: List[Dict[str, str]] = field(default_factory=list)  # For Cursor state.vscdb
    compilation_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "target_agent": self.target_agent,
            "target_model": self.target_model,
            "target_host": self.target_host.to_dict(),
            "include_private": self.include_private,
            "workspace_files": self.workspace_files,
            "home_files": self.home_files,
            "sqlite_memories": self.sqlite_memories,
            "compilation_notes": self.compilation_notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CompiledMemoryPackage:
        return cls(
            checkpoint_id=data["checkpoint_id"],
            target_agent=data["target_agent"],
            target_model=data["target_model"],
            target_host=HostFingerprint.from_dict(data["target_host"]),
            include_private=data.get("include_private", True),
            workspace_files=data.get("workspace_files", {}),
            home_files=data.get("home_files", {}),
            sqlite_memories=data.get("sqlite_memories", []),
            compilation_notes=data.get("compilation_notes", []),
        )


def _active_facts(chk: MemoryCheckpointIR) -> List[ProvenanceNode]:
    return [
        f for f in chk.confirmed_facts
        if f.verification_status != VerificationStatus.SUPERSEDED.value
    ]


def _superseded_facts(chk: MemoryCheckpointIR) -> List[ProvenanceNode]:
    return [
        f for f in chk.confirmed_facts
        if f.verification_status == VerificationStatus.SUPERSEDED.value
    ]


def _model_cognitive_preamble(chk: MemoryCheckpointIR, target_model: str, target_host: HostFingerprint) -> str:
    """Generate model-specific cognitive adaptation header based on destination LLM family."""
    model_lower = target_model.lower()
    cuda_str = (
        f"ENABLED ({target_host.gpu_device})"
        if target_host.cuda_available
        else "DISABLED (CPU/MPS only)"
    )
    env_block = (
        f"- **Target OS / Arch**: `{target_host.os_name} ({target_host.arch})`\n"
        f"- **Target Workspace Root**: `{target_host.workspace_root}`\n"
        f"- **Target Shell**: `{target_host.shell}`\n"
        f"- **GPU / CUDA Acceleration**: `{cuda_str}`\n"
    )

    if "claude" in model_lower:
        return (
            "<!-- Model Profile: Anthropic Claude Cognitive Format -->\n"
            "<memgit_cognitive_handoff>\n"
            f"  <origin checkpoint=\"{chk.checkpoint_id}\" source_agent=\"{chk.source_agent}\" "
            f"source_model=\"{chk.source_model}\" />\n"
            "  <reasoning_directive>\n"
            "    Prior model thinking blocks were stripped to prevent signature incompatibility.\n"
            "    Initialize a fresh <thinking> chain grounded strictly in the Declarative State below.\n"
            "  </reasoning_directive>\n"
            "</memgit_cognitive_handoff>\n\n"
            "### Target Host Environment Re-Hydration\n" + env_block
        )
    elif "codex" in model_lower or "gpt" in model_lower or "o3" in model_lower:
        return (
            "<!-- Model Profile: OpenAI Codex Execution-First Format -->\n"
            f"**MemGit Handoff**: Resumed from `{chk.checkpoint_id}` "
            f"(origin: `{chk.source_agent}` / `{chk.source_model}`).\n"
            "**Execution Directive**: Do not re-run COMPLETED (`[DONE]`) steps. "
            "Run verification commands directly in the target workspace before committing.\n\n"
            "### Target Host Environment Re-Hydration\n" + env_block
        )
    else:
        # Gemini / Antigravity default
        return (
            "<!-- Model Profile: Google Gemini / Antigravity Artifact-Centric Format -->\n"
            f"> [!IMPORTANT]\n"
            f"> **MemGit Cross-Model Handoff (`{chk.checkpoint_id}`)**: Transferred from "
            f"`{chk.source_agent}` (`{chk.source_model}`) to `{target_model}`.\n"
            f"> Raw thought signatures were compiled into Declarative State and Knowledge Items.\n\n"
            "### Target Host Environment Re-Hydration\n" + env_block
        )


def _render_handoff_markdown(
    chk: MemoryCheckpointIR,
    target_agent: str,
    target_model: str,
    target_host: HostFingerprint,
) -> str:
    """Render Pattern A `.agent-context/HANDOFF.md` with Pitfall 1-4 protections."""
    lines: List[str] = [
        f"# MemGit Handoff Snapshot (`{chk.checkpoint_id}`)",
        "",
        _model_cognitive_preamble(chk, target_model, target_host).strip(),
        "",
        "## 1. Confirmed Constraints & Architectural Decisions (`M_shared` + Active)",
    ]
    active = _active_facts(chk)
    if active:
        for f in active:
            scope_badge = "[PRIVATE]" if f.scope == "private" else "[SHARED]"
            ver_badge = f"({f.verification_status})"
            lines.append(
                f"- `{f.entity_key}` {scope_badge} {ver_badge}: {f.statement} "
                f"*(by {f.author} via {f.source_model})*"
            )
    else:
        lines.append("- None recorded.")

    superseded = _superseded_facts(chk)
    if superseded:
        lines.extend(["", "### Superseded / Obsolete Assumptions (DO NOT REVERT TO THESE)"])
        for sf in superseded:
            lines.append(
                f"- ~~`{sf.entity_key}`: {sf.statement}~~ -> **SUPERSEDED BY `{sf.superseded_by}`**"
            )

    lines.extend(["", "## 2. Negative Memory / Failed Paths (Do Not Repeat)"])
    if chk.negative_memories:
        for nm in chk.negative_memories:
            files_str = ", ".join(f"`{p}`" for p in nm.files_involved) if nm.files_involved else "general"
            lines.append(
                f"- **Failed Attempt (`{nm.attempt_id}`)**: {nm.hypothesis} ({files_str})\n"
                f"  - *Why it failed*: {nm.failure_reason}"
            )
    else:
        lines.append("- No failed paths recorded.")

    lines.extend([
        "",
        "## 3. Explicit User Deferrals (`userDeferrals` — FORBIDDEN UNLESS RE-AUTHORIZED)",
    ])
    if chk.user_deferrals:
        for ud in chk.user_deferrals:
            lines.append(
                f"- **[LOCKED DEFERRAL `{ud.deferral_id}`] {ud.feature_or_option}**: {ud.directive}\n"
                f"  - *Unlock condition*: {ud.reauthorize_condition}"
            )
    else:
        lines.append("- No active user deferrals.")

    stack = chk.execution_stack
    lines.extend([
        "",
        "## 4. Current Task Execution Stack",
        f"- **Active Goal**: {stack.active_goal}",
        f"- **Current Hypothesis**: {stack.current_hypothesis or 'N/A'}",
        f"- **Modified Files (WIP)**: {', '.join(f'`{m}`' for m in stack.modified_files) or 'Clean'}",
        f"- **Passing Tests**: {', '.join(f'`{c}`' for c in stack.passing_test_commands) or 'None'}",
        f"- **Failing Tests**: {', '.join(f'`{c}`' for c in stack.failing_test_commands) or 'None'}",
    ])
    if stack.stack_trace_digest:
        lines.extend([
            "- **Active Stack Trace / Error Digest**:",
            "```text",
            stack.stack_trace_digest.strip(),
            "```",
        ])

    lines.extend(["", "### Pending & Locked Action Plan"])
    for idx, step in enumerate(stack.steps, start=1):
        files_hint = f" (files: {', '.join(f'`{p}`' for p in step.relative_files)})" if step.relative_files else ""
        cmd_hint = f" — verify via `{step.verification_cmd}`" if step.verification_cmd else ""
        lines.append(f"{idx}. **[{step.status}]** `{step.step_id}`: {step.title}{files_hint}{cmd_hint}")
        if step.notes:
            lines.append(f"   - *Notes*: {step.notes}")

    if chk.conversation_history:
        lines.extend([
            "",
            f"## 5. Preserved Conversation History (`{len(chk.conversation_history)}` turns, Thinking Stripped)",
            "- Full portable transcript: `.agent-context/CHAT_HISTORY.md`",
        ])
        for idx, turn in enumerate(chk.conversation_history[-8:], start=1):
            role_label = "User" if turn.role == "user" else f"Assistant ({chk.source_model})"
            preview = turn.content.replace("\n", " ")[:220]
            lines.append(f"- **Turn {idx} [{role_label}]** (`{turn.timestamp}`): {preview}")

    return "\n".join(lines) + "\n"


def _render_chat_history_markdown(chk: MemoryCheckpointIR) -> str:
    """Render portable multi-turn conversation history markdown (`.agent-context/CHAT_HISTORY.md`)."""
    lines: List[str] = [
        f"# Portable Conversation History — `{chk.repo_name}` (`{chk.checkpoint_id}`)",
        "",
        f"- **Source Agent / Model**: `{chk.source_agent}` (`{chk.source_model}`)",
        f"- **Captured Turns**: `{len(chk.conversation_history)}` (all model reasoning blocks & provider signatures stripped)",
        "",
    ]
    if not chk.conversation_history:
        lines.append("_No conversation turns recorded in this checkpoint._\n")
        return "\n".join(lines)

    for idx, turn in enumerate(chk.conversation_history, start=1):
        role_badge = "USER" if turn.role == "user" else f"ASSISTANT ({chk.source_model})"
        lines.append(f"## Turn {idx} — {role_badge} (`{turn.timestamp}`)")
        if turn.tool_summary:
            lines.append(f"> **Tool Actions**: {turn.tool_summary}")
            lines.append("")
        lines.append(turn.content.strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_concise_agents_md(
    chk: MemoryCheckpointIR,
    target_agent: str,
    target_model: str,
    target_host: HostFingerprint,
) -> str:
    """Render root `AGENTS.md` guaranteed under 150 lines (Pitfall 3 Fix: routing index)."""
    active = _active_facts(chk)
    next_steps = [
        s for s in chk.execution_stack.steps
        if s.status in (TaskLockStatus.PARTIALLY_DONE.value, TaskLockStatus.NOT_DONE.value)
    ]
    lines: List[str] = [
        f"# AGENTS.md — MemGit Continuity Routing Index (`{chk.repo_name}`)",
        "",
        f"> **Active Checkpoint**: `{chk.checkpoint_id}` | **Target Agent**: `{target_agent}` | **Target Model**: `{target_model}`",
        f"> **Workspace**: `{target_host.workspace_root}` (`{target_host.os_name}/{target_host.arch}`, CUDA={target_host.cuda_available})",
        "",
        "## Continuity Routing Pointers",
        "- **Full Handoff & Execution State**: `.agent-context/HANDOFF.md`",
        "- **Conversation History**: `.agent-context/CHAT_HISTORY.md`",
        "- **Topic Knowledge Base**: `.agent-context/memory/`",
        "",
        "## Core Architectural Constraints",
    ]
    for f in active[:15]:
        lines.append(f"- **[{f.entity_key}]**: {f.statement}")

    if chk.user_deferrals:
        lines.extend(["", "## User Deferral Locks (DO NOT IMPLEMENT)"])
        for ud in chk.user_deferrals:
            lines.append(f"- **FORBIDDEN ({ud.feature_or_option})**: {ud.directive}")

    if chk.negative_memories:
        lines.extend(["", "## Known Dead-Ends (Do Not Retry)"])
        for nm in chk.negative_memories[:5]:
            lines.append(f"- Avoid: {nm.hypothesis} -> *{nm.failure_reason}*")

    lines.extend([
        "",
        f"## Immediate Resumption Target: {chk.execution_stack.active_goal}",
    ])
    for s in chk.execution_stack.steps:
        lines.append(f"- **[{s.status}]** `{s.step_id}`: {s.title}")
    if next_steps:
        first_next = next_steps[0]
        lines.extend([
            "",
            f"**Next Action**: Resume at `{first_next.step_id}` ({first_next.title}). "
            "Inspect `.agent-context/HANDOFF.md` and `git status` before editing.",
        ])

    # Enforce < 150 lines Pitfall 3 guarantee
    return "\n".join(lines[:145]) + "\n"


def compile_checkpoint(
    raw_checkpoint: MemoryCheckpointIR,
    target_agent: str,
    target_model: str,
    target_host: HostFingerprint,
    include_private: bool = True,
) -> CompiledMemoryPackage:
    """Compile a provider-agnostic MemoryCheckpointIR into target agent & model artifacts."""
    chk = filter_checkpoint_for_target(
        raw_checkpoint, target_host=target_host, include_private=include_private
    )
    pkg = CompiledMemoryPackage(
        checkpoint_id=chk.checkpoint_id,
        target_agent=target_agent,
        target_model=target_model,
        target_host=target_host,
        include_private=include_private,
    )

    handoff_md = _render_handoff_markdown(chk, target_agent, target_model, target_host)
    agents_md = _render_concise_agents_md(chk, target_agent, target_model, target_host)
    chat_history_md = _render_chat_history_markdown(chk)

    # Always populate Pattern A universal `.agent-context/` + `AGENTS.md`
    pkg.workspace_files[".agent-context/HANDOFF.md"] = handoff_md
    pkg.workspace_files[".agent-context/CHAT_HISTORY.md"] = chat_history_md
    pkg.workspace_files["AGENTS.md"] = agents_md
    for topic in chk.topic_memories:
        pkg.workspace_files[f".agent-context/memory/{topic.topic_slug}.md"] = (
            f"# {topic.title}\n\n> Summary: {topic.summary}\n\n{topic.content}\n"
        )

    agent_norm = target_agent.lower()

    if agent_norm == AgentType.CLAUDE_CODE.value:
        # 1. CLAUDE.md in workspace root (< 200 lines)
        pkg.workspace_files["CLAUDE.md"] = agents_md
        # 2. Path-scoped rules in .claude/rules/*.md with YAML frontmatter
        for fact in _active_facts(chk):
            if fact.path_glob:
                slug = fact.entity_key.replace(".", "_")
                rule_content = (
                    f"---\npaths:\n  - \"{fact.path_glob}\"\n---\n"
                    f"# Rule: {fact.entity_key}\n\n{fact.statement}\n"
                )
                pkg.workspace_files[f".claude/rules/{slug}.md"] = rule_content

        # 3. Dynamic Auto-Memory in ~/.claude/projects/<proj>/memory/MEMORY.md (capped < 200 lines!)
        proj_slug = chk.repo_name.replace("/", "-")
        mem_lines: List[str] = [
            f"# Claude Code Auto-Memory Index (`{chk.repo_name}`)",
            f"<!-- MemGit Checkpoint: {chk.checkpoint_id} | Target Model: {target_model} -->",
            "",
            "## Key Verified Facts",
        ]
        for fact in _active_facts(chk):
            mem_lines.append(f"- `{fact.entity_key}`: {fact.statement}")
        if chk.user_deferrals:
            mem_lines.extend(["", "## Deferred / Off-Limits"])
            for ud in chk.user_deferrals:
                mem_lines.append(f"- [DEFERRED] {ud.feature_or_option}: {ud.directive}")
        mem_lines.extend(["", "## Topic Memory Files (Linked)"])
        for topic in chk.topic_memories:
            mem_lines.append(f"- [{topic.title}](./topics/{topic.topic_slug}.md): {topic.summary}")
            pkg.home_files[
                f".claude/projects/{proj_slug}/memory/topics/{topic.topic_slug}.md"
            ] = f"# {topic.title}\n\n{topic.content}\n"

        # Strictly enforce Claude Code's 200-line startup limit (Pitfall 3)
        capped_mem_lines = mem_lines[:195]
        pkg.home_files[f".claude/projects/{proj_slug}/memory/MEMORY.md"] = (
            "\n".join(capped_mem_lines) + "\n"
        )
        pkg.compilation_notes.append(
            f"Compiled for Claude Code ({target_model}): MEMORY.md capped at {len(capped_mem_lines)}/200 lines."
        )

    elif agent_norm == AgentType.CODEX_CLI.value:
        # OpenAI Codex CLI:
        # 1. Repo-level AGENTS.md + ~/.codex/AGENTS.md (cascading up to 32 KiB limit)
        codex_agents = (
            _model_cognitive_preamble(chk, target_model, target_host)
            + "\n"
            + agents_md
        )
        # Enforce 32 KiB limit
        encoded = codex_agents.encode("utf-8")
        if len(encoded) > 32768:
            codex_agents = encoded[:32000].decode("utf-8", errors="ignore") + "\n...(truncated to 32KiB)\n"
        pkg.workspace_files["AGENTS.md"] = codex_agents
        pkg.home_files[".codex/AGENTS.md"] = (
            f"# Global Codex Instructions (Synced via MemGit `{chk.checkpoint_id}`)\n"
            f"- Default Host: `{target_host.os_name}/{target_host.arch}`\n"
            f"- Inspect repo `AGENTS.md` and `.agent-context/HANDOFF.md` on startup.\n"
        )
        # 2. ~/.codex/memories/memory_summary.md, chat_history.md & MEMORY.md
        pkg.home_files[".codex/memories/memory_summary.md"] = handoff_md
        pkg.home_files[".codex/memories/chat_history.md"] = chat_history_md
        topic_concat = "\n\n---\n\n".join(
            f"## {t.title}\n{t.content}" for t in chk.topic_memories
        )
        pkg.home_files[".codex/memories/MEMORY.md"] = topic_concat or "# Codex Learned Memory\n"
        pkg.compilation_notes.append(
            f"Compiled for OpenAI Codex CLI ({target_model}): AGENTS.md size={len(codex_agents.encode('utf-8'))} bytes (<= 32 KiB budget)."
        )

    elif agent_norm == AgentType.ANTIGRAVITY.value:
        # Google Antigravity:
        # 1. GEMINI.md + concise AGENTS.md
        pkg.workspace_files["GEMINI.md"] = (
            _model_cognitive_preamble(chk, target_model, target_host)
            + "\n"
            + agents_md
        )
        # 2. Knowledge Items under ~/.gemini/antigravity/knowledge/<ki_slug>/
        for topic in chk.topic_memories:
            ki_base = f".gemini/antigravity/knowledge/{topic.topic_slug}"
            metadata = {
                "title": topic.title,
                "summary": topic.summary,
                "checkpoint_id": chk.checkpoint_id,
                "source_model": chk.source_model,
                "target_model": target_model,
                "updated_at": topic.updated_at,
                "tags": topic.tags,
            }
            pkg.home_files[f"{ki_base}/metadata.json"] = json.dumps(metadata, indent=2)
            pkg.home_files[f"{ki_base}/artifacts/overview.md"] = (
                f"# {topic.title}\n\n{topic.content}\n"
            )
        # 3. First-Class Session Artifacts under ~/.gemini/antigravity/brain/<checkpoint_id>/
        brain_base = f".gemini/antigravity/brain/{chk.checkpoint_id}"
        task_lines = [
            f"# Task Checklist — {chk.execution_stack.active_goal}",
            "",
        ]
        status_to_box = {
            TaskLockStatus.DONE.value: "[x]",
            TaskLockStatus.PARTIALLY_DONE.value: "[/]",
            TaskLockStatus.NOT_DONE.value: "[ ]",
        }
        for s in chk.execution_stack.steps:
            box = status_to_box.get(s.status, "[ ]")
            task_lines.append(f"- {box} `{s.step_id}`: {s.title} ({s.status})")
        if chk.user_deferrals:
            task_lines.extend(["", "## Deferred by User (Locked)"])
            for ud in chk.user_deferrals:
                task_lines.append(f"- [-] **DEFERRED**: {ud.feature_or_option} — {ud.directive}")

        pkg.home_files[f"{brain_base}/task.md"] = "\n".join(task_lines) + "\n"
        pkg.home_files[f"{brain_base}/implementation_plan.md"] = handoff_md
        pkg.home_files[f"{brain_base}/chat_history.md"] = chat_history_md
        pkg.compilation_notes.append(
            f"Compiled for Google Antigravity ({target_model}): {len(chk.topic_memories)} Knowledge Items + "
            f"structured Artifacts (task.md, implementation_plan.md, chat_history.md with {len(chk.conversation_history)} turns)."
        )

    elif agent_norm in (AgentType.GEMINI_CLI.value, "gemini", "gemini-app"):
        # Gemini CLI / Gemini App (distinct from Antigravity):
        # 1. Workspace GEMINI.md with cognitive preamble + concise routing index
        pkg.workspace_files["GEMINI.md"] = (
            _model_cognitive_preamble(chk, target_model, target_host)
            + "\n"
            + agents_md
        )
        # 2. Global ~/.gemini/GEMINI.md + ~/.gemini/memories/{memory_summary.md, chat_history.md}
        pkg.home_files[".gemini/GEMINI.md"] = (
            f"# Global Gemini Instructions (Synced via MemGit `{chk.checkpoint_id}`)\n"
            f"- Active Project: `{chk.repo_name}` (`{target_host.workspace_root}`)\n"
            f"- Default Host: `{target_host.os_name}/{target_host.arch}`\n"
            f"- Inspect workspace `GEMINI.md`, `.agent-context/HANDOFF.md`, and `.agent-context/CHAT_HISTORY.md` on startup.\n"
        )
        pkg.home_files[".gemini/memories/memory_summary.md"] = handoff_md
        pkg.home_files[".gemini/memories/chat_history.md"] = chat_history_md
        pkg.compilation_notes.append(
            f"Compiled for Gemini CLI / Gemini App ({target_model}): GEMINI.md + ~/.gemini/GEMINI.md + "
            f"~/.gemini/memories/chat_history.md ({len(chk.conversation_history)} turns)."
        )

    elif agent_norm == AgentType.CURSOR.value:
        # Cursor Agent: .cursor/rules/*.mdc + SQLite state.vscdb entries
        mdc_content = (
            "---\n"
            "description: MemGit Handoff & Architectural Continuity Rules\n"
            "globs: \"**/*\"\n"
            "alwaysApply: true\n"
            "---\n\n"
            + handoff_md
        )
        pkg.workspace_files[".cursor/rules/memgit-continuity.mdc"] = mdc_content
        for f in _active_facts(chk):
            pkg.sqlite_memories.append({
                "key": f"memgit.fact.{f.entity_key}",
                "value": json.dumps(f.to_dict()),
            })
        pkg.compilation_notes.append(
            f"Compiled for Cursor ({target_model}): .cursor/rules/memgit-continuity.mdc + {len(pkg.sqlite_memories)} SQLite state.vscdb records."
        )

    return pkg
