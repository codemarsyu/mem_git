# MemGit CLI Product Scoping & Release Plan

**Status:** Draft v1  
**Target Platforms:** macOS (`arm64`, `x86_64`) & Linux (`x86_64`, `arm64`)  
**Primary Distribution:** Installable CLI binary / package (`brew`, `pipx`/`uv`, GitHub Releases `.tar.gz`/`.pkg`/`.deb`)

---

## 1. Product Vision & Use Cases

Developers increasingly work across multiple AI agent apps and models on the same workstation—and across remote Linux work nodes—due to model strengths, rate limits, pricing, or IDE/terminal workflows (e.g., brainstorming in Claude Desktop, coding in Claude Code or Antigravity, debugging on a headless Linux GPU node via Codex CLI or Gemini CLI).

**MemGit CLI (`memgit`)** is a local-first, cross-platform control plane that lets a developer:
1. **Discover** active and historical conversations/memories across all installed AI agents on a machine.
2. **Snapshot (`checkpoint`)** an agent session into a sanitized, portable, versioned **Memory IR** bundle (stripping raw thinking traces, redacting secrets, normalizing OS paths, and capturing optional Git WIP diffs).
3. **Transform & Resume (`handoff` / `resume`)** work seamlessly from any supported source agent to any target agent—on the **same machine** or transferred to **another MacBook or Linux work node**.
4. **Manage & Audit** checkpoints, roll back changes safely, and keep local agent storage clean.

---

## 2. Architecture Assessment: Is CLI the Right Product Shape?

**Yes—CLI-first is the optimal foundation** for this use case:
- **Works everywhere:** Identical UX on a MacBook and on headless Linux SSH/GPU work nodes where GUI apps do not run.
- **Composable:** Easily invoked by humans, shell scripts, Git hooks, or *by the agents themselves* (via an optional MemGit Skill or MCP tool).
- **Low-friction installation:** A single `memgit` command is far easier to adopt and trust than a background daemon or heavyweight desktop app.
- **Clear expansion path:** Once the CLI and adapter contracts are rock-solid, a TUI picker, macOS menu-bar companion, or MCP server can sit cleanly on top of the exact same CLI core.

### Key Architectural Guardrails for Production
1. **Two-Tier Adapter Contract (Resilience against app updates):**
   - **Tier 1 (Stable / Documented):** Official workspace & global memory files (`CLAUDE.md`, `GEMINI.md`, `AGENTS.md`, `.agent-context/HANDOFF.md`, `.agent-context/CHAT_HISTORY.md`, `.cursor/rules/`) + official CLIs (`claude`, `agy`, `gemini`, `codex`). Always guaranteed to succeed.
   - **Tier 2 (Native UI Integration — Best-Effort with Safe Fallback):** Direct readers/writers for app-internal stores (Claude Desktop Chromium IndexedDB LevelDB, Antigravity SQLite + protobuf metadata, Cursor `state.vscdb`). Must validate schema version before reading/writing, snapshot automatic backups before mutation, and degrade gracefully to Tier 1 with a clear warning if an app update changes internal schemas.
2. **Secret Redaction & Safety by Default:**
   - Redact API keys, bearer tokens, private keys, and `.env` secrets *before* writing checkpoints or exporting bundles.
   - Never mutate a target agent's DB without an automatic atomic backup (`~/.memgit/backups/`) and `memgit undo`.
3. **Serverless-First Cross-Machine Transfer:**
   - Prioritize **portable bundle export/import** (`memgit export` / `memgit import`) and **SSH/Git transfer** (`scp` / `git` refs) between MacBooks and Linux work nodes before investing in a hosted sync server.

---

## 3. Target CLI User Experience

### Primary Commands (v1.0 Surface)

| Command | Purpose | Example |
| :--- | :--- | :--- |
| `memgit doctor` | Detect installed agents, OS paths, CLI binaries, auth readiness, and permissions. | `memgit doctor` |
| `memgit ls` | List discoverable conversations/sessions across all installed agents (filterable by agent, query, or workspace). | `memgit ls --agent claude-desktop --query "Gradio"` |
| `memgit handoff` | **One-step workflow:** extract from source agent, sanitize, transform, and resume in target agent (interactive picker if omitted). | `memgit handoff "Gradio Parquet" --from claude-desktop --to antigravity` |
| `memgit checkpoint` | Snapshot a conversation/workspace into `~/.memgit/checkpoints/<id>.json` (or repo `.memgit/`). | `memgit checkpoint --agent claude-code --workspace .` |
| `memgit resume` | Transform and load an existing checkpoint into a target agent (standalone conversation by default; `--create-project` optional). | `memgit resume chk_1b841627b4cb --to antigravity --bootstrap` |
| `memgit export` / `memgit import` | Package a checkpoint + optional Git WIP into a single portable `.mgit.tar.gz` bundle (optionally encrypted) for MacBook ↔ Linux transfer. | `memgit export chk_1b841627b4cb -o task.mgit.tar.gz` |
| `memgit show` / `memgit diff` | Inspect checkpoint facts, turns, sanitization stats, or compare two checkpoints. | `memgit show chk_1b841627b4cb` |
| `memgit rm` / `memgit undo` | Cleanly delete a checkpoint or roll back the last target-agent DB/file write from backup. | `memgit undo` |
| `memgit completion` | Generate shell completion scripts (`zsh`, `bash`, `fish`). | `memgit completion zsh` |

### Global UX Standards
- **Human-readable output by default** (tables, clear next-step links/instructions) + **`--json` flag** on every command for scripting and agent consumption.
- **Dry-run mode (`--dry-run`)** on all mutating commands (`handoff`, `resume`, `import`, `rm`).
- **Config file** at `~/.config/memgit/config.toml` (respecting `$XDG_CONFIG_HOME` on Linux and `~/Library/Application Support/memgit` or `~/.config/memgit` on macOS) for default author, preferred target models, redaction rules, and storage directory.

---

## 4. Platform & Agent Matrix (macOS + Linux)

| Agent | macOS Storage Paths | Linux Storage Paths | Read Support | Resume / Write Support |
| :--- | :--- | :--- | :--- | :--- |
| **Claude Desktop** | `~/Library/Application Support/Claude/IndexedDB/` | `~/.config/Claude/IndexedDB/` | LevelDB + Snappy + V8 clone parser (to productize) | N/A (read-only source) |
| **Claude Code** | `~/.claude/projects/<key>/` + `CLAUDE.md` | `~/.claude/projects/<key>/` + `CLAUDE.md` | Native JSONL + `MEMORY.md` | Native JSONL + `CLAUDE.md` + `claude` CLI |
| **Antigravity App** | `~/.gemini/antigravity/` + `~/.gemini/antigravity-cli/` | `~/.gemini/antigravity/` + `~/.gemini/antigravity-cli/` | SQLite + brain artifacts | `agy` CLI + WAL-merged `.db`, `annotations/`, `conversation_summaries.db` |
| **Gemini CLI** | `~/.gemini/` + `GEMINI.md` | `~/.gemini/` + `GEMINI.md` | `GEMINI.md` + `~/.gemini/memories/` | `GEMINI.md` + `gemini` CLI |
| **OpenAI Codex CLI** | `~/.codex/` + `AGENTS.md` | `~/.codex/` + `AGENTS.md` | `AGENTS.md` + sessions | `AGENTS.md` + `sessions/` + `codex` CLI |
| **Cursor** | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | `~/.config/Cursor/User/globalStorage/state.vscdb` | SQLite `state.vscdb` + `.cursor/rules/` | `.cursor/rules/*.mdc` + `state.vscdb` |

> **Known Platform Caveat (Antigravity Desktop App):** Antigravity reconciles `~/.gemini/antigravity/` into its sidebar at app startup or when opening a `conversation://<id>` link. Until Antigravity exposes a public live-reload CLI/URL hook, `memgit resume` completes all disk/DB syncing and prints the clickable `conversation://<id>` link plus reload hint (`Cmd+R`).

---

## 5. Packaging, Release & Installation Strategy

### Runtime Decision
- **v0.2 – v1.0:** Keep **Python (`>=3.9`)** with **zero mandatory runtime dependencies** (stdlib `sqlite3`, `json`, `tarfile`, `urllib`, `argparse`). This maximizes velocity across fast-moving agent formats and keeps the codebase simple and auditable.
- **Standalone Binaries:** Use **PyInstaller** (or `shiv` / `pex` / `Nuitka`) in GitHub Actions to emit self-contained binaries so target MacBooks and Linux nodes **do not even need a matching Python version installed**.

### Distribution Channels

| Channel | Target Users | Install Command | Implementation Effort |
| :--- | :--- | :--- | :--- |
| **PyPI (`pipx` / `uv`)** | Python/ML engineers, Linux work nodes | `pipx install memgit` or `uv tool install memgit` | Low (1–2 days) |
| **Homebrew Tap** | macOS developers | `brew install codemarsyu/tap/memgit` | Low (1–2 days) |
| **GitHub Releases + `install.sh`** | Any macOS / Linux node (no Python needed) | `curl -fsSL https://raw.githubusercontent.com/codemarsyu/mem_git/main/install.sh \| sh` | Medium (2–3 days) |
| **macOS `.pkg` / Linux `.deb` & `.rpm`** | Enterprise / fleet installation on MacBooks & Ubuntu/Debian/RHEL nodes | `sudo installer -pkg memgit.pkg -target /` / `sudo dpkg -i memgit.deb` | Medium (2–3 days) |

### Release Pipeline (GitHub Actions)
Trigger on semantic version tags (`v*.*.*`):
1. **CI Matrix Verification:** Run full unit + integration test suite across `macos-14` (`arm64`), `macos-13` (`x86_64`), `ubuntu-latest` (`x86_64`), and `ubuntu-24.04-arm` (`arm64`) on Python `3.9`, `3.11`, `3.12`.
2. **Build Artifacts:**
   - Python `sdist` + universal `.whl` (published to PyPI via OIDC Trusted Publishing).
   - Standalone executables: `memgit-darwin-arm64.tar.gz`, `memgit-darwin-x86_64.tar.gz`, `memgit-linux-x86_64.tar.gz`, `memgit-linux-arm64.tar.gz` + `SHA256SUMS`.
   - Native packages: signed/unsigned `.pkg` (macOS via `pkgbuild` + `productbuild`), `.deb` & `.rpm` (Linux via `nfpm`).
3. **Auto-Update Homebrew Formula:** Workflow opens/pushes updated formula with new SHA256 hashes to `homebrew-tap`.

---

## 6. Phased Work Scope & Milestones

### Milestone 1 — Core Product Hardening & Unified UX (~1.5–2 weeks)
- [ ] **Python 3.9+ compatibility & clean `pyproject.toml`:** Update `requires-python = ">=3.9"`, add package metadata, classifiers, license, and console entrypoint `memgit = "memgit.cli:main"`.
- [ ] **Modularize `memgit/converter.py`:** Split the ~1,700-line `converter.py` into focused modules (`memgit/sync/antigravity.py`, `memgit/sync/protobuf_wire.py`, `memgit/sync/claude.py`, `memgit/sync/codex.py`, `memgit/bootstrap.py`).
- [ ] **Productize `ClaudeDesktopAdapter` (`memgit/adapters/claude_desktop.py`):** Move LevelDB WAL + Snappy + V8 structured-clone conversation listing & extraction into a first-class adapter supporting both macOS and Linux paths, with unit tests on fixture data.
- [ ] **Implement `memgit doctor` and `memgit ls`:** Auto-discover installed agents, validate paths/binaries, and list conversations across all agents in a unified table (`--json` supported).
- [ ] **Implement 1-step `memgit handoff`:** Combine discover/extract → sanitize → convert → bootstrap into a single ergonomic command with `--dry-run`.
- [ ] **OS path abstraction (`memgit/paths.py`):** Centralize macOS vs. Linux (`XDG_*`) storage discovery and workspace path re-hydration.

### Milestone 2 — Safety, Security & Cross-Machine Portability (~1–1.5 weeks)
- [ ] **Secret redaction engine in `memgit/sanitizer.py`:** Detect & mask API keys (`sk-ant-*`, `sk-*`, `AIza*`, `ghp_*`, AWS keys, Bearer tokens, PEM blocks) with audit counts in `SanitizationStats`.
- [ ] **Atomic backup & `memgit undo`:** Snapshot touched target DBs/files to `~/.memgit/backups/<op_id>/` before any write; add `memgit undo` and `memgit rm`.
- [ ] **Portable Bundle Export/Import (`memgit export` / `memgit import`):** Self-contained `.mgit.tar.gz` archives (checkpoint JSON + Git WIP bundle + manifest + SHA256 checksum) for easy `scp`/AirDrop/Slack/Git handoff between MacBooks and Linux work nodes.
- [ ] **Schema guardrails & Tier-1 fallback:** Detect unknown SQLite/protobuf/IndexedDB schema changes and cleanly fall back to Tier-1 markdown/CLI handoff with actionable diagnostics.

### Milestone 3 — Packaging, Installers & Automated Release (~1 week)
- [ ] **Release workflow (`.github/workflows/release.yml`):** Automated cross-OS test matrix, wheel/sdist build, standalone PyInstaller archives (`macos-arm64/x86_64`, `linux-x86_64/arm64`), and `SHA256SUMS`.
- [ ] **Installable packages:**
  - PyPI release (`pipx install memgit` / `uv tool install memgit`).
  - Homebrew formula (`codemarsyu/homebrew-tap`).
  - `install.sh` one-liner script detecting OS/arch and verifying checksums.
  - Native `.deb` / `.rpm` (Linux via `nfpm`) and macOS `.pkg` (via `pkgbuild`).
- [ ] **Shell completions & CLI docs:** `memgit completion {zsh,bash,fish}`, manpage/README quickstart, and version command (`memgit --version`).

### Milestone 4 — Ecosystem & Power Features (Post-v1.0)
- [ ] **Agent Skill / MCP Server (`memgit mcp` / `memgit install-skill`):** Let users trigger handoffs directly from inside Claude Code, Antigravity, or Codex chat.
- [ ] **Authenticated Remote Sync (`memgit push` / `memgit pull`):** Replace `MockRemoteMemGitServer` with token/SSH-authenticated sync or Git-backed remote (`refs/memgit/*`).
- [ ] **macOS Code Signing & Notarization:** Sign `.pkg` and macOS binaries with an Apple Developer ID certificate to eliminate Gatekeeper prompts on other MacBooks.
- [ ] **Interactive TUI (`memgit tui`):** Optional curses/Textual interface for browsing conversations, inspecting diffs, and triggering handoffs visually.

---

## 7. Key Risks & Mitigations

| Risk | Impact | Mitigation |
| :--- | :--- | :--- |
| **Upstream app changes internal DB/protobuf/IndexedDB format** | Native UI resumption breaks after an Antigravity, Claude, or Cursor update. | Two-tier adapter contract: always write Tier-1 portable files (`GEMINI.md`, `CLAUDE.md`, `AGENTS.md`, `CHAT_HISTORY.md`); treat Tier-2 DB sync as guarded best-effort with schema checks + backups. |
| **Accidental secret leakage in checkpoints** | API keys or credentials copied between machines or committed to Git. | Mandatory secret scanner in `sanitizer.py` (blocking/masking by default); `.memgit/` gitignored by default. |
| **macOS Gatekeeper blocking unsigned binaries on other MacBooks** | Friction installing on teammates' MacBooks. | Distribute via Homebrew / `pipx` first (avoids Gatekeeper friction); add Apple Developer ID signing + notarization in Milestone 4 for `.pkg` releases. |
| **SQLite lock contention with running desktop apps** | Partial writes or `database is locked` errors. | Always use `sqlite3.Connection.backup()` for WAL reads, atomic temp-file writes + `replace()`, and explicit timeout/retry handling. |

---

## 8. Definition of Done for v1.0 Release

1. A fresh MacBook (`arm64`) and a fresh Ubuntu Linux (`x86_64`) work node can each install `memgit` in **one command** without cloning the repository.
2. `memgit doctor` accurately reports installed agents and readiness on both OSes.
3. `memgit ls` lists local conversations across all detected agents; `memgit handoff` completes an end-to-end cross-agent transfer in a single command with automatic secret redaction and backup.
4. `memgit export` on a MacBook + `scp` + `memgit import && memgit resume` on a Linux work node restores both conversation memory and Git WIP cleanly.
5. Automated CI passes on macOS and Linux across all supported Python versions, and tagged GitHub Releases publish verified checksums, wheels, standalone tarballs, `.deb`, and `.pkg` artifacts.
