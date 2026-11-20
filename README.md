# MemGit

**MemGit** is a distributed version-control and cross-model handoff system for AI coding-agent memory.

AI coding agents (Claude Code, OpenAI Codex CLI, Google Antigravity, Cursor) each store project context, learned facts, and conversation history in incompatible local formats. MemGit decouples cognitive state from agent-specific storage by compiling sessions into a portable **Memory Intermediate Representation (`Memory IR`)**, allowing developers to seamlessly snapshot, transfer, merge, and resume agent work across different machines and different AI agents.

## Key Capabilities

- **Cross-Agent Checkpoint & Handoff**: Extract project memory, active task state, and conversation history from one agent and compile it into native memory files and session transcripts for another (Claude Code, Codex CLI, Antigravity, Cursor).
- **Sanitized & Portable Context**: Automatically strips model-specific reasoning/thinking traces, normalizes machine-specific paths, preserves user deferral constraints, and separates private vs. shareable memory.
- **Dual-Stream Git WIP Sync**: Pairs memory checkpoints with dirty Git working-tree state (HEAD, staged/unstaged diffs, and untracked files) so work resumes cleanly on a new machine.
- **Provenance DAG Semantic Merging**: Resolves conflicting learned facts across branches or teammates using provenance-aware merging and cognitive blame.

## Quick Start

Run the test suite:
```bash
python3 -m unittest discover -s tests -v
```

Run the end-to-end multi-agent handoff demo:
```bash
python3 -m memgit.cli demo
```

## Architecture & Design

For detailed architecture notes, language selection rationale, and repository layout, see [design.md](./design.md).
