"""Core data models for MemGit Memory IR (Intermediate Representation),
Provenance DAG, User Deferral Locks, and Dual-Stream Git WIP State Commits.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Dict, List, Optional


class AgentType(str, Enum):
    CLAUDE_CODE = "claude-code"
    CODEX_CLI = "codex"
    ANTIGRAVITY = "antigravity"
    CURSOR = "cursor"


class MemoryScope(str, Enum):
    SHARED = "shared"    # M_shared: Architectural facts, project decisions, task state
    PRIVATE = "private"  # M_private: Personal preferences ("respond in Chinese", SSH aliases)


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED_PASS = "verified_pass"
    VERIFIED_FAIL = "verified_fail"
    SUPERSEDED = "superseded"


class TaskLockStatus(str, Enum):
    DONE = "DONE"
    PARTIALLY_DONE = "PARTIALLY_DONE"
    NOT_DONE = "NOT_DONE"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HostFingerprint:
    """Environment metadata captured on source or target machine for re-hydration."""
    os_name: str               # e.g. "Darwin", "Linux"
    arch: str                  # e.g. "arm64", "x86_64"
    workspace_root: str        # e.g. "/Users/alice/proj" or "/home/ubuntu/proj"
    user_home: str             # e.g. "/Users/alice" or "/home/ubuntu"
    shell: str = "zsh"
    cuda_available: bool = False
    gpu_device: str = "none"
    hostname: str = "localhost"

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HostFingerprint:
        return cls(**data)


@dataclass
class ProvenanceNode:
    """A node in the Provenance DAG tagging a single declarative fact or rule."""
    fact_id: str
    entity_key: str            # Canonical subject/topic key, e.g. "module_x.concurrency"
    category: str              # e.g. "constraint", "architecture", "convention", "preference"
    statement: str             # Declarative statement with normalized paths ($WORKSPACE_ROOT)
    scope: str = MemoryScope.SHARED.value
    author: str = "developer"
    source_agent: str = AgentType.CLAUDE_CODE.value
    source_model: str = "claude-3-7-sonnet"
    timestamp: str = field(default_factory=utc_now_iso)
    parent_hash: Optional[str] = None
    verification_status: str = VerificationStatus.UNVERIFIED.value
    superseded_by: Optional[str] = None
    path_glob: Optional[str] = None  # Optional path-scoped rule pattern (e.g. "src/api/**/*.py")

    def compute_hash(self) -> str:
        payload = f"{self.entity_key}|{self.statement}|{self.author}|{self.timestamp}|{self.parent_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProvenanceNode:
        return cls(**data)


@dataclass
class NegativeMemoryItem:
    """Records a failed approach so target agents do not repeat dead-ends."""
    attempt_id: str
    hypothesis: str            # What was attempted
    failure_reason: str        # Why it failed (test error, perf regression, architectural mismatch)
    files_involved: List[str] = field(default_factory=list)
    source_model: str = "unknown"
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> NegativeMemoryItem:
        return cls(**data)


@dataclass
class UserDeferralLock:
    """Pitfall 4 Fix: Explicit user deferrals that target agents are forbidden from touching."""
    deferral_id: str
    feature_or_option: str     # e.g. "Option B: Redis distributed lock"
    directive: str             # e.g. "DO NOT implement Redis caching in v1; keep in-memory store."
    reauthorize_condition: str = "Explicit user prompt re-authorizing this item"
    author: str = "user"
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UserDeferralLock:
        return cls(**data)


@dataclass
class TaskStep:
    """Granular task step with strict DONE / PARTIALLY_DONE / NOT_DONE locks."""
    step_id: str
    title: str
    status: str = TaskLockStatus.NOT_DONE.value
    relative_files: List[str] = field(default_factory=list)
    verification_cmd: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskStep:
        return cls(**data)


@dataclass
class ExecutionStack:
    """Declarative execution state compiled from raw transcripts (Pitfall 1 Fix)."""
    active_goal: str
    current_hypothesis: str = ""
    modified_files: List[str] = field(default_factory=list)
    passing_test_commands: List[str] = field(default_factory=list)
    failing_test_commands: List[str] = field(default_factory=list)
    stack_trace_digest: str = ""
    steps: List[TaskStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_goal": self.active_goal,
            "current_hypothesis": self.current_hypothesis,
            "modified_files": self.modified_files,
            "passing_test_commands": self.passing_test_commands,
            "failing_test_commands": self.failing_test_commands,
            "stack_trace_digest": self.stack_trace_digest,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExecutionStack:
        steps = [TaskStep.from_dict(s) for s in data.get("steps", [])]
        return cls(
            active_goal=data.get("active_goal", ""),
            current_hypothesis=data.get("current_hypothesis", ""),
            modified_files=data.get("modified_files", []),
            passing_test_commands=data.get("passing_test_commands", []),
            failing_test_commands=data.get("failing_test_commands", []),
            stack_trace_digest=data.get("stack_trace_digest", ""),
            steps=steps,
        )


@dataclass
class TopicMemoryFile:
    """Modular topic memory note (e.g. Claude topic file or Antigravity Knowledge Item)."""
    topic_slug: str            # e.g. "auth_pipeline", "db_migrations"
    title: str
    summary: str
    content: str               # Normalized markdown content
    scope: str = MemoryScope.SHARED.value
    tags: List[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TopicMemoryFile:
        return cls(**data)


@dataclass
class GitWipBundle:
    """Dual-Stream Atomic Sync: Captures dirty working tree alongside cognitive state."""
    base_commit: str
    branch: str
    staged_diff: str = ""
    unstaged_diff: str = ""
    untracked_files: Dict[str, str] = field(default_factory=dict)  # rel_path -> utf-8 content
    is_dirty: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> GitWipBundle:
        return cls(**data)


@dataclass
class SanitizationStats:
    """Audit metrics proving 4-Pitfall sanitization occurred during checkpointing."""
    stripped_thinking_blocks: int = 0
    stripped_tool_call_schemas: int = 0
    normalized_host_paths: int = 0
    isolated_user_deferrals: int = 0
    private_facts_count: int = 0
    shared_facts_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SanitizationStats:
        return cls(**data)


@dataclass
class MemoryCheckpointIR:
    """Atomic State Commit combining Declarative Memory IR, Provenance DAG, and Git WIP Patch."""
    checkpoint_id: str
    repo_name: str
    branch: str
    parent_checkpoint_id: Optional[str]
    author: str
    source_agent: str
    source_model: str
    source_host: HostFingerprint
    created_at: str
    message: str
    confirmed_facts: List[ProvenanceNode] = field(default_factory=list)
    negative_memories: List[NegativeMemoryItem] = field(default_factory=list)
    user_deferrals: List[UserDeferralLock] = field(default_factory=list)
    execution_stack: ExecutionStack = field(
        default_factory=lambda: ExecutionStack(active_goal="General development")
    )
    topic_memories: List[TopicMemoryFile] = field(default_factory=list)
    git_wip: GitWipBundle = field(
        default_factory=lambda: GitWipBundle(base_commit="HEAD", branch="main")
    )
    sanitization_stats: SanitizationStats = field(default_factory=SanitizationStats)

    @staticmethod
    def generate_id(repo_name: str, author: str, message: str, timestamp: str, parent: Optional[str]) -> str:
        raw = f"{repo_name}|{author}|{message}|{timestamp}|{parent or 'root'}"
        return "chk_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "repo_name": self.repo_name,
            "branch": self.branch,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "author": self.author,
            "source_agent": self.source_agent,
            "source_model": self.source_model,
            "source_host": self.source_host.to_dict(),
            "created_at": self.created_at,
            "message": self.message,
            "confirmed_facts": [f.to_dict() for f in self.confirmed_facts],
            "negative_memories": [n.to_dict() for n in self.negative_memories],
            "user_deferrals": [d.to_dict() for d in self.user_deferrals],
            "execution_stack": self.execution_stack.to_dict(),
            "topic_memories": [t.to_dict() for t in self.topic_memories],
            "git_wip": self.git_wip.to_dict(),
            "sanitization_stats": self.sanitization_stats.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryCheckpointIR:
        return cls(
            checkpoint_id=data["checkpoint_id"],
            repo_name=data["repo_name"],
            branch=data.get("branch", "main"),
            parent_checkpoint_id=data.get("parent_checkpoint_id"),
            author=data.get("author", "developer"),
            source_agent=data["source_agent"],
            source_model=data["source_model"],
            source_host=HostFingerprint.from_dict(data["source_host"]),
            created_at=data["created_at"],
            message=data.get("message", ""),
            confirmed_facts=[ProvenanceNode.from_dict(f) for f in data.get("confirmed_facts", [])],
            negative_memories=[NegativeMemoryItem.from_dict(n) for n in data.get("negative_memories", [])],
            user_deferrals=[UserDeferralLock.from_dict(d) for d in data.get("user_deferrals", [])],
            execution_stack=ExecutionStack.from_dict(data.get("execution_stack", {})),
            topic_memories=[TopicMemoryFile.from_dict(t) for t in data.get("topic_memories", [])],
            git_wip=GitWipBundle.from_dict(data.get("git_wip", {"base_commit": "HEAD", "branch": "main"})),
            sanitization_stats=SanitizationStats.from_dict(data.get("sanitization_stats", {})),
        )
