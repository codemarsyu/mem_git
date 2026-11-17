# MemGit: Distributed Version Control & Cross-Model Handoff for Agent Memory

MemGit decouples **agent-specific storage schemas** (Claude Code, OpenAI Codex CLI, Google Antigravity, Cursor) from **declarative cognitive state** (`Memory IR`), enabling seamless cross-machine and cross-model checkpoint handoff, dirty Git working tree synchronization, and Provenance DAG semantic merging.

---

## 1. Programming Language Selection Analysis

To determine the optimal language for MemGit (spanning the local CLI/daemon, Memory IR compiler, Git WIP bundler, and remote transformation/merge server), we evaluated four candidates:

| Criterion | **Python (3.10+)** *(Selected for MVP & Server)* | **Go (Golang)** | **Rust** | **TypeScript / Node.js** |
| :--- | :--- | :--- | :--- | :--- |
| **LLM & Memory Ecosystem** | **Best-in-Class**: Native home of MCP servers, tokenizers, vector stores (Milvus/Mem0), and semantic conflict resolution pipelines. | Moderate: Good HTTP/CLI libraries, fewer native NLP/embedding/AST tools. | Moderate: Fast tokenizers, higher friction for rapid schema & prompt iteration. | Good: Strong MCP SDKs, weaker on ML/scientific tooling on GPU servers. |
| **Cross-Agent Schema Parsing** | **Excellent**: Built-in `sqlite3` (for Cursor `state.vscdb` & Server DB), `json` (JSONL transcripts), regex/Markdown/YAML frontmatter parsing. | Good (requires CGo or pure-Go SQLite driver). | Good (requires `rusqlite` compilation). | Good (requires native `better-sqlite3` bindings). |
| **Pre-installed Availability on Target Hosts** | **Ubiquitous**: Pre-installed on macOS and virtually every Linux GPU server where developers roam with Claude/Codex/Antigravity. | Requires pre-compiled binary per OS/Arch. | Requires pre-compiled binary per OS/Arch. | Requires Node runtime (`node`/`npm`) on target host. |
| **Zero-Dependency Local & Server Prototyping** | **Excellent**: Standard library includes `sqlite3`, `http.server`, `urllib`, `subprocess`, `difflib`, `hashlib`. | Standard library has `net/http`, needs external SQLite. | Needs Cargo crates (`tokio`, `axum`, `serde`, `rusqlite`). | Needs `npm` packages. |

### Recommendation
- **Primary Language: Python 3.10+ (Zero external runtime dependencies in core)** for the **Memory IR Compiler, Agent Adapters, Semantic Merger, Mock/Remote Control Plane Server, and CLI**.
  - It runs out-of-the-box on this machine (`python3`, `git`, `sqlite3`) and on remote Linux GPU nodes without compiling C extensions.
- **Long-Term Production Edge Option**: Keep the **Server Control Plane & Semantic Compiler in Python**, and optionally wrap the lightweight workstation file-watcher/CLI hook in a single static **Go** or **Rust** binary if zero-runtime edge distribution is desired later.

---

## 2. Project Structure (`mem_git/`)

```text
mem_git/
├── pyproject.toml                   # Package metadata & `memgit` CLI entry point
├── README.md                        # Architecture & Language Dossier
├── memgit/
│   ├── __init__.py
│   ├── models.py                    # MemoryCheckpointIR, ProvenanceNode, UserDeferralLock, GitWipBundle
│   ├── sanitizer.py                 # 4-Pitfall protection (Thinking strip, Path normalize, Deferrals, M_private)
│   ├── git_wip.py                   # Dual-Stream Atomic Sync: Git HEAD + staged/unstaged/untracked WIP bundle
│   ├── compiler.py                  # Cross-Model Cognitive Compiler (Claude Code / Codex / Antigravity / Cursor)
│   ├── merge.py                     # Provenance DAG Semantic Merger & Cognitive Blame engine
│   ├── client.py                    # Client SDK for Remote Server push / pull+transform / merge / blame
│   ├── cli.py                       # `memgit` CLI (`server`, `checkpoint`, `pull`, `merge`, `blame`, `demo`)
│   ├── adapters/
│   │   ├── base.py                  # Abstract AgentAdapter & AgentBootSimulationReport
│   │   ├── claude_code.py           # Claude Code extractor, mock session seeder & 200-line MEMORY.md simulator
│   │   ├── codex_cli.py             # OpenAI Codex CLI extractor, seeder & 32 KiB cascading AGENTS.md simulator
│   │   ├── antigravity.py           # Google Antigravity Knowledge Items & Brain Artifacts adapter/simulator
│   │   └── cursor.py                # Cursor (.cursor/rules/*.mdc + SQLite state.vscdb) adapter/simulator
│   └── server/
│       ├── storage.py               # Server-side SQLite + Content-Addressed Object Store
│       └── app.py                   # Mock Remote HTTP REST Server (`MockRemoteMemGitServer`)
└── tests/
    ├── simulate_roaming_demo.py     # Full 5-node cross-model roaming & team merge simulation
    └── test_end_to_end_handoff.py   # Automated end-to-end unittest suite
```

---

## 3. Running the Tests & Multi-Node Simulation

Run the automated test suite:
```bash
python3 -m unittest discover -s tests -v
```

Run the interactive multi-machine, cross-model handoff simulation:
```bash
python3 -m memgit.cli demo
```
