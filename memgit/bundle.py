"""Serverless Cross-Machine Portable Bundles (`.mgit.tar.gz`) for MacBook <-> Linux Work Node handoffs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
from typing import Any, Dict

from memgit import __version__
from memgit.models import MemoryCheckpointIR, MemoryScope
from memgit.sanitizer import redact_checkpoint_secrets

BUNDLE_FORMAT_VERSION = 1


def export_checkpoint_bundle(
    checkpoint_path: Path,
    output_path: Path,
    strip_private: bool = False,
) -> Dict[str, Any]:
    """Package a checkpoint JSON into a verified, secret-redacted `.mgit.tar.gz` archive."""
    raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    chk = MemoryCheckpointIR.from_dict(raw)

    if strip_private:
        chk.confirmed_facts = [f for f in chk.confirmed_facts if f.scope != MemoryScope.PRIVATE.value]
        chk.topic_memories = [t for t in chk.topic_memories if t.scope != MemoryScope.PRIVATE.value]

    redacted_now = redact_checkpoint_secrets(chk)
    chk_bytes = chk.to_json(indent=2).encode("utf-8")
    sha256_hex = hashlib.sha256(chk_bytes).hexdigest()

    manifest = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "memgit_version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_from_os": platform.system(),
        "exported_from_arch": platform.machine(),
        "checkpoint_id": chk.checkpoint_id,
        "repo_name": chk.repo_name,
        "message": chk.message,
        "source_agent": chk.source_agent,
        "source_model": chk.source_model,
        "conversation_turns": len(chk.conversation_history),
        "strip_private": strip_private,
        "redacted_secrets_on_export": redacted_now,
        "checkpoint_sha256": sha256_hex,
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

    out = output_path.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f".{out.name}.tmp")
    with tarfile.open(tmp_out, "w:gz") as tar:
        for name, payload in (("manifest.json", manifest_bytes), ("checkpoint.json", chk_bytes)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mtime = int(datetime.now(timezone.utc).timestamp())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    tmp_out.replace(out)

    return {
        "bundle_path": str(out),
        "bundle_bytes": out.stat().st_size,
        **manifest,
    }


def import_checkpoint_bundle(
    bundle_path: Path,
    dest_dir: Path,
) -> Dict[str, Any]:
    """Verify SHA256 integrity, guard against path traversal, and install a `.mgit.tar.gz` bundle."""
    bpath = bundle_path.resolve()
    if not bpath.exists():
        raise FileNotFoundError(f"Bundle not found: {bpath}")

    manifest_bytes: bytes = b""
    chk_bytes: bytes = b""
    with tarfile.open(bpath, "r:gz") as tar:
        for member in tar.getmembers():
            # Reject symlinks, absolute paths, and parent traversals
            if member.issym() or member.islnk():
                raise ValueError(f"Refusing unsafe link entry in bundle: {member.name}")
            norm = Path(member.name)
            if norm.is_absolute() or ".." in norm.parts:
                raise ValueError(f"Refusing unsafe path traversal entry in bundle: {member.name}")
            if member.name == "manifest.json" and member.isfile():
                fobj = tar.extractfile(member)
                if fobj is not None:
                    manifest_bytes = fobj.read()
            elif member.name == "checkpoint.json" and member.isfile():
                fobj = tar.extractfile(member)
                if fobj is not None:
                    chk_bytes = fobj.read()

    if not manifest_bytes or not chk_bytes:
        raise ValueError("Invalid MemGit bundle: missing manifest.json or checkpoint.json")

    manifest = json.loads(manifest_bytes.decode("utf-8"))
    expected_sha = manifest.get("checkpoint_sha256", "")
    actual_sha = hashlib.sha256(chk_bytes).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        raise ValueError(
            f"Bundle checksum mismatch! Expected {expected_sha}, got {actual_sha}."
        )

    chk = MemoryCheckpointIR.from_dict(json.loads(chk_bytes.decode("utf-8")))
    redact_checkpoint_secrets(chk)

    dest = dest_dir.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    out_file = dest / f"{chk.checkpoint_id}.json"
    tmp_file = dest / f".{chk.checkpoint_id}.json.tmp"
    tmp_file.write_text(chk.to_json(indent=2), encoding="utf-8")
    tmp_file.replace(out_file)

    return {
        "checkpoint_id": chk.checkpoint_id,
        "installed_path": str(out_file),
        "repo_name": chk.repo_name,
        "message": chk.message,
        "source_agent": chk.source_agent,
        "conversation_turns": len(chk.conversation_history),
        "checksum_verified": bool(expected_sha),
        "sha256": actual_sha,
    }
