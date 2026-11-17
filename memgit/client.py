"""MemGit Client SDK for communicating with the Remote MemGit HTTP Server
and orchestrating local extraction, restoration, and agent loading verification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from memgit.adapters import AgentBootSimulationReport, get_adapter
from memgit.compiler import CompiledMemoryPackage
from memgit.git_wip import restore_git_wip_bundle
from memgit.models import GitWipBundle, HostFingerprint, MemoryCheckpointIR
from memgit.server.app import get_active_local_server


class MemGitClient:
    """HTTP Client speaking to a remote (or local mock) MemGit Server."""

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.server_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {err_body}") from exc
        except (urllib.error.URLError, PermissionError, OSError):
            local_srv = get_active_local_server(self.server_url)
            if local_srv is not None:
                return local_srv.dispatch_direct(method, path, payload)
            raise

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/health")

    def push_checkpoint(self, checkpoint: MemoryCheckpointIR) -> Dict[str, Any]:
        path = f"/api/v1/repos/{urllib.parse.quote(checkpoint.repo_name)}/push"
        return self._request("POST", path, {"checkpoint": checkpoint.to_dict()})

    def pull_and_restore(
        self,
        repo_name: str,
        ref: str,
        target_agent: str,
        target_model: str,
        target_host: HostFingerprint,
        include_private: bool = True,
        restore_code_wip: bool = True,
    ) -> Dict[str, Any]:
        """Request server-side checkpoint transformation for the destination node's model/agent,
        restore uncommitted Git WIP edits, and materialize target agent memory files.
        """
        path = f"/api/v1/repos/{urllib.parse.quote(repo_name)}/pull"
        resp = self._request(
            "POST",
            path,
            {
                "ref": ref,
                "target_agent": target_agent,
                "target_model": target_model,
                "target_host": target_host.to_dict(),
                "include_private": include_private,
            },
        )
        compiled_pkg = CompiledMemoryPackage.from_dict(resp["compiled_package"])
        git_wip = GitWipBundle.from_dict(resp["git_wip"])

        ws_path = Path(target_host.workspace_root)
        home_path = Path(target_host.user_home)
        ws_path.mkdir(parents=True, exist_ok=True)
        home_path.mkdir(parents=True, exist_ok=True)

        wip_report: Dict[str, object] = {}
        if restore_code_wip:
            wip_report = restore_git_wip_bundle(ws_path, git_wip)

        adapter = get_adapter(target_agent)
        written_files = adapter.install_compiled_package(
            compiled_pkg, workspace_root=ws_path, user_home=home_path
        )
        return {
            "checkpoint_id": resp["checkpoint_id"],
            "source_agent": resp["source_agent"],
            "source_model": resp["source_model"],
            "target_agent": target_agent,
            "target_model": target_model,
            "git_wip_report": wip_report,
            "written_files": written_files,
            "compilation_notes": compiled_pkg.compilation_notes,
        }

    def merge_remote(
        self,
        repo_name: str,
        base_ref: str,
        incoming_ref: str,
        author: str = "team-merger",
        target_branch: str = "main",
        strip_private: bool = True,
    ) -> Dict[str, Any]:
        path = f"/api/v1/repos/{urllib.parse.quote(repo_name)}/merge"
        return self._request(
            "POST",
            path,
            {
                "base_ref": base_ref,
                "incoming_ref": incoming_ref,
                "author": author,
                "target_branch": target_branch,
                "strip_private": strip_private,
            },
        )

    def log(self, repo_name: str) -> List[Dict[str, Any]]:
        path = f"/api/v1/repos/{urllib.parse.quote(repo_name)}/log"
        resp = self._request("GET", path)
        return resp.get("checkpoints", [])

    def blame(self, repo_name: str, ref: str = "main") -> List[Dict[str, Any]]:
        path = f"/api/v1/repos/{urllib.parse.quote(repo_name)}/blame?ref={urllib.parse.quote(ref)}"
        resp = self._request("GET", path)
        return resp.get("blame", [])
