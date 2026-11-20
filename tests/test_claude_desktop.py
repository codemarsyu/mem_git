"""Tests for ClaudeDesktopAdapter (LevelDB WAL + Snappy + V8 Structured Clone + macOS/Linux paths)."""

from __future__ import annotations

from pathlib import Path
import shutil
import struct
import tempfile
import unittest

from memgit.adapters.claude_desktop import (
    ClaudeDesktopAdapter,
    parse_claude_v8_conversation,
    parse_leveldb_wal_records,
    snappy_decompress,
)
from memgit.models import HostFingerprint
from memgit.paths import claude_desktop_indexeddb_dir


def _v8_str(s: str) -> bytes:
    raw = s.encode("latin1")
    ln = len(raw)
    varint = bytearray()
    while True:
        b = ln & 0x7F
        ln >>= 7
        if ln:
            varint.append(b | 0x80)
        else:
            varint.append(b)
            break
    return b"\x22" + bytes(varint) + raw


def _snappy_compress_literals(data: bytes) -> bytes:
    """Minimal valid Snappy encoder using literal chunks only (for deterministic test fixtures)."""
    out = bytearray()
    # Uncompressed length varint
    n = len(data)
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    # Emit in <=50-byte literal chunks
    pos = 0
    while pos < len(data):
        chunk = data[pos : pos + 50]
        pos += len(chunk)
        tag = ((len(chunk) - 1) << 2) | 0x00
        out.append(tag)
        out.extend(chunk)
    return bytes(out)


def _wrap_leveldb_wal_full(payload: bytes) -> bytes:
    return struct.pack("<IHB", 0, len(payload), 1) + payload


class TestClaudeDesktopAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="memgit_cd_test_"))
        self.adapter = ClaudeDesktopAdapter()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _build_fixture_blob(self, conv_uuid: str, title: str) -> bytes:
        return b"".join(
            [
                _v8_str("uuid"),
                _v8_str(conv_uuid),
                _v8_str("name"),
                _v8_str(title),
                _v8_str("updated_at"),
                _v8_str("2026-11-19T12:00:00Z"),
                _v8_str("sender"),
                _v8_str("human"),
                _v8_str("created_at"),
                _v8_str("2026-11-19T11:55:00Z"),
                _v8_str("text"),
                _v8_str("Build a Gradio Parquet viewer using key sk-ant-api03-TESTSECRETKEY1234567890"),
                _v8_str("sender"),
                _v8_str("assistant"),
                _v8_str("created_at"),
                _v8_str("2026-11-19T11:56:00Z"),
                _v8_str("text"),
                _v8_str("Here is a clean Gradio DataFrame app with pagination and dropdown label editing."),
            ]
        )

    def test_wal_snappy_v8_roundtrip(self) -> None:
        cid = "01323916-a919-4362-8565-b686fad8889b"
        v8_blob = self._build_fixture_blob(cid, "Gradio Parquet DataFrame Viewer")
        compressed = _snappy_compress_literals(v8_blob)
        decompressed = snappy_decompress(compressed)
        self.assertEqual(decompressed, v8_blob)

        wal = _wrap_leveldb_wal_full(b"prefix_key" + cid.encode() + b"\xff\x11\x02" + compressed)
        recs = parse_leveldb_wal_records(wal)
        self.assertEqual(len(recs), 1)

        parsed = parse_claude_v8_conversation(decompressed)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["uuid"], cid)
        self.assertEqual(parsed["title"], "Gradio Parquet DataFrame Viewer")
        self.assertEqual(len(parsed["turns"]), 2)

    def test_list_and_extract_on_macos_and_linux_paths(self) -> None:
        for os_name in ("Darwin", "Linux"):
            home = self.tmp_dir / f"home_{os_name.lower()}"
            ws = self.tmp_dir / f"ws_{os_name.lower()}"
            ws.mkdir(parents=True, exist_ok=True)
            idb_dir = claude_desktop_indexeddb_dir(home, os_name=os_name)
            idb_dir.mkdir(parents=True, exist_ok=True)

            cid = "11112222-3333-4444-5555-666677778888"
            v8_blob = self._build_fixture_blob(cid, "Cross-OS Parquet Tool")
            compressed = _snappy_compress_literals(v8_blob)
            (idb_dir / "000001.log").write_bytes(
                _wrap_leveldb_wal_full(cid.encode() + b"\xff\x11\x02" + compressed)
            )

            sessions = self.adapter.list_sessions(home, os_name=os_name)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].session_id, cid)
            self.assertEqual(sessions[0].title, "Cross-OS Parquet Tool")
            self.assertEqual(sessions[0].turn_count, 2)

            fp = HostFingerprint(os_name=os_name, arch="arm64", workspace_root=str(ws), user_home=str(home))
            chk = self.adapter.extract_checkpoint(
                workspace_root=ws,
                user_home=home,
                repo_name="parquet_tool",
                author="tester",
                source_model="claude-sonnet-4",
                host_fingerprint=fp,
                title_query="Parquet Tool",
                os_name=os_name,
            )
            self.assertEqual(chk.message, "Cross-OS Parquet Tool")
            self.assertEqual(len(chk.conversation_history), 2)
            # Secret must be redacted automatically during extraction
            self.assertGreaterEqual(chk.sanitization_stats.redacted_secrets, 1)
            self.assertNotIn("TESTSECRETKEY1234567890", chk.conversation_history[0].content)


if __name__ == "__main__":
    unittest.main()
