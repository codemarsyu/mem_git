"""Command-Line Interface (`memgit`) for Agent Memory Checkpointing, Server Sync,
Cross-Model Handoff Transformation, Semantic Merge, Cognitive Blame, and End-to-End Demo.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
from typing import List, Optional

from memgit.adapters import get_adapter
from memgit.client import MemGitClient
from memgit.models import HostFingerprint, MemoryCheckpointIR
from memgit.server.app import MockRemoteMemGitServer


def _default_host_fingerprint(workspace: Path, home: Path) -> HostFingerprint:
    return HostFingerprint(
        os_name=platform.system(),
        arch=platform.machine(),
        workspace_root=str(workspace.resolve()),
        user_home=str(home.resolve()),
        shell="zsh",
        cuda_available=False,
        gpu_device="Apple M-Series MPS" if platform.system() == "Darwin" else "CPU",
        hostname=platform.node() or "localhost",
    )


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
    kwargs = dict(
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
    if getattr(args, "conversation_id", None) and args.agent in ("antigravity", "gemini"):
        kwargs["conversation_id"] = args.conversation_id
    chk = adapter.extract_checkpoint(**kwargs)
    local_dir = Path(args.output_dir).resolve() if getattr(args, "output_dir", None) else (ws / ".memgit" / "checkpoints")
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
        from memgit.models import MemoryCheckpointIR

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
    chk = converter.load_checkpoint_file(Path(args.checkpoint).resolve())
    ws = Path(args.workspace).resolve()
    home = Path(args.home).resolve()
    fp = HostFingerprint(
        os_name=args.target_os or platform.system(),
        arch=args.target_arch or platform.machine(),
        workspace_root=str(ws),
        user_home=str(home),
        shell="bash" if (args.target_os or "").lower() == "linux" else "zsh",
        cuda_available=bool(args.cuda),
        gpu_device="NVIDIA GPU" if args.cuda else ("Apple M-Series MPS" if platform.system() == "Darwin" else "CPU"),
        hostname=platform.node() or "localhost",
    )
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
        description="MemGit: Cross-Machine, Cross-Model Agent Memory Dropbox & Distributed Version Control",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
    p_chk.add_argument("--agent", required=True, choices=["claude-code", "codex", "antigravity", "gemini-cli", "cursor"])
    p_chk.add_argument("--model", required=True, help="Source LLM model name")
    p_chk.add_argument("--author", default="developer", help="Checkpoint author")
    p_chk.add_argument("--branch", default="main", help="Branch name")
    p_chk.add_argument("--parent", default=None, help="Parent checkpoint ID")
    p_chk.add_argument("--message", "-m", default="MemGit checkpoint", help="Commit message")
    p_chk.add_argument("--conversation-id", default=None, help="Specific Antigravity conversation ID to extract")
    p_chk.add_argument("--output-dir", default=None, help="Output directory for checkpoint JSON")
    p_chk.add_argument("--server-url", default=None, help="Optional Remote Server URL to push immediately")
    p_chk.set_defaults(func=cmd_checkpoint)

    # convert
    p_conv = sub.add_parser("convert", help="Convert a checkpoint JSON into target agent format (Claude/Codex/Antigravity/Gemini CLI) and install")
    p_conv.add_argument("--checkpoint", required=True, help="Path to MemoryCheckpointIR JSON file")
    p_conv.add_argument("--target-agent", required=True, choices=["claude-code", "codex", "antigravity", "gemini-cli", "cursor"])
    p_conv.add_argument("--target-model", required=True, help="Target LLM model name (e.g., claude-4-opus, gpt-5-codex, gemini-3-pro)")
    p_conv.add_argument("--workspace", required=True, help="Target workspace root directory")
    p_conv.add_argument("--home", default=str(Path.home()), help="Target user home directory")
    p_conv.add_argument("--target-os", default=None, help="Override target OS fingerprint")
    p_conv.add_argument("--target-arch", default=None, help="Override target CPU arch")
    p_conv.add_argument("--cuda", action="store_true", help="Indicate target machine has CUDA GPU")
    p_conv.add_argument("--strip-private", action="store_true", help="Strip M_private personal directives")
    p_conv.add_argument("--no-verify", action="store_true", help="Skip post-write target agent boot simulation")
    p_conv.add_argument("--bootstrap", action="store_true", help="Auto-run destination CLI (`agy` for antigravity, `gemini` for gemini-cli) to seed a native resumable session")
    p_conv.add_argument("--bootstrap-bin", default=None, help="Optional explicit path to the destination CLI binary")
    p_conv.add_argument("--bootstrap-timeout", type=int, default=120, help="Timeout in seconds for CLI session bootstrap")
    p_conv.add_argument("--create-project", action="store_true", help="Create a new Antigravity sidebar Project (default: resume as standalone conversation outside-of-project)")
    p_conv.set_defaults(func=cmd_convert)

    # pull
    p_pull = sub.add_parser("pull", help="Pull, server-transform, and restore checkpoint on destination node")
    p_pull.add_argument("--workspace", required=True, help="Target workspace root path")
    p_pull.add_argument("--home", required=True, help="Target user home path")
    p_pull.add_argument("--repo", required=True, help="Repository identifier")
    p_pull.add_argument("--ref", default="main", help="Branch name or checkpoint ID")
    p_pull.add_argument("--target-agent", required=True, choices=["claude-code", "codex", "antigravity", "gemini-cli", "cursor"])
    p_pull.add_argument("--target-model", required=True, help="Destination LLM model name")
    p_pull.add_argument("--server-url", required=True, help="Remote MemGit Server URL")
    p_pull.add_argument("--target-os", default=None, help="Override target OS fingerprint")
    p_pull.add_argument("--target-arch", default=None, help="Override target CPU arch")
    p_pull.add_argument("--cuda", action="store_true", help="Indicate target machine has CUDA GPU")
    p_pull.add_argument("--hostname", default=None, help="Target hostname")
    p_pull.add_argument("--strip-private", action="store_true", help="Strip M_private personal directives")
    p_pull.add_argument("--no-code-wip", action="store_true", help="Skip Git WIP patch restoration")
    p_pull.add_argument("--bootstrap", action="store_true", help="Auto-run destination CLI (`agy` for antigravity, `gemini` for gemini-cli) to seed a native resumable session")
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
