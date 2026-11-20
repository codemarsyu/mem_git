# MemGit

**MemGit** is a cross-agent, cross-machine CLI tool and distributed version-control system for AI coding-agent memory.

AI coding agents (Claude Desktop, Claude Code, Google Antigravity, Gemini CLI, OpenAI Codex CLI, Cursor) each store project context, learned facts, and conversation history in incompatible local formats. MemGit decouples cognitive state from agent-specific storage by compiling sessions into a portable, secret-redacted **Memory Intermediate Representation (`Memory IR`)**, allowing developers to discover, snapshot, transfer, transform, and resume work across agents on the same MacBook or across remote Linux work nodes.

## Key Capabilities

- **Unified Discovery & 1-Step Handoff**: Auto-detect installed agents (`memgit doctor`), browse local conversations across all agents (`memgit ls`), and transfer work in one command (`memgit handoff`).
- **Cross-Agent Native Compilation & Two-Tier Safety**: Compiles sanitized checkpoints into native workspace/global memory files (Tier 1) plus guarded native UI/session stores (Tier 2) with automatic pre-write backups (`~/.memgit/backups/`) and one-command rollback (`memgit undo`).
- **Sanitized & Portable Context**: Automatically redacts API keys/tokens/PEM secrets, strips model-specific reasoning traces, normalizes macOS/Linux paths, preserves user deferral constraints, and separates private vs. shareable memory.
- **Serverless MacBook ↔ Linux Bundles & Git WIP Sync**: Export verified `.mgit.tar.gz` bundles (`memgit export` / `memgit import`) with SHA256 integrity checks and optional Git working-tree diffs for offline/SSH transfer to Linux work nodes.
- **Provenance DAG Semantic Merging**: Resolves conflicting learned facts across branches or teammates using provenance-aware merging and cognitive blame.

## Installation (macOS & Linux)

### Option 1: One-Line Installer (GitHub Releases)
```bash
curl -fsSL https://raw.githubusercontent.com/codemarsyu/mem_git/main/install.sh | sh
```

### Option 2: Python (`pipx` / `uv` / `pip`)
```bash
pipx install .          # or: python3 -m pip install .
memgit --version
```

### Option 3: Build Local Installers (`.whl`, `.tar.gz`, macOS `.pkg`, Linux `.deb`/`.rpm`)
```bash
bash packaging/build_wheel.sh        # Python wheel + clean-venv smoke test
bash packaging/build_binary.sh       # Standalone executable + release tarball
bash packaging/build_macos_pkg.sh    # macOS .pkg installer (installs /usr/local/bin/memgit + completions)
bash packaging/build_linux_pkg.sh    # Linux .deb & .rpm packages via nfpm / dpkg-deb
```

## Everyday CLI Usage

```bash
# 1. Check installed agents, paths, and CLI readiness
memgit doctor

# 2. List conversations across Claude Desktop, Claude Code, Antigravity, Codex, etc.
memgit ls
memgit ls --agent claude-desktop --query "Gradio"

# 3. One-step handoff from Claude Desktop -> Antigravity (standalone conversation, no project created)
memgit handoff "Gradio" --from claude-desktop --to antigravity --workspace ./my-project --bootstrap

# 4. Transfer a checkpoint from MacBook to a Linux work node (no server required)
memgit export chk_1b841627b4cb -o task.mgit.tar.gz --strip-private
scp task.mgit.tar.gz user@linux-node:~/
# On Linux work node:
memgit import ~/task.mgit.tar.gz
memgit resume chk_1b841627b4cb --to codex --workspace ~/project

# 5. Roll back the last target-agent write if needed
memgit undo
```

## Development & Testing

```bash
python3 -m unittest discover -s tests -v
python3 -m memgit.cli demo
```

## Architecture & Product Scoping

- Architecture & language rationale: [design.md](./design.md)
- CLI product scoping & release roadmap: [CLI_PRODUCT_SCOPING.md](./CLI_PRODUCT_SCOPING.md)
