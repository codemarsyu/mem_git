"""Registry of Agent Memory Adapters and Simulators."""

from __future__ import annotations

from typing import Dict

from memgit.adapters.antigravity import AntigravityAdapter
from memgit.adapters.base import AgentAdapter, AgentBootSimulationReport
from memgit.adapters.claude_code import ClaudeCodeAdapter
from memgit.adapters.codex_cli import CodexCLIAdapter
from memgit.adapters.cursor import CursorAdapter
from memgit.adapters.gemini_cli import GeminiCLIAdapter
from memgit.models import AgentType

_ADAPTERS: Dict[str, AgentAdapter] = {
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


def get_adapter(agent_type: str) -> AgentAdapter:
    key = agent_type.lower().strip()
    if key not in _ADAPTERS:
        raise ValueError(
            f"Unsupported agent_type '{agent_type}'. Supported: {sorted(set(_ADAPTERS.keys()))}"
        )
    return _ADAPTERS[key]


__all__ = [
    "AgentAdapter",
    "AgentBootSimulationReport",
    "ClaudeCodeAdapter",
    "CodexCLIAdapter",
    "AntigravityAdapter",
    "GeminiCLIAdapter",
    "CursorAdapter",
    "get_adapter",
]
