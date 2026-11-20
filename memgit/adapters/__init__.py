"""Registry of Agent Memory Adapters and Simulators."""

from __future__ import annotations

from typing import Dict, List

from memgit.adapters.antigravity import AntigravityAdapter
from memgit.adapters.base import AgentAdapter, AgentBootSimulationReport, AgentSessionSummary
from memgit.adapters.claude_code import ClaudeCodeAdapter
from memgit.adapters.claude_desktop import ClaudeDesktopAdapter
from memgit.adapters.codex_cli import CodexCLIAdapter
from memgit.adapters.cursor import CursorAdapter
from memgit.adapters.gemini_cli import GeminiCLIAdapter
from memgit.models import AgentType

_ADAPTERS: Dict[str, AgentAdapter] = {
    AgentType.CLAUDE_DESKTOP.value: ClaudeDesktopAdapter(),
    "claude-app": ClaudeDesktopAdapter(),
    AgentType.CLAUDE_CODE.value: ClaudeCodeAdapter(),
    "claude": ClaudeCodeAdapter(),
    AgentType.CODEX_CLI.value: CodexCLIAdapter(),
    "codex-cli": CodexCLIAdapter(),
    AgentType.ANTIGRAVITY.value: AntigravityAdapter(),
    "agy": AntigravityAdapter(),
    AgentType.GEMINI_CLI.value: GeminiCLIAdapter(),
    "gemini": GeminiCLIAdapter(),
    "gemini-app": GeminiCLIAdapter(),
    AgentType.CURSOR.value: CursorAdapter(),
}

CANONICAL_ADAPTER_KEYS: List[str] = [
    AgentType.CLAUDE_DESKTOP.value,
    AgentType.CLAUDE_CODE.value,
    AgentType.ANTIGRAVITY.value,
    AgentType.GEMINI_CLI.value,
    AgentType.CODEX_CLI.value,
    AgentType.CURSOR.value,
]


def get_adapter(agent_type: str) -> AgentAdapter:
    key = agent_type.lower().strip()
    if key not in _ADAPTERS:
        raise ValueError(
            f"Unsupported agent_type '{agent_type}'. Supported: {sorted(set(_ADAPTERS.keys()))}"
        )
    return _ADAPTERS[key]


def list_all_adapters() -> List[AgentAdapter]:
    return [_ADAPTERS[k] for k in CANONICAL_ADAPTER_KEYS]


__all__ = [
    "AgentAdapter",
    "AgentBootSimulationReport",
    "AgentSessionSummary",
    "ClaudeDesktopAdapter",
    "ClaudeCodeAdapter",
    "CodexCLIAdapter",
    "AntigravityAdapter",
    "GeminiCLIAdapter",
    "CursorAdapter",
    "CANONICAL_ADAPTER_KEYS",
    "get_adapter",
    "list_all_adapters",
]
