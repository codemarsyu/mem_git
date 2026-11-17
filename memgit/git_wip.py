"""Dual-Stream Atomic Sync: Capturing and restoring dirty Git working trees
(staged diffs, unstaged diffs, and untracked files) alongside cognitive state.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Dict, List, Optional

from memgit.models import GitWipBundle

IGNORED_PREFIXES = (
    ".memgit/",
    ".agent-context/",
    ".claude/",
    ".codex/",
    ".gemini/",
    ".cursor/",
    "CLAUDE.md",
    "GEMINI.md",
    "AGENTS.md",
    "__pycache__/",
)


def _run_git(cwd: Path, args: List[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def is_git_repo(workspace_root: Path) -> bool:
    res = _run_git(workspace_root, ["rev-parse", "--is-inside-work-tree"])
    return res.returncode == 0 and res.stdout.strip() == "true"


def capture_git_wip_bundle(workspace_root: Path) -> GitWipBundle:
    """Capture current HEAD commit, branch, staged diff, unstaged diff, and untracked code files."""
    if not is_git_repo(workspace_root):
        return GitWipBundle(base_commit="NO_GIT", branch="main", is_dirty=False)

    head_res = _run_git(workspace_root, ["rev-parse", "HEAD"])
    base_commit = head_res.stdout.strip() if head_res.returncode == 0 else "UNBORN"

    branch_res = _run_git(workspace_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "main"

    # Exclude agent instruction files from code WIP diff so agent compilation manages them cleanly
    exclude_args = ["--", "."] + [f":(exclude){p.rstrip('/')}" for p in IGNORED_PREFIXES]

    staged_res = _run_git(workspace_root, ["diff", "--cached"] + exclude_args)
    staged_diff = staged_res.stdout if staged_res.returncode == 0 else ""

    unstaged_res = _run_git(workspace_root, ["diff"] + exclude_args)
    unstaged_diff = unstaged_res.stdout if unstaged_res.returncode == 0 else ""

    untracked_res = _run_git(workspace_root, ["ls-files", "--others", "--exclude-standard"])
    untracked_files: Dict[str, str] = {}
    if untracked_res.returncode == 0:
        for rel_line in untracked_res.stdout.splitlines():
            rel_path = rel_line.strip()
            if not rel_path or any(rel_path.startswith(p) or rel_path == p for p in IGNORED_PREFIXES):
                continue
            full_path = workspace_root / rel_path
            if full_path.is_file():
                try:
                    untracked_files[rel_path] = full_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue

    is_dirty = bool(staged_diff.strip() or unstaged_diff.strip() or untracked_files)
    return GitWipBundle(
        base_commit=base_commit,
        branch=branch,
        staged_diff=staged_diff,
        unstaged_diff=unstaged_diff,
        untracked_files=untracked_files,
        is_dirty=is_dirty,
    )


def restore_git_wip_bundle(workspace_root: Path, bundle: GitWipBundle) -> Dict[str, object]:
    """Restore Git working tree state (patches + untracked WIP files) on target machine."""
    report: Dict[str, object] = {
        "base_commit": bundle.base_commit,
        "staged_applied": False,
        "unstaged_applied": False,
        "untracked_restored_count": 0,
    }
    if bundle.base_commit in ("NO_GIT", "UNBORN"):
        return report

    if is_git_repo(workspace_root):
        # Verify if base_commit exists locally; if so, checkout/reset clean code tree first
        cat_res = _run_git(workspace_root, ["cat-file", "-t", bundle.base_commit])
        if cat_res.returncode == 0 and "commit" in cat_res.stdout:
            _run_git(workspace_root, ["reset", "--hard", bundle.base_commit])

        if bundle.staged_diff.strip():
            apply_staged = _run_git(
                workspace_root,
                ["apply", "--index", "--whitespace=nowarn", "-"],
                input_text=bundle.staged_diff,
            )
            report["staged_applied"] = apply_staged.returncode == 0

        if bundle.unstaged_diff.strip():
            apply_unstaged = _run_git(
                workspace_root,
                ["apply", "--whitespace=nowarn", "-"],
                input_text=bundle.unstaged_diff,
            )
            report["unstaged_applied"] = apply_unstaged.returncode == 0

    for rel_path, content in bundle.untracked_files.items():
        dest = workspace_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        report["untracked_restored_count"] = int(report["untracked_restored_count"]) + 1

    return report
