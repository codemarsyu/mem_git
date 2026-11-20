"""Command-Line Interface (`memgit`) for Cross-Agent & Cross-Machine AI Agent Memory Management.

Everyday Product Commands:
- `memgit doctor`      Detect installed agents, OS storage paths, CLI binaries, and readiness
- `memgit ls`          List conversations across all installed agents (or saved checkpoints)
- `memgit handoff`     One-step extract -> sanitize -> transform -> backup -> resume
- `memgit resume`      Resume a saved checkpoint ID/path into a target agent
- `memgit export`      Package a checkpoint into a portable, secret-redacted `.mgit.tar.gz` bundle
- `memgit import`      Verify SHA256 and install a `.mgit.tar.gz` bundle on another Mac/Linux node
- `memgit show` / `rm` Inspect or delete saved checkpoints
- `memgit undo`        Roll back the last target-agent file/DB mutation from atomic backup
- `memgit completion`  Emit shell completion scripts (`zsh`, `bash`, `fish`)

Plumbing & Sync Commands (backwards-compatible):
- `checkpoint`, `convert`, `pull`, `merge`, `blame`, `server`, `demo`
"""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import platform
import re
import sys
from typing import Any, Dict, List, Optional

from memgit import __version__
from memgit.adapters import get_adapter, list_all_adapters
from memgit.backup import BackupManager
from memgit.bundle import export_checkpoint_bundle, import_checkpoint_bundle
from memgit.client import MemGitClient
from memgit.models import AgentType, HostFingerprint, MemoryCheckpointIR
from memgit.paths import (
    detect_installed_agents,
    discover_checkpoint_files,
    memgit_backups_dir,
    memgit_checkpoints_dir,
    memgit_config_path,
    resolve_checkpoint_path,
)
from memgit.sanitizer import redact_checkpoint_secrets
from memgit.server.app import MockRemoteMemGitServer

SOURCE_AGENTS = ["claude-desktop", "claude-code", "codex", "antigravity", "gemini-cli", "cursor"]
TARGET_AGENTS = ["claude-code", "codex", "antigravity", "gemini-cli", "cursor"]

DEFAULT_MODELS: Dict[str, str] = {
    "claude-desktop": "claude-sonnet-4",
    "claude-code": "claude-sonnet-4",
    "claude": "claude-sonnet-4",
    "antigravity": "gemini-3-pro",
    "agy": "gemini-3-pro",
    "gemini-cli": "gemini-3-pro",
    "gemini": "gemini-3-pro",
    "codex": "gpt-5-codex",
    "cursor": "claude-sonnet-4",
}


def _default_host_fingerprint(workspace: Path, home: Path, target_os: Optional[str] = None, target_arch: Optional[str] = None, cuda: bool = False) -> HostFingerprint:
    os_name = target_os or platform.system()
    arch = target_arch or platform.machine()
    return HostFingerprint(
        os_name=os_name,
        arch=arch,
        workspace_root=str(workspace.resolve()),
        user_home=str(home.resolve()),
        shell="bash" if os_name.lower() == "linux" else "zsh",
        cuda_available=bool(cuda),
        gpu_device="NVIDIA GPU" if cuda else ("Apple M-Series MPS" if os_name == "Darwin" else "CPU"),
        hostname=platform.node() or "localhost",
    )


def _format_table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    for r in rows:
        lines.append(fmt.format(*r))
    return "\n".join(lines)


def _truncate(text: str, width: int = 52) -> str:
    single = " ".join((text or "").split())
    return (single[: width - 3] + "...") if len(single) > width else single


# ---------------------------------------------------------------------------
# Product Commands (v1.0)
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    statuses = detect_installed_agents(home, os_name=args.os)
    ck_dir = memgit_checkpoints_dir(home)
    bk_dir = memgit_backups_dir(home)
    cfg_path = memgit_config_path(home)
    report = {
        "memgit_version": __version__,
        "os": args.os or platform.system(),
        "arch": platform.machine(),
        "python_version": platform.python_version(),
        "user_home": str(home),
        "checkpoints_dir": str(ck_dir),
        "checkpoints_count": len(list(ck_dir.glob("*.json"))) if ck_dir.exists() else 0,
        "backups_dir": str(bk_dir),
        "backups_count": len(BackupManager(home).list_backups()),
        "config_path": str(cfg_path),
        "agents": [s.to_dict() for s in statuses],
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"MemGit Doctor v{__version__} ({report['os']}/{report['arch']}, Python {report['python_version']})")
    print(f"Checkpoints: {report['checkpoints_dir']} ({report['checkpoints_count']} saved)")
    print(f"Backups:     {report['backups_dir']} ({report['backups_count']} snapshots)")
    print("")
    rows = []
    for s in statuses:
        status_str = "READY" if s.installed else "missing"
        cli_str = s.cli_binary_path or (f"{s.cli_binary_name} (not found)" if s.cli_binary_name else "-")
        rows.append([s.agent_id, status_str, _truncate(cli_str, 34), _truncate(s.notes, 44)])
    print(_format_table(["AGENT", "STATUS", "CLI", "NOTES"], rows))
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()

    if args.checkpoints:
        files = discover_checkpoint_files(home, workspace_root=ws)
        items: List[Dict[str, Any]] = []
        for fpath in files:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                msg = str(data.get("message", ""))
                if args.query and args.query.lower() not in msg.lower() and args.query.lower() not in fpath.stem.lower():
                    continue
                items.append(
                    {
                        "checkpoint_id": data.get("checkpoint_id", fpath.stem),
                        "source_agent": data.get("source_agent", ""),
                        "message": msg,
                        "turns": len(data.get("conversation_history", [])),
                        "created_at": data.get("created_at", ""),
                        "path": str(fpath),
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue
        items = items[: args.limit]
        if args.json:
            print(json.dumps(items, indent=2))
            return 0
        rows = [
            [
                it["checkpoint_id"],
                it["source_agent"],
                str(it["turns"]),
                it["created_at"][:19],
                _truncate(it["message"], 48),
            ]
            for it in items
        ]
        print(_format_table(["CHECKPOINT", "SOURCE", "TURNS", "CREATED", "TITLE"], rows))
        return 0

    adapters = [get_adapter(args.agent)] if args.agent else list_all_adapters()
    all_sessions = []
    for adapter in adapters:
        try:
            all_sessions.extend(adapter.list_sessions(home, workspace_root=ws))
        except Exception:
            continue

    if args.query:
        q = args.query.lower().strip()
        all_sessions = [
            s for s in all_sessions if q in s.title.lower() or q in s.session_id.lower() or q in s.workspace_path.lower()
        ]

    all_sessions.sort(key=lambda s: (s.updated_at, s.turn_count), reverse=True)
    all_sessions = all_sessions[: args.limit]

    if args.json:
        print(json.dumps([s.to_dict() for s in all_sessions], indent=2))
        return 0

    rows = [
        [
            s.agent_type,
            s.session_id[:12],
            str(s.turn_count),
            (s.updated_at or "")[:19],
            _truncate(s.title, 50),
        ]
        for s in all_sessions
    ]
    print(_format_table(["AGENT", "SESSION_ID", "TURNS", "UPDATED", "TITLE"], rows))
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    from memgit.converter import CheckpointConverter

    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve()
    source_agent = args.source_agent.lower().strip()
    target_agent = args.target_agent.lower().strip()
    source_model = args.source_model or DEFAULT_MODELS.get(source_agent, "unknown")
    target_model = args.target_model or DEFAULT_MODELS.get(target_agent, "gemini-3-pro")
    author = args.author or getpass.getuser()

    src_adapter = get_adapter(source_agent)
    src_fp = _default_host_fingerprint(ws, home)
    repo_slug = args.repo or re.sub(r"[^a-z0-9]+", "_", (args.query or ws.name).lower()).strip("_") or "session"

    extract_kwargs: Dict[str, Any] = dict(
        workspace_root=ws,
        user_home=home,
        repo_name=repo_slug,
        author=author,
        source_model=source_model,
        host_fingerprint=src_fp,
        message=args.message or "Checkpoint",
        branch=args.branch,
    )
    if source_agent in ("claude-desktop", "claude-app"):
        if args.conversation_id:
            extract_kwargs["conversation_id"] = args.conversation_id
        elif args.query:
            if re.match(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", args.query, re.IGNORECASE):
                extract_kwargs["conversation_id"] = args.query
            else:
                extract_kwargs["title_query"] = args.query
    elif source_agent in ("antigravity", "agy") and (args.conversation_id or args.query):
        extract_kwargs["conversation_id"] = args.conversation_id or args.query

    chk = src_adapter.extract_checkpoint(**extract_kwargs)
    redact_checkpoint_secrets(chk)

    chk_path: Optional[Path] = None
    if not args.dry_run:
        ck_dir = Path(args.output_dir).resolve() if args.output_dir else memgit_checkpoints_dir(home)
        ck_dir.mkdir(parents=True, exist_ok=True)
        chk_path = ck_dir / f"{chk.checkpoint_id}.json"
        chk_path.write_text(chk.to_json(indent=2), encoding="utf-8")

    target_ws = Path(args.target_workspace).resolve() if args.target_workspace else ws
    target_fp = _default_host_fingerprint(target_ws, home, target_os=args.target_os, target_arch=args.target_arch, cuda=args.cuda)
    res = CheckpointConverter().convert_and_install(
        checkpoint=chk,
        target_agent=target_agent,
        target_model=target_model,
        workspace_root=target_ws,
        user_home=home,
        target_host=target_fp,
        include_private=not args.strip_private,
        verify=not args.no_verify,
        bootstrap_session=bool(args.bootstrap),
        bootstrap_bin=args.bootstrap_bin,
        bootstrap_timeout_s=args.bootstrap_timeout,
        create_project=bool(args.create_project),
        dry_run=bool(args.dry_run),
        enable_backup=not args.no_backup,
    )

    payload = {
        "checkpoint_id": chk.checkpoint_id,
        "checkpoint_path": str(chk_path) if chk_path else None,
        "redacted_secrets": chk.sanitization_stats.redacted_secrets,
        "conversion": res.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Handoff {source_agent} -> {target_agent} ({target_model})")
    print(f"  Title:       {chk.message}")
    print(f"  Checkpoint:  {chk.checkpoint_id}" + (f" ({chk_path})" if chk_path else ""))
    print(f"  Turns:       {len(chk.conversation_history)} | Redacted secrets: {chk.sanitization_stats.redacted_secrets}")
    print(f"  Tier-1/2:    tier1={res.tier1_succeeded}, tier2={res.tier2_status}")
    if res.backup_id:
        print(f"  Backup ID:   {res.backup_id} (run `memgit undo {res.backup_id}` to revert)")
    if res.bootstrap_session and res.bootstrap_session.conversation_url:
        print(f"  Resumed URL: {res.bootstrap_session.conversation_url}")
    for w in res.tier2_warnings:
        print(f"  Warning:     {w}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from memgit.converter import CheckpointConverter

    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve()
    chk_path = resolve_checkpoint_path(args.checkpoint, user_home=home, workspace_root=ws)
    converter = CheckpointConverter()
    chk = converter.load_checkpoint_file(chk_path)
    target_agent = args.target_agent.lower().strip()
    target_model = args.target_model or DEFAULT_MODELS.get(target_agent, "gemini-3-pro")

    fp = _default_host_fingerprint(ws, home, target_os=args.target_os, target_arch=args.target_arch, cuda=args.cuda)
    res = converter.convert_and_install(
        checkpoint=chk,
        target_agent=target_agent,
        target_model=target_model,
        workspace_root=ws,
        user_home=home,
        target_host=fp,
        include_private=not args.strip_private,
        verify=not args.no_verify,
        bootstrap_session=bool(args.bootstrap),
        bootstrap_bin=args.bootstrap_bin,
        bootstrap_timeout_s=args.bootstrap_timeout,
        create_project=bool(args.create_project),
        dry_run=bool(args.dry_run),
        enable_backup=not args.no_backup,
    )
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
        return 0

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Resumed {chk.checkpoint_id} -> {target_agent} ({target_model})")
    print(f"  Title:       {chk.message}")
    print(f"  Workspace:   {ws}")
    print(f"  Files:       {len(res.written_files)} | Tier-2: {res.tier2_status}")
    if res.backup_id:
        print(f"  Backup ID:   {res.backup_id} (run `memgit undo {res.backup_id}` to revert)")
    if res.bootstrap_session and res.bootstrap_session.conversation_url:
        print(f"  Resumed URL: {res.bootstrap_session.conversation_url}")
    for w in res.tier2_warnings:
        print(f"  Warning:     {w}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    chk_path = resolve_checkpoint_path(args.checkpoint, user_home=home, workspace_root=ws)
    data = json.loads(chk_path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    chk = MemoryCheckpointIR.from_dict(data)
    print(f"Checkpoint:  {chk.checkpoint_id}")
    print(f"Path:        {chk_path}")
    print(f"Title:       {chk.message}")
    print(f"Source:      {chk.source_agent} ({chk.source_model}) on {chk.source_host.os_name}/{chk.source_host.arch}")
    print(f"Created:     {chk.created_at}")
    print(f"Facts:       {len(chk.confirmed_facts)} | Topics: {len(chk.topic_memories)} | Turns: {len(chk.conversation_history)}")
    print(f"Sanitized:   {json.dumps(chk.sanitization_stats.to_dict())}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    chk_path = resolve_checkpoint_path(args.checkpoint, user_home=home, workspace_root=ws)
    if args.dry_run:
        out = {"checkpoint_path": str(chk_path), "deleted": False, "dry_run": True}
    else:
        chk_path.unlink()
        out = {"checkpoint_path": str(chk_path), "deleted": True, "dry_run": False}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        verb = "[DRY RUN] Would delete" if args.dry_run else "Deleted"
        print(f"{verb} checkpoint: {chk_path}")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    mgr = BackupManager(home)
    if args.list:
        backups = mgr.list_backups(include_reverted=True)
        if args.json:
            print(json.dumps(backups, indent=2))
            return 0
        rows = [
            [
                b.get("op_id", ""),
                b.get("operation", ""),
                "reverted" if b.get("reverted") else "active",
                str(len(b.get("entries", []))),
                str(b.get("created_at", ""))[:19],
            ]
            for b in backups
        ]
        print(_format_table(["BACKUP_ID", "OPERATION", "STATE", "FILES", "CREATED"], rows))
        return 0

    res = mgr.undo(op_id=args.op_id)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Reverted backup {res['op_id']} ({res['operation']}):")
        print(f"  Restored files: {len(res['restored_files'])}")
        print(f"  Removed files:  {len(res['removed_files'])}")
        if res["errors"]:
            print(f"  Errors:         {res['errors']}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    chk_path = resolve_checkpoint_path(args.checkpoint, user_home=home, workspace_root=ws)
    out_path = Path(args.output) if args.output else Path.cwd() / f"{chk_path.stem}.mgit.tar.gz"
    res = export_checkpoint_bundle(chk_path, out_path, strip_private=bool(args.strip_private))
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Exported bundle: {res['bundle_path']} ({res['bundle_bytes']} bytes)")
        print(f"  Checkpoint:    {res['checkpoint_id']} ({res['conversation_turns']} turns)")
        print(f"  SHA256:        {res['checkpoint_sha256']}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    home = Path(args.home).resolve()
    dest_dir = Path(args.dest_dir).resolve() if args.dest_dir else memgit_checkpoints_dir(home)
    res = import_checkpoint_bundle(Path(args.bundle), dest_dir=dest_dir)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Imported checkpoint {res['checkpoint_id']} -> {res['installed_path']}")
        print(f"  Title:  {res['message']} ({res['conversation_turns']} turns)")
        print(f"  SHA256: {res['sha256']} (verified={res['checksum_verified']})")
    return 0


def cmd_completion(args: argparse.Namespace) -> int:
    subcommands = "doctor ls handoff resume checkpoint convert export import show rm undo completion pull merge blame server demo"
    if args.shell == "zsh":
        print(
            f"""#compdef memgit
_memgit() {{
  local -a commands
  commands=(
    'doctor:Check installed AI agents and paths'
    'ls:List local agent sessions or checkpoints'
    'handoff:One-step extract, sanitize, and resume between agents'
    'resume:Resume a saved checkpoint into a target agent'
    'checkpoint:Extract agent memory into a checkpoint JSON'
    'convert:Convert and install a checkpoint JSON'
    'export:Export a checkpoint as a portable .mgit.tar.gz bundle'
    'import:Import a .mgit.tar.gz bundle onto this machine'
    'show:Inspect a saved checkpoint'
    'rm:Delete a saved checkpoint'
    'undo:Roll back the last target-agent mutation'
    'completion:Generate shell completion scripts'
    'pull:Pull and restore checkpoint from a MemGit server'
    'merge:Semantic merge two checkpoints'
    'blame:Show cognitive blame audit trail'
    'server:Start local MemGit HTTP server'
    'demo:Run end-to-end multi-agent simulation'
  )
  _describe 'command' commands
}}
compdef _memgit memgit"""
        )
    elif args.shell == "bash":
        print(
            f"""_memgit_completions() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  if [[ ${{COMP_CWORD}} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "{subcommands}" -- "$cur") )
  fi
}}
complete -F _memgit_completions memgit"""
        )
    else:  # fish
        for cmd in subcommands.split():
            print(f"complete -c memgit -n '__fish_use_subcommand' -a '{cmd}'")
    return 0


# ---------------------------------------------------------------------------
# Existing Plumbing Commands (backwards-compatible)
# ---------------------------------------------------------------------------


def cmd_server(args: argparse.Namespace) -> int:
    storage_dir = Path(args.storage_dir).resolve()
    srv = MockRemoteMemGitServer(storage_dir=storage_dir, host=args.host, port=args.port)
    print(f"[MemGit Server] Listening on {srv.base_url} (storage: {storage_dir})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[MemGit Server] Shutting down.")
        srv.stop()
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    home = Path(args.home).resolve()
    adapter = get_adapter(args.agent)
    fp = _default_host_fingerprint(ws, home)
    kwargs: Dict[str, Any] = dict(
        workspace_root=ws,
        user_home=home,
        repo_name=args.repo,
        author=args.author,
        source_model=args.model,
        host_fingerprint=fp,
        message=args.message,
        branch=args.branch,
        parent_checkpoint_id=args.parent,
    )
    if getattr(args, "conversation_id", None) and args.agent in ("antigravity", "gemini", "claude-desktop"):
        kwargs["conversation_id"] = args.conversation_id
    if getattr(args, "title_query", None) and args.agent == "claude-desktop":
        kwargs["title_query"] = args.title_query
    chk = adapter.extract_checkpoint(**kwargs)
    redact_checkpoint_secrets(chk)
    local_dir = Path(args.output_dir).resolve() if getattr(args, "output_dir", None) else (
        memgit_checkpoints_dir(home) if getattr(args, "global_store", False) else (ws / ".memgit" / "checkpoints")
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    out_file = local_dir / f"{chk.checkpoint_id}.json"
    out_file.write_text(chk.to_json(indent=2), encoding="utf-8")
    print(f"[MemGit] Created checkpoint {chk.checkpoint_id} -> {out_file}")
    print(f"         Sanitization stats: {json.dumps(chk.sanitization_stats.to_dict())}")

    if args.server_url:
        client = MemGitClient(args.server_url)
        res = client.push_checkpoint(chk)
        print(f"[MemGit] Pushed to remote server ({args.server_url}): {json.dumps(res)}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    home = Path(args.home).resolve()
    fp = HostFingerprint(
        os_name=args.target_os or platform.system(),
        arch=args.target_arch or platform.machine(),
        workspace_root=str(ws),
        user_home=str(home),
        shell="bash" if (args.target_os or "").lower() == "linux" else "zsh",
        cuda_available=bool(args.cuda),
        gpu_device="NVIDIA A100-SXM4-80GB" if args.cuda else "CPU",
        hostname=args.hostname or "target-node",
    )
    client = MemGitClient(args.server_url)
    res = client.pull_and_restore(
        repo_name=args.repo,
        ref=args.ref,
        target_agent=args.target_agent,
        target_model=args.target_model,
        target_host=fp,
        include_private=not args.strip_private,
        restore_code_wip=not args.no_code_wip,
    )
    if args.bootstrap:
        from memgit.converter import CheckpointConverter

        raw_chk = client.fetch_checkpoint(args.repo, res["checkpoint_id"])
        chk = MemoryCheckpointIR.from_dict(raw_chk)
        boot = CheckpointConverter()._bootstrap_target_cli_session(
            checkpoint=chk,
            target_agent=args.target_agent,
            ws_resolved=ws,
            home_resolved=home,
            bootstrap_bin=args.bootstrap_bin,
            timeout_s=args.bootstrap_timeout,
        )
        res["bootstrap_session"] = boot.to_dict()
    print(json.dumps(res, indent=2))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    client = MemGitClient(args.server_url)
    res = client.merge_remote(
        repo_name=args.repo,
        base_ref=args.base,
        incoming_ref=args.incoming,
        author=args.author,
        target_branch=args.target_branch,
        strip_private=not args.keep_private,
    )
    print(json.dumps(res["merge_report"], indent=2))
    return 0


def cmd_blame(args: argparse.Namespace) -> int:
    client = MemGitClient(args.server_url)
    records = client.blame(repo_name=args.repo, ref=args.ref)
    print(json.dumps(records, indent=2))
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    from memgit.converter import CheckpointConverter

    converter = CheckpointConverter()
    home = Path(args.home).resolve()
    ws = Path(args.workspace).resolve()
    chk_path = resolve_checkpoint_path(args.checkpoint, user_home=home, workspace_root=ws)
    chk = converter.load_checkpoint_file(chk_path)
    fp = _default_host_fingerprint(ws, home, target_os=args.target_os, target_arch=args.target_arch, cuda=args.cuda)
    res = converter.convert_and_install(
        checkpoint=chk,
        target_agent=args.target_agent,
        target_model=args.target_model,
        workspace_root=ws,
        user_home=home,
        target_host=fp,
        include_private=not args.strip_private,
        verify=not args.no_verify,
        bootstrap_session=bool(args.bootstrap),
        bootstrap_bin=args.bootstrap_bin,
        bootstrap_timeout_s=args.bootstrap_timeout,
        create_project=bool(args.create_project),
        dry_run=bool(args.dry_run),
        enable_backup=not args.no_backup,
    )
    print(json.dumps(res.to_dict(), indent=2))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from tests.simulate_roaming_demo import run_full_simulation
    run_full_simulation()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memgit",
        description="MemGit: Cross-Agent & Cross-Machine AI Coding Agent Memory Checkpoint, Transform, and Resume CLI",
    )
    parser.add_argument("--version", action="version", version=f"memgit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # doctor
    p_doc = sub.add_parser("doctor", help="Detect installed AI agents, OS storage paths, CLI binaries, and readiness")
    p_doc.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_doc.add_argument("--os", default=None, help="Override OS detection (Darwin or Linux)")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output")
    p_doc.set_defaults(func=cmd_doctor)

    # ls
    p_ls = sub.add_parser("ls", help="List conversations across installed agents or saved checkpoints")
    p_ls.add_argument("--agent", default=None, choices=SOURCE_AGENTS, help="Filter by agent")
    p_ls.add_argument("--query", "-q", default=None, help="Substring filter on title/ID/workspace")
    p_ls.add_argument("--checkpoints", action="store_true", help="List saved MemGit checkpoints instead of agent sessions")
    p_ls.add_argument("--workspace", default=None, help="Workspace path to scan for repo-local checkpoints")
    p_ls.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_ls.add_argument("--limit", type=int, default=25, help="Maximum rows to display")
    p_ls.add_argument("--json", action="store_true", help="Emit JSON output")
    p_ls.set_defaults(func=cmd_ls)

    # handoff (1-step workflow)
    p_ho = sub.add_parser("handoff", help="One-step extract, sanitize, transform, and resume from one agent to another")
    p_ho.add_argument("query", nargs="?", default=None, help="Conversation title substring or session UUID")
    p_ho.add_argument("--from", dest="source_agent", required=True, choices=SOURCE_AGENTS, help="Source agent")
    p_ho.add_argument("--to", dest="target_agent", required=True, choices=TARGET_AGENTS, help="Target agent")
    p_ho.add_argument("--source-model", default=None, help="Source LLM model name")
    p_ho.add_argument("--target-model", default=None, help="Target LLM model name")
    p_ho.add_argument("--workspace", default=".", help="Source (and default target) workspace root")
    p_ho.add_argument("--target-workspace", default=None, help="Optional separate target workspace root")
    p_ho.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_ho.add_argument("--repo", default=None, help="Repository/project slug")
    p_ho.add_argument("--author", default=None, help="Checkpoint author")
    p_ho.add_argument("--branch", default="main", help="Branch name")
    p_ho.add_argument("--message", "-m", default=None, help="Override checkpoint title/message")
    p_ho.add_argument("--conversation-id", default=None, help="Explicit source conversation UUID")
    p_ho.add_argument("--output-dir", default=None, help="Directory to save checkpoint JSON (default: ~/.memgit/checkpoints)")
    p_ho.add_argument("--target-os", default=None, help="Override target OS fingerprint")
    p_ho.add_argument("--target-arch", default=None, help="Override target CPU arch")
    p_ho.add_argument("--cuda", action="store_true", help="Indicate target machine has CUDA GPU")
    p_ho.add_argument("--strip-private", action="store_true", help="Strip M_private personal directives")
    p_ho.add_argument("--no-verify", action="store_true", help="Skip post-write boot simulation")
    p_ho.add_argument("--no-backup", action="store_true", help="Skip pre-write atomic backup")
    p_ho.add_argument("--bootstrap", action="store_true", help="Auto-run destination CLI (`agy`/`gemini`) to create native session")
    p_ho.add_argument("--bootstrap-bin", default=None, help="Explicit path to destination CLI binary")
    p_ho.add_argument("--bootstrap-timeout", type=int, default=120, help="CLI bootstrap timeout in seconds")
    p_ho.add_argument("--create-project", action="store_true", help="Create a new Antigravity sidebar Project")
    p_ho.add_argument("--dry-run", action="store_true", help="Preview files/actions without writing to disk")
    p_ho.add_argument("--json", action="store_true", help="Emit JSON output")
    p_ho.set_defaults(func=cmd_handoff)

    # resume (ergonomic checkpoint -> target agent)
    p_res = sub.add_parser("resume", help="Resume a saved checkpoint (by ID, prefix, or path) into a target agent")
    p_res.add_argument("checkpoint", help="Checkpoint ID, prefix, or JSON file path")
    p_res.add_argument("--to", dest="target_agent", required=True, choices=TARGET_AGENTS, help="Target agent")
    p_res.add_argument("--target-model", default=None, help="Target LLM model name")
    p_res.add_argument("--workspace", default=".", help="Target workspace root directory")
    p_res.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_res.add_argument("--target-os", default=None, help="Override target OS fingerprint")
    p_res.add_argument("--target-arch", default=None, help="Override target CPU arch")
    p_res.add_argument("--cuda", action="store_true", help="Indicate target machine has CUDA GPU")
    p_res.add_argument("--strip-private", action="store_true", help="Strip M_private personal directives")
    p_res.add_argument("--no-verify", action="store_true", help="Skip post-write boot simulation")
    p_res.add_argument("--no-backup", action="store_true", help="Skip pre-write atomic backup")
    p_res.add_argument("--bootstrap", action="store_true", help="Auto-run destination CLI (`agy`/`gemini`) to seed native session")
    p_res.add_argument("--bootstrap-bin", default=None, help="Explicit path to destination CLI binary")
    p_res.add_argument("--bootstrap-timeout", type=int, default=120, help="CLI bootstrap timeout in seconds")
    p_res.add_argument("--create-project", action="store_true", help="Create a new Antigravity sidebar Project")
    p_res.add_argument("--dry-run", action="store_true", help="Preview files/actions without writing to disk")
    p_res.add_argument("--json", action="store_true", help="Emit JSON output")
    p_res.set_defaults(func=cmd_resume)

    # show
    p_show = sub.add_parser("show", help="Inspect a saved checkpoint by ID, prefix, or path")
    p_show.add_argument("checkpoint", help="Checkpoint ID, prefix, or JSON file path")
    p_show.add_argument("--workspace", default=None, help="Workspace path to search")
    p_show.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_show.add_argument("--json", action="store_true", help="Emit JSON output")
    p_show.set_defaults(func=cmd_show)

    # rm
    p_rm = sub.add_parser("rm", help="Delete a saved checkpoint")
    p_rm.add_argument("checkpoint", help="Checkpoint ID, prefix, or JSON file path")
    p_rm.add_argument("--workspace", default=None, help="Workspace path to search")
    p_rm.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_rm.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    p_rm.add_argument("--json", action="store_true", help="Emit JSON output")
    p_rm.set_defaults(func=cmd_rm)

    # undo
    p_undo = sub.add_parser("undo", help="Roll back the last (or specified) target-agent write operation from backup")
    p_undo.add_argument("op_id", nargs="?", default=None, help="Optional backup operation ID (default: most recent)")
    p_undo.add_argument("--list", action="store_true", help="List available backup snapshots")
    p_undo.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_undo.add_argument("--json", action="store_true", help="Emit JSON output")
    p_undo.set_defaults(func=cmd_undo)

    # export
    p_exp = sub.add_parser("export", help="Export a checkpoint as a portable, secret-redacted .mgit.tar.gz bundle")
    p_exp.add_argument("checkpoint", help="Checkpoint ID, prefix, or JSON file path")
    p_exp.add_argument("-o", "--output", default=None, help="Output .mgit.tar.gz path")
    p_exp.add_argument("--strip-private", action="store_true", help="Strip M_private facts/topics before bundling")
    p_exp.add_argument("--workspace", default=None, help="Workspace path to search")
    p_exp.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_exp.add_argument("--json", action="store_true", help="Emit JSON output")
    p_exp.set_defaults(func=cmd_export)

    # import
    p_imp = sub.add_parser("import", help="Verify SHA256 and import a .mgit.tar.gz bundle onto this machine")
    p_imp.add_argument("bundle", help="Path to .mgit.tar.gz bundle")
    p_imp.add_argument("--dest-dir", default=None, help="Destination checkpoint dir (default: ~/.memgit/checkpoints)")
    p_imp.add_argument("--home", default=str(Path.home()), help="User home directory")
    p_imp.add_argument("--json", action="store_true", help="Emit JSON output")
    p_imp.set_defaults(func=cmd_import)

    # completion
    p_comp = sub.add_parser("completion", help="Generate shell completion script (zsh, bash, fish)")
    p_comp.add_argument("shell", choices=["zsh", "bash", "fish"], help="Target shell")
    p_comp.set_defaults(func=cmd_completion)

    # server
    p_srv = sub.add_parser("server", help="Start Mock Remote MemGit HTTP Server")
    p_srv.add_argument("--storage-dir", default=".memgit-server", help="Server storage directory")
    p_srv.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_srv.add_argument("--port", type=int, default=8765, help="Bind port")
    p_srv.set_defaults(func=cmd_server)

    # checkpoint
    p_chk = sub.add_parser("checkpoint", help="Extract agent memory & Git WIP into a Memory IR checkpoint")
    p_chk.add_argument("--workspace", default=".", help="Workspace root path")
    p_chk.add_argument("--home", default=str(Path.home()), help="User home path")
    p_chk.add_argument("--repo", required=True, help="Repository identifier")
    p_chk.add_argument("--agent", required=True, choices=SOURCE_AGENTS)
    p_chk.add_argument("--model", required=True, help="Source LLM model name")
    p_chk.add_argument("--author", default="developer", help="Checkpoint author")
    p_chk.add_argument("--branch", default="main", help="Branch name")
    p_chk.add_argument("--parent", default=None, help="Parent checkpoint ID")
    p_chk.add_argument("--message", "-m", default="MemGit checkpoint", help="Commit message")
    p_chk.add_argument("--conversation-id", default=None, help="Specific conversation ID to extract")
    p_chk.add_argument("--title-query", default=None, help="Conversation title substring (for claude-desktop)")
    p_chk.add_argument("--output-dir", default=None, help="Output directory for checkpoint JSON")
    p_chk.add_argument("--global", dest="global_store", action="store_true", help="Save to ~/.memgit/checkpoints/")
    p_chk.add_argument("--server-url", default=None, help="Optional Remote Server URL to push immediately")
    p_chk.set_defaults(func=cmd_checkpoint)

    # convert
    p_conv = sub.add_parser("convert", help="Convert a checkpoint JSON into target agent format and install")
    p_conv.add_argument("--checkpoint", required=True, help="Path or ID of MemoryCheckpointIR JSON")
    p_conv.add_argument("--target-agent", required=True, choices=TARGET_AGENTS)
    p_conv.add_argument("--target-model", required=True, help="Target LLM model name")
    p_conv.add_argument("--workspace", required=True, help="Target workspace root directory")
    p_conv.add_argument("--home", default=str(Path.home()), help="Target user home directory")
    p_conv.add_argument("--target-os", default=None, help="Override target OS fingerprint")
    p_conv.add_argument("--target-arch", default=None, help="Override target CPU arch")
    p_conv.add_argument("--cuda", action="store_true", help="Indicate target machine has CUDA GPU")
    p_conv.add_argument("--strip-private", action="store_true", help="Strip M_private personal directives")
    p_conv.add_argument("--no-verify", action="store_true", help="Skip post-write target agent boot simulation")
    p_conv.add_argument("--no-backup", action="store_true", help="Skip pre-write atomic backup")
    p_conv.add_argument("--bootstrap", action="store_true", help="Auto-run destination CLI (`agy`/`gemini`) to seed native session")
    p_conv.add_argument("--bootstrap-bin", default=None, help="Optional explicit path to the destination CLI binary")
    p_conv.add_argument("--bootstrap-timeout", type=int, default=120, help="Timeout in seconds for CLI session bootstrap")
    p_conv.add_argument("--create-project", action="store_true", help="Create a new Antigravity sidebar Project")
    p_conv.add_argument("--dry-run", action="store_true", help="Preview files/actions without writing to disk")
    p_conv.set_defaults(func=cmd_convert)

    # pull
    p_pull = sub.add_parser("pull", help="Pull, server-transform, and restore checkpoint on destination node")
    p_pull.add_argument("--workspace", required=True, help="Target workspace root path")
    p_pull.add_argument("--home", required=True, help="Target user home path")
    p_pull.add_argument("--repo", required=True, help="Repository identifier")
    p_pull.add_argument("--ref", default="main", help="Branch name or checkpoint ID")
    p_pull.add_argument("--target-agent", required=True, choices=TARGET_AGENTS)
    p_pull.add_argument("--target-model", required=True, help="Destination LLM model name")
    p_pull.add_argument("--server-url", required=True, help="Remote MemGit Server URL")
    p_pull.add_argument("--target-os", default=None, help="Override target OS fingerprint")
    p_pull.add_argument("--target-arch", default=None, help="Override target CPU arch")
    p_pull.add_argument("--cuda", action="store_true", help="Indicate target machine has CUDA GPU")
    p_pull.add_argument("--hostname", default=None, help="Target hostname")
    p_pull.add_argument("--strip-private", action="store_true", help="Strip M_private personal directives")
    p_pull.add_argument("--no-code-wip", action="store_true", help="Skip Git WIP patch restoration")
    p_pull.add_argument("--bootstrap", action="store_true", help="Auto-run destination CLI (`agy`/`gemini`) to seed native session")
    p_pull.add_argument("--bootstrap-bin", default=None, help="Optional explicit path to the destination CLI binary")
    p_pull.add_argument("--bootstrap-timeout", type=int, default=120, help="Timeout in seconds for CLI session bootstrap")
    p_pull.set_defaults(func=cmd_pull)

    # merge
    p_merge = sub.add_parser("merge", help="Semantic merge two branches/checkpoints via Provenance DAG")
    p_merge.add_argument("--repo", required=True, help="Repository identifier")
    p_merge.add_argument("--base", required=True, help="Base ref or checkpoint ID")
    p_merge.add_argument("--incoming", required=True, help="Incoming ref or checkpoint ID")
    p_merge.add_argument("--author", default="team-merger", help="Merger author")
    p_merge.add_argument("--target-branch", default="main", help="Target branch to update")
    p_merge.add_argument("--keep-private", action="store_true", help="Keep private rules during merge")
    p_merge.add_argument("--server-url", required=True, help="Remote MemGit Server URL")
    p_merge.set_defaults(func=cmd_merge)

    # blame
    p_blame = sub.add_parser("blame", help="Show Cognitive Blame audit trail for a repository ref")
    p_blame.add_argument("--repo", required=True, help="Repository identifier")
    p_blame.add_argument("--ref", default="main", help="Branch or checkpoint ID")
    p_blame.add_argument("--server-url", required=True, help="Remote MemGit Server URL")
    p_blame.set_defaults(func=cmd_blame)

    # demo
    p_demo = sub.add_parser("demo", help="Run full end-to-end multi-node, cross-model handoff simulation")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
