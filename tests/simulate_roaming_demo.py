"""End-to-End Multi-Machine, Cross-Model Handoff & Server Transformation Simulation.

Simulates on a single development machine:
1. Mock Remote MemGit Server (HTTP REST API + SQLite + Object Store + Compiler + Semantic Merger)
2. Machine A (MacBook Pro): Mock Claude Code (`claude-3-7-sonnet`) with dirty Git working tree,
   `<thinking>` signatures, path-scoped rules, topic memory, private directives, and user deferrals.
3. Machine B (Linux GPU Server): Mock OpenAI Codex CLI (`gpt-5-codex`) pulling & transforming
   Checkpoint A from the remote server, restoring uncommitted Git WIP edits, resuming at Step 2,
   and fixing the Module X concurrency bug.
4. Server-Side Semantic Merge: Merging Machine B's branch into `main` via the Provenance DAG,
   superseding obsolete assumptions (`workers=1` -> `workers=8`) and stripping `M_private` rules.
5. Machine C (Cloud Workstation): Mock Google Antigravity (`gemini-4-argon`) pulling the merged
   checkpoint transformed into Antigravity Knowledge Items & Artifacts, resuming at Step 3.
6. Machine D (Laptop): Mock Cursor (`cursor-claude-sonnet`) pulling into `.cursor/rules/*.mdc`
   and SQLite `state.vscdb`.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict

from memgit.adapters import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexCLIAdapter,
    CursorAdapter,
)
from memgit.client import MemGitClient
from memgit.models import HostFingerprint
from memgit.server.app import MockRemoteMemGitServer


def _init_git_repo_with_base_commit(ws: Path) -> str:
    """Initialize a real Git repository with committed base code files."""
    ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Alice Dev"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.email", "alice@example.com"], cwd=str(ws), check=True)

    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(parents=True, exist_ok=True)

    (ws / "src" / "ledger.py").write_text(
        "def to_cents(dollars: int) -> int:\n    return dollars * 100\n",
        encoding="utf-8",
    )
    (ws / "src" / "worker.py").write_text(
        "def settle_batch(total_cents: int, count: int) -> list[int]:\n"
        "    # Base committed version (has rounding bug)\n"
        "    return [total_cents // count] * count\n",
        encoding="utf-8",
    )
    (ws / "tests" / "test_ledger.py").write_text(
        "import unittest\nfrom src.ledger import to_cents\n\n"
        "class TestLedger(unittest.TestCase):\n"
        "    def test_cents(self):\n"
        "        self.assertEqual(to_cents(5), 500)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(ws), check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial payment engine base commit"],
        cwd=str(ws),
        check=True,
        capture_output=True,
    )
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ws),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def _clone_base_repo(src_ws: Path, dest_ws: Path) -> None:
    """Clone the clean committed base state onto another simulated machine."""
    subprocess.run(
        ["git", "clone", "--quiet", str(src_ws), str(dest_ws)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "config", "user.name", "Remote Worker"], cwd=str(dest_ws), check=True)
    subprocess.run(["git", "config", "user.email", "worker@example.com"], cwd=str(dest_ws), check=True)


def run_full_simulation(verbose: bool = True) -> Dict[str, Any]:
    """Execute the complete multi-node, cross-model handoff simulation and return verification metrics."""
    base_tmp = Path(tempfile.mkdtemp(prefix="memgit_sim_"))
    server_dir = base_tmp / "remote_server_storage"
    node_a_dir = base_tmp / "node_a_macbook_claude"
    node_b_dir = base_tmp / "node_b_linux_codex"
    node_c_dir = base_tmp / "node_c_workstation_antigravity"
    node_d_dir = base_tmp / "node_d_cursor"

    server = MockRemoteMemGitServer(storage_dir=server_dir, host="127.0.0.1", port=0)
    server_url = server.start_background()
    client = MemGitClient(server_url)

    try:
        if verbose:
            print("=" * 80)
            print("MemGit End-to-End Cross-Machine & Cross-Model Handoff Simulation")
            print(f"Mock Remote Server listening at: {server_url}")
            print("=" * 80)

        # -------------------------------------------------------------------------
        # STEP 1: Node A (MacBook Pro) running Claude Code (`claude-3-7-sonnet`)
        # -------------------------------------------------------------------------
        ws_a = node_a_dir / "workspace"
        home_a = node_a_dir / "home"
        base_git_commit = _init_git_repo_with_base_commit(ws_a)

        # Clone clean base commit to Node B, Node C, Node D before making dirty WIP edits on Node A
        ws_b = node_b_dir / "workspace"
        home_b = node_b_dir / "home"
        ws_c = node_c_dir / "workspace"
        home_c = node_c_dir / "home"
        ws_d = node_d_dir / "workspace"
        home_d = node_d_dir / "home"
        _clone_base_repo(ws_a, ws_b)
        _clone_base_repo(ws_a, ws_c)
        _clone_base_repo(ws_a, ws_d)

        # Now create dirty uncommitted WIP edits on Node A (Scenario 1 Dual-Stream Atomic Sync)
        (ws_a / "src" / "worker.py").write_text(
            "def settle_batch(total_cents: int, count: int) -> list[int]:\n"
            "    # WIP unstaged edit from Node A (Claude Code): largest remainder draft\n"
            "    base, rem = divmod(total_cents, count)\n"
            "    return [base + (1 if i < rem else 0) for i in range(count)]\n",
            encoding="utf-8",
        )
        (ws_a / "src" / "retry.py").write_text(
            "# Untracked WIP file created on Node A\n"
            "BACKOFF_BASE_SECONDS = 0.5\n"
            "BACKOFF_MAX_SECONDS = 8.0\n",
            encoding="utf-8",
        )

        # Seed realistic Claude Code memory & session JSONL transcript on Node A
        ClaudeCodeAdapter.seed_mock_claude_session(ws_a, home_a, repo_name="payment-engine")

        host_a = HostFingerprint(
            os_name="Darwin",
            arch="arm64",
            workspace_root=str(ws_a),
            user_home=str(home_a),
            shell="zsh",
            cuda_available=False,
            gpu_device="Apple M3 Max MPS",
            hostname="alice-macbook.local",
        )
        claude_adapter = ClaudeCodeAdapter()
        chk_a = claude_adapter.extract_checkpoint(
            workspace_root=ws_a,
            user_home=home_a,
            repo_name="payment-engine",
            author="alice",
            source_model="claude-3-7-sonnet",
            host_fingerprint=host_a,
            message="Mid-task checkpoint from MacBook Claude Code (Step 2 WIP)",
            branch="main",
        )
        push_a_resp = client.push_checkpoint(chk_a)

        if verbose:
            print(f"\n[1] Node A (Claude Code / claude-3-7-sonnet) -> Pushed Checkpoint `{chk_a.checkpoint_id}`")
            print(f"    - Git Base Commit: {chk_a.git_wip.base_commit[:10]} (Dirty WIP Tree: {chk_a.git_wip.is_dirty})")
            print(f"    - Stripped Thinking Blocks: {chk_a.sanitization_stats.stripped_thinking_blocks}")
            print(f"    - Normalized Host Paths: {chk_a.sanitization_stats.normalized_host_paths}")
            print(f"    - Isolated User Deferral Locks: {len(chk_a.user_deferrals)}")
            print(f"    - Facts Captured: {len(chk_a.confirmed_facts)} ({chk_a.sanitization_stats.shared_facts_count} shared, {chk_a.sanitization_stats.private_facts_count} private)")

        # -------------------------------------------------------------------------
        # STEP 2: Node B (Linux GPU Server) running OpenAI Codex CLI (`gpt-5-codex`)
        #         Single-User Roaming (`include_private=True`)
        # -------------------------------------------------------------------------
        host_b = HostFingerprint(
            os_name="Linux",
            arch="x86_64",
            workspace_root=str(ws_b),
            user_home=str(home_b),
            shell="bash",
            cuda_available=True,
            gpu_device="NVIDIA A100-SXM4-80GB",
            hostname="gpu-node-04.internal",
        )
        pull_b_resp = client.pull_and_restore(
            repo_name="payment-engine",
            ref="main",
            target_agent="codex",
            target_model="gpt-5-codex",
            target_host=host_b,
            include_private=True,
            restore_code_wip=True,
        )
        codex_adapter = CodexCLIAdapter()
        boot_b = codex_adapter.simulate_boot_and_load(
            workspace_root=ws_b,
            user_home=home_b,
            repo_name="payment-engine",
            model_name="gpt-5-codex",
            forbidden_source_paths=[str(ws_a), str(home_a)],
        )

        if verbose:
            print(f"\n[2] Node B (OpenAI Codex CLI / gpt-5-codex on Linux GPU) <- Pulled & Transformed `{chk_a.checkpoint_id}`")
            print(f"    - Server Compilation Note: {pull_b_resp['compilation_notes'][0]}")
            print(f"    - Uncommitted Git WIP Restored: unstaged={pull_b_resp['git_wip_report']['unstaged_applied']}, untracked_files={pull_b_resp['git_wip_report']['untracked_restored_count']}")
            print(f"    - Codex Boot Check: within_32KiB_budget={boot_b.within_budget_limits}, thinking_leaks={boot_b.foreign_thinking_leaks}, source_path_leaks={boot_b.source_path_leaks}")
            print(f"    - Resumed Execution Step: `{boot_b.resumed_step_id}` ({boot_b.resumed_step_title})")

        # -------------------------------------------------------------------------
        # STEP 3: Node B (Codex CLI) finishes Step 2, fixes Module X leak, pushes branch
        #         & Server executes Provenance DAG Semantic Merge + Privacy Filter
        # -------------------------------------------------------------------------
        CodexCLIAdapter.seed_mock_codex_update(ws_b, home_b, repo_name="payment-engine")
        chk_b = codex_adapter.extract_checkpoint(
            workspace_root=ws_b,
            user_home=home_b,
            repo_name="payment-engine",
            author="bob",
            source_model="gpt-5-codex",
            host_fingerprint=host_b,
            message="Fixed Module X leak (enable workers=8) and completed Step 2 rounding",
            branch="feat/codex-fix",
            parent_checkpoint_id=chk_a.checkpoint_id,
        )
        client.push_checkpoint(chk_b)

        merge_resp = client.merge_remote(
            repo_name="payment-engine",
            base_ref="main",
            incoming_ref="feat/codex-fix",
            author="team-lead",
            target_branch="main",
            strip_private=True,
        )
        merge_report = merge_resp["merge_report"]

        if verbose:
            print(f"\n[3] Remote Server Semantic Merge (`main` + `feat/codex-fix` -> `{merge_report['merged_checkpoint_id']}`)")
            print(f"    - Memory PR Summary: {merge_report['summary_headline']}")
            for res in merge_report["resolutions"]:
                print(
                    f"    - Semantic Conflict Resolved on `{res['entity_key']}`:\n"
                    f"      * SUPERSEDED: \"{res['superseded_statement']}\" [{res['superseded_author_model']}]\n"
                    f"      * ACTIVE WINNER: \"{res['winner_statement']}\" [{res['winner_author_model']}]\n"
                    f"      * Reason: {res['reason']}"
                )

        # -------------------------------------------------------------------------
        # STEP 4: Node C (Cloud Workstation) running Google Antigravity (`gemini-4-argon`)
        #         Multi-User Team Pull (`include_private=False`)
        # -------------------------------------------------------------------------
        host_c = HostFingerprint(
            os_name="Linux",
            arch="x86_64",
            workspace_root=str(ws_c),
            user_home=str(home_c),
            shell="zsh",
            cuda_available=True,
            gpu_device="NVIDIA H100-80GB",
            hostname="carol-antigravity-cloud",
        )
        pull_c_resp = client.pull_and_restore(
            repo_name="payment-engine",
            ref="main",
            target_agent="antigravity",
            target_model="gemini-4-argon",
            target_host=host_c,
            include_private=False,
            restore_code_wip=True,
        )
        ag_adapter = AntigravityAdapter()
        boot_c = ag_adapter.simulate_boot_and_load(
            workspace_root=ws_c,
            user_home=home_c,
            repo_name="payment-engine",
            model_name="gemini-4-argon",
            forbidden_source_paths=[str(ws_a), str(home_a), str(ws_b), str(home_b)],
        )

        if verbose:
            print(f"\n[4] Node C (Google Antigravity / gemini-4-argon) <- Pulled & Transformed `{pull_c_resp['checkpoint_id']}`")
            print(f"    - Server Compilation Note: {pull_c_resp['compilation_notes'][0]}")
            print(f"    - Antigravity Boot Check: AGENTS.md_lines={boot_c.memory_index_lines}/150, thinking_leaks={boot_c.foreign_thinking_leaks}, source_path_leaks={boot_c.source_path_leaks}")
            print(f"    - Active Constraints Loaded: {len(boot_c.active_constraints_loaded)} (Superseded blocked: {len(boot_c.superseded_constraints_blocked)})")
            print(f"    - User Deferral Locks Enforced: {len(boot_c.user_deferrals_enforced)}")
            print(f"    - Resumed Execution Step: `{boot_c.resumed_step_id}` ({boot_c.resumed_step_title})")

        # -------------------------------------------------------------------------
        # STEP 5: Node D (Laptop) running Cursor (`cursor-claude-sonnet`)
        #         Verifying `.cursor/rules/*.mdc` + SQLite `state.vscdb` transformation
        # -------------------------------------------------------------------------
        host_d = HostFingerprint(
            os_name="Darwin",
            arch="arm64",
            workspace_root=str(ws_d),
            user_home=str(home_d),
            shell="zsh",
            cuda_available=False,
            gpu_device="Apple M4 Pro",
            hostname="dave-cursor-mac",
        )
        pull_d_resp = client.pull_and_restore(
            repo_name="payment-engine",
            ref="main",
            target_agent="cursor",
            target_model="cursor-claude-sonnet",
            target_host=host_d,
            include_private=False,
            restore_code_wip=True,
        )
        cursor_adapter = CursorAdapter()
        boot_d = cursor_adapter.simulate_boot_and_load(
            workspace_root=ws_d,
            user_home=home_d,
            repo_name="payment-engine",
            model_name="cursor-claude-sonnet",
            forbidden_source_paths=[str(ws_a), str(home_a), str(ws_b), str(home_b)],
        )

        if verbose:
            print(f"\n[5] Node D (Cursor / cursor-claude-sonnet) <- Pulled & Transformed `{pull_d_resp['checkpoint_id']}`")
            print(f"    - Server Compilation Note: {pull_d_resp['compilation_notes'][0]}")
            print(f"    - Resumed Execution Step: `{boot_d.resumed_step_id}` ({boot_d.resumed_step_title})")

        # -------------------------------------------------------------------------
        # STEP 6: Also verify Reverse Handoff into Claude Code (200-line MEMORY.md cap)
        # -------------------------------------------------------------------------
        node_e_dir = base_tmp / "node_e_claude_return"
        ws_e = node_e_dir / "workspace"
        home_e = node_e_dir / "home"
        _clone_base_repo(ws_a, ws_e)
        host_e = HostFingerprint(
            os_name="Darwin",
            arch="arm64",
            workspace_root=str(ws_e),
            user_home=str(home_e),
            shell="zsh",
        )
        pull_e_resp = client.pull_and_restore(
            repo_name="payment-engine",
            ref="main",
            target_agent="claude-code",
            target_model="claude-4-opus",
            target_host=host_e,
            include_private=False,
            restore_code_wip=True,
        )
        boot_e = claude_adapter.simulate_boot_and_load(
            workspace_root=ws_e,
            user_home=home_e,
            repo_name="payment-engine",
            model_name="claude-4-opus",
            forbidden_source_paths=[str(ws_a), str(home_a), str(ws_b), str(home_b)],
        )

        blame_records = client.blame("payment-engine", ref="main")
        if verbose:
            print(f"\n[6] Cognitive Blame Audit Trail on `main` ({len(blame_records)} tracked nodes):")
            for rec in blame_records:
                print(
                    f"    - [{rec['status']}] `{rec['entity_key']}` by {rec['author']} "
                    f"({rec['source_agent']}/{rec['source_model']})"
                )
            print("=" * 80)

        return {
            "base_git_commit": base_git_commit,
            "chk_a": chk_a.to_dict(),
            "push_a": push_a_resp,
            "pull_b": pull_b_resp,
            "boot_b_codex": boot_b.to_dict(),
            "chk_b": chk_b.to_dict(),
            "merge_report": merge_report,
            "pull_c": pull_c_resp,
            "boot_c_antigravity": boot_c.to_dict(),
            "pull_d": pull_d_resp,
            "boot_d_cursor": boot_d.to_dict(),
            "pull_e": pull_e_resp,
            "boot_e_claude": boot_e.to_dict(),
            "blame": blame_records,
            "ws_b_worker_code": (ws_b / "src" / "worker.py").read_text(encoding="utf-8"),
            "ws_b_retry_exists": (ws_b / "src" / "retry.py").exists(),
        }
    finally:
        server.stop()
        shutil.rmtree(base_tmp, ignore_errors=True)


if __name__ == "__main__":
    run_full_simulation(verbose=True)
