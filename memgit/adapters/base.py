"""Base classes for Agent Memory Adapters and Mock Agent Loading Simulators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from memgit.compiler import CompiledMemoryPackage
from memgit.models import HostFingerprint, MemoryCheckpointIR


@dataclass
class AgentBootSimulationReport:
    """Result of simulating an agent's native startup memory loading and task resumption."""
    agent_type: str
    model_name: str
    workspace_root: str
    user_home: str
    loaded_files: List[str] = field(default_factory=list)
    context_window_bytes: int = 0
    memory_index_lines: int = 0
    within_budget_limits: bool = True
    foreign_thinking_leaks: int = 0
    source_path_leaks: int = 0
    active_constraints_loaded: List[str] = field(default_factory=list)
    superseded_constraints_blocked: List[str] = field(default_factory=list)
    user_deferrals_enforced: List[str] = field(default_factory=list)
    negative_memories_loaded: List[str] = field(default_factory=list)
    resumed_step_id: Optional[str] = None
    resumed_step_title: Optional[str] = None
    assembled_system_context_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "model_name": self.model_name,
            "workspace_root": self.workspace_root,
            "user_home": self.user_home,
            "loaded_files": self.loaded_files,
            "context_window_bytes": self.context_window_bytes,
            "memory_index_lines": self.memory_index_lines,
            "within_budget_limits": self.within_budget_limits,
            "foreign_thinking_leaks": self.foreign_thinking_leaks,
            "source_path_leaks": self.source_path_leaks,
            "active_constraints_loaded": self.active_constraints_loaded,
            "superseded_constraints_blocked": self.superseded_constraints_blocked,
            "user_deferrals_enforced": self.user_deferrals_enforced,
            "negative_memories_loaded": self.negative_memories_loaded,
            "resumed_step_id": self.resumed_step_id,
            "resumed_step_title": self.resumed_step_title,
            "assembled_system_context_preview": self.assembled_system_context_preview,
        }


class AgentAdapter(ABC):
    """Abstract adapter for extracting, installing, and simulating an LLM coding agent's memory."""

    agent_type: str

    @abstractmethod
    def extract_checkpoint(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        author: str,
        source_model: str,
        host_fingerprint: HostFingerprint,
        message: str = "Checkpoint",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        """Extract native agent memory files, transcripts, and Git WIP into MemoryCheckpointIR."""

    def install_compiled_package(
        self,
        pkg: CompiledMemoryPackage,
        workspace_root: Path,
        user_home: Path,
    ) -> List[str]:
        """Write compiled workspace_files and home_files onto the target node's filesystem."""
        written: List[str] = []
        for rel_path, content in pkg.workspace_files.items():
            target = workspace_root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(str(target))

        for rel_path, content in pkg.home_files.items():
            target = user_home / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(str(target))

        return written

    @abstractmethod
    def simulate_boot_and_load(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        model_name: str,
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        """Simulate the agent's native memory loading mechanism and verify handoff integrity."""
