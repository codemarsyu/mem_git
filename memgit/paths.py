"""Cross-platform (macOS & Linux) path resolution and local agent discovery for MemGit CLI."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import os
from pathlib import Path
import platform
import shutil
from typing import Dict, List, Optional


def current_os_name(os_override: Optional[str] = None) -> str:
    """Return canonical OS name: 'Darwin' or 'Linux' (or platform.system() fallback)."""
    raw = (os_override or platform.system()).strip().lower()
    if raw in ("darwin", "macos", "osx"):
        return "Darwin"
    if raw == "linux":
        return "Linux"
    return os_override or platform.system()


def xdg_config_home(user_home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    env_map = env if env is not None else os.environ
    xdg = env_map.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser()
    return user_home / ".config"


def xdg_data_home(user_home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    env_map = env if env is not None else os.environ
    xdg = env_map.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser()
    return user_home / ".local" / "share"


def memgit_home_dir(user_home: Path) -> Path:
    return user_home / ".memgit"


def memgit_checkpoints_dir(user_home: Path) -> Path:
    return memgit_home_dir(user_home) / "checkpoints"


def memgit_backups_dir(user_home: Path) -> Path:
    return memgit_home_dir(user_home) / "backups"


def memgit_config_path(user_home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    return xdg_config_home(user_home, env=env) / "memgit" / "config.toml"


def claude_desktop_support_dir(
    user_home: Path,
    os_name: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Path:
    os_norm = current_os_name(os_name)
    if os_norm == "Darwin":
        return user_home / "Library" / "Application Support" / "Claude"
    return xdg_config_home(user_home, env=env) / "Claude"


def claude_desktop_indexeddb_dir(
    user_home: Path,
    os_name: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Path:
    return claude_desktop_support_dir(user_home, os_name=os_name, env=env) / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"


def claude_code_root_dir(user_home: Path) -> Path:
    return user_home / ".claude"


def antigravity_app_dir(user_home: Path) -> Path:
    return user_home / ".gemini" / "antigravity"


def antigravity_cli_dir(user_home: Path) -> Path:
    return user_home / ".gemini" / "antigravity-cli"


def gemini_cli_dir(user_home: Path) -> Path:
    return user_home / ".gemini"


def codex_cli_dir(user_home: Path) -> Path:
    return user_home / ".codex"


def cursor_storage_dir(
    user_home: Path,
    os_name: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Path:
    os_norm = current_os_name(os_name)
    if os_norm == "Darwin":
        return user_home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
    return xdg_config_home(user_home, env=env) / "Cursor" / "User" / "globalStorage"


def find_cli_binary(binary_name: str, user_home: Optional[Path] = None) -> Optional[str]:
    """Locate a CLI binary on PATH or common user-local install locations (~/.local/bin, /opt/homebrew/bin)."""
    found = shutil.which(binary_name)
    if found:
        return found
    home = user_home or Path.home()
    candidates = [
        home / ".local" / "bin" / binary_name,
        Path("/opt/homebrew/bin") / binary_name,
        Path("/usr/local/bin") / binary_name,
    ]
    for cand in candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


@dataclass
class AgentInstallationStatus:
    """Diagnostic status of one supported AI agent on the local machine."""
    agent_id: str
    display_name: str
    installed: bool
    storage_dir: str
    storage_exists: bool
    cli_binary_name: Optional[str] = None
    cli_binary_path: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, object]:
        return dataclasses.asdict(self)


def detect_installed_agents(
    user_home: Path,
    os_name: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> List[AgentInstallationStatus]:
    """Inspect standard macOS/Linux storage locations and CLI binaries for all supported agents."""
    home = user_home.resolve()
    os_norm = current_os_name(os_name)

    cd_idb = claude_desktop_indexeddb_dir(home, os_name=os_norm, env=env)
    cd_root = claude_desktop_support_dir(home, os_name=os_norm, env=env)
    cc_root = claude_code_root_dir(home)
    agy_app = antigravity_app_dir(home)
    gem_dir = gemini_cli_dir(home)
    codex_dir = codex_cli_dir(home)
    cursor_dir = cursor_storage_dir(home, os_name=os_norm, env=env)

    claude_bin = find_cli_binary("claude", home)
    agy_bin = find_cli_binary("agy", home)
    gemini_bin = find_cli_binary("gemini", home)
    codex_bin = find_cli_binary("codex", home)

    statuses = [
        AgentInstallationStatus(
            agent_id="claude-desktop",
            display_name="Claude Desktop",
            installed=cd_idb.exists() or cd_root.exists(),
            storage_dir=str(cd_idb if cd_idb.exists() else cd_root),
            storage_exists=cd_idb.exists(),
            cli_binary_name=None,
            cli_binary_path=None,
            capabilities=["read", "list", "checkpoint"],
            notes="IndexedDB store ready" if cd_idb.exists() else ("App folder found; IndexedDB not initialized" if cd_root.exists() else "Not detected"),
        ),
        AgentInstallationStatus(
            agent_id="claude-code",
            display_name="Claude Code",
            installed=cc_root.exists() or claude_bin is not None,
            storage_dir=str(cc_root),
            storage_exists=cc_root.exists(),
            cli_binary_name="claude",
            cli_binary_path=claude_bin,
            capabilities=["read", "list", "checkpoint", "resume"],
            notes="Storage & CLI ready" if (cc_root.exists() and claude_bin) else ("Storage ready (CLI optional)" if cc_root.exists() else ("CLI installed" if claude_bin else "Not detected")),
        ),
        AgentInstallationStatus(
            agent_id="antigravity",
            display_name="Google Antigravity",
            installed=agy_app.exists() or agy_bin is not None,
            storage_dir=str(agy_app),
            storage_exists=agy_app.exists(),
            cli_binary_name="agy",
            cli_binary_path=agy_bin,
            capabilities=["read", "list", "checkpoint", "resume", "bootstrap"],
            notes="App storage & agy CLI ready" if (agy_app.exists() and agy_bin) else ("App storage ready; install `agy` for --bootstrap" if agy_app.exists() else ("agy CLI installed" if agy_bin else "Not detected")),
        ),
        AgentInstallationStatus(
            agent_id="gemini-cli",
            display_name="Gemini CLI",
            installed=(gem_dir / "GEMINI.md").exists() or (gem_dir / "memories").exists() or gemini_bin is not None,
            storage_dir=str(gem_dir),
            storage_exists=gem_dir.exists(),
            cli_binary_name="gemini",
            cli_binary_path=gemini_bin,
            capabilities=["read", "list", "checkpoint", "resume", "bootstrap"],
            notes="gemini CLI ready" if gemini_bin else ("~/.gemini present; install `@google/gemini-cli` for --bootstrap" if gem_dir.exists() else "Not detected"),
        ),
        AgentInstallationStatus(
            agent_id="codex",
            display_name="OpenAI Codex CLI",
            installed=codex_dir.exists() or codex_bin is not None,
            storage_dir=str(codex_dir),
            storage_exists=codex_dir.exists(),
            cli_binary_name="codex",
            cli_binary_path=codex_bin,
            capabilities=["read", "list", "checkpoint", "resume"],
            notes="Storage & CLI ready" if (codex_dir.exists() and codex_bin) else ("Storage ready" if codex_dir.exists() else ("CLI installed" if codex_bin else "Not detected")),
        ),
        AgentInstallationStatus(
            agent_id="cursor",
            display_name="Cursor",
            installed=cursor_dir.exists(),
            storage_dir=str(cursor_dir),
            storage_exists=cursor_dir.exists(),
            cli_binary_name="cursor",
            cli_binary_path=find_cli_binary("cursor", home),
            capabilities=["read", "list", "checkpoint", "resume"],
            notes="globalStorage ready" if (cursor_dir / "state.vscdb").exists() else ("Cursor dir present" if cursor_dir.exists() else "Not detected"),
        ),
    ]
    return statuses


def resolve_checkpoint_path(
    ref: str,
    user_home: Path,
    workspace_root: Optional[Path] = None,
) -> Path:
    """Resolve a checkpoint reference (explicit file path, checkpoint ID, or prefix) across:
    1. Explicit path if it exists
    2. Workspace `.memgit/checkpoints/<id>.json`
    3. Global `~/.memgit/checkpoints/<id>.json`
    """
    direct = Path(ref).expanduser()
    if direct.exists() and direct.is_file():
        return direct.resolve()

    stem = ref[:-5] if ref.endswith(".json") else ref
    search_dirs: List[Path] = []
    if workspace_root is not None:
        search_dirs.append(workspace_root.resolve() / ".memgit" / "checkpoints")
    search_dirs.append(memgit_checkpoints_dir(user_home.resolve()))

    for directory in search_dirs:
        if not directory.exists():
            continue
        exact = directory / f"{stem}.json"
        if exact.exists():
            return exact.resolve()
        # Allow unambiguous prefix match (e.g. `chk_1b84`)
        matches = sorted(directory.glob(f"{stem}*.json"))
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            names = ", ".join(m.stem for m in matches[:5])
            raise ValueError(f"Ambiguous checkpoint prefix '{ref}'. Matches: {names}")

    raise FileNotFoundError(
        f"Checkpoint '{ref}' not found as a file path or in {[str(d) for d in search_dirs]}"
    )


def discover_checkpoint_files(
    user_home: Path,
    workspace_root: Optional[Path] = None,
) -> List[Path]:
    """Return deduplicated checkpoint JSON paths sorted newest-first across workspace & global store."""
    seen: Dict[str, Path] = {}
    search_dirs: List[Path] = []
    if workspace_root is not None:
        search_dirs.append(workspace_root.resolve() / ".memgit" / "checkpoints")
    search_dirs.append(memgit_checkpoints_dir(user_home.resolve()))

    for directory in search_dirs:
        if not directory.exists():
            continue
        for fpath in directory.glob("*.json"):
            if fpath.stem not in seen:
                seen[fpath.stem] = fpath.resolve()

    return sorted(seen.values(), key=lambda p: p.stat().st_mtime, reverse=True)
