"""Mock Remote MemGit HTTP Server (REST API for Push, Server-Side Transform/Pull, Merge, and Blame)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from memgit.models import HostFingerprint, MemoryCheckpointIR
from memgit.server.storage import ServerStorage


def route_memgit_http_request(
    storage: ServerStorage,
    method: str,
    raw_path: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Core REST router shared by ThreadingHTTPServer and sandbox loopback transport."""
    parsed = urlparse(raw_path)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    query = parse_qs(parsed.query)
    body = body or {}

    if method == "GET":
        if parts == ["api", "v1", "health"]:
            return 200, {"status": "ok", "service": "memgit-remote-server"}

        if len(parts) == 5 and parts[:3] == ["api", "v1", "repos"] and parts[4] == "log":
            repo_name = parts[3]
            commits = storage.list_checkpoints(repo_name)
            return 200, {"repo_name": repo_name, "checkpoints": commits}

        if len(parts) == 5 and parts[:3] == ["api", "v1", "repos"] and parts[4] == "blame":
            repo_name = parts[3]
            ref = query.get("ref", ["main"])[0]
            try:
                records = storage.blame(repo_name, ref=ref)
                return 200, {"repo_name": repo_name, "ref": ref, "blame": records}
            except KeyError as exc:
                return 404, {"error": str(exc)}

        return 404, {"error": f"Unknown GET endpoint: {parsed.path}"}

    if method == "POST":
        if len(parts) == 5 and parts[:3] == ["api", "v1", "repos"] and parts[4] == "push":
            repo_name = parts[3]
            chk_data = body.get("checkpoint", body)
            chk = MemoryCheckpointIR.from_dict(chk_data)
            chk.repo_name = repo_name
            meta = storage.save_checkpoint(chk)
            return 200, meta

        if len(parts) == 5 and parts[:3] == ["api", "v1", "repos"] and parts[4] == "pull":
            repo_name = parts[3]
            ref = body.get("ref", "main")
            target_agent = body.get("target_agent", "antigravity")
            target_model = body.get("target_model", "gemini-4-argon")
            include_private = bool(body.get("include_private", True))
            target_host = HostFingerprint.from_dict(body["target_host"])

            try:
                chk, compiled_pkg = storage.transform_for_destination(
                    repo_name=repo_name,
                    ref=ref,
                    target_agent=target_agent,
                    target_model=target_model,
                    target_host=target_host,
                    include_private=include_private,
                )
                return 200, {
                    "status": "transformed",
                    "checkpoint_id": chk.checkpoint_id,
                    "source_agent": chk.source_agent,
                    "source_model": chk.source_model,
                    "target_agent": target_agent,
                    "target_model": target_model,
                    "git_wip": chk.git_wip.to_dict(),
                    "compiled_package": compiled_pkg.to_dict(),
                }
            except KeyError as exc:
                return 404, {"error": str(exc)}

        if len(parts) == 5 and parts[:3] == ["api", "v1", "repos"] and parts[4] == "merge":
            repo_name = parts[3]
            base_ref = body["base_ref"]
            incoming_ref = body["incoming_ref"]
            author = body.get("author", "team-merger")
            target_branch = body.get("target_branch", "main")
            strip_private = bool(body.get("strip_private", True))
            try:
                merged_chk, report = storage.merge_refs(
                    repo_name=repo_name,
                    base_ref=base_ref,
                    incoming_ref=incoming_ref,
                    author=author,
                    target_branch=target_branch,
                    strip_private=strip_private,
                )
                return 200, {
                    "status": "merged",
                    "merged_checkpoint": merged_chk.to_dict(),
                    "merge_report": report.to_dict(),
                }
            except KeyError as exc:
                return 404, {"error": str(exc)}

        return 404, {"error": f"Unknown POST endpoint: {parsed.path}"}

    return 405, {"error": f"Method {method} not allowed"}


class MemGitRequestHandler(BaseHTTPRequestHandler):
    storage: ServerStorage

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def do_GET(self) -> None:
        code, resp = route_memgit_http_request(self.storage, "GET", self.path)
        self._send_json(code, resp)

    def do_POST(self) -> None:
        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"Invalid JSON request body: {exc}"})
            return
        code, resp = route_memgit_http_request(self.storage, "POST", self.path, body)
        self._send_json(code, resp)


_ACTIVE_LOCAL_SERVERS: Dict[str, "MockRemoteMemGitServer"] = {}


def get_active_local_server(url: str) -> Optional["MockRemoteMemGitServer"]:
    return _ACTIVE_LOCAL_SERVERS.get(url.rstrip("/"))


class MockRemoteMemGitServer:
    """Lifecycle wrapper for running the MemGit remote HTTP server in a background thread or foreground."""

    def __init__(self, storage_dir: Path, host: str = "127.0.0.1", port: int = 0) -> None:
        self.storage = ServerStorage(storage_dir)
        self.host = host
        self.httpd: Optional[ThreadingHTTPServer] = None
        try:
            handler_cls = type(
                "BoundMemGitRequestHandler",
                (MemGitRequestHandler,),
                {"storage": self.storage},
            )
            self.httpd = ThreadingHTTPServer((host, port), handler_cls)
            self.port = self.httpd.server_address[1]
        except OSError:
            self.port = port or 8765
        self._thread: Optional[threading.Thread] = None
        _ACTIVE_LOCAL_SERVERS[self.base_url] = self

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def dispatch_direct(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        code, resp = route_memgit_http_request(self.storage, method, path, payload)
        if code >= 400:
            raise RuntimeError(f"HTTP {code} from {self.base_url}{path}: {json.dumps(resp)}")
        return resp

    def start_background(self) -> str:
        if self.httpd is not None:
            self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self._thread.start()
        return self.base_url

    def serve_forever(self) -> None:
        if self.httpd is not None:
            self.httpd.serve_forever()

    def stop(self) -> None:
        _ACTIVE_LOCAL_SERVERS.pop(self.base_url, None)
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
