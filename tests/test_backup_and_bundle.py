"""Tests for Atomic Backup & Undo, Tier-2 Schema Guardrails, and Portable Bundles (.mgit.tar.gz)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest

from memgit.backup import BackupManager
from memgit.bundle import export_checkpoint_bundle, import_checkpoint_bundle
from memgit.converter import CheckpointConverter
from memgit.models import (
    ChatTurn,
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    MemoryScope,
    ProvenanceNode,
)
from memgit.sync.antigravity_sync import sync_antigravity_cli_session_to_app


class TestBackupTier2AndBundle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="memgit_bb_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sample_checkpoint(self, ws: Path, home: Path) -> MemoryCheckpointIR:
        fp = HostFingerprint(os_name="Darwin", arch="arm64", workspace_root=str(ws), user_home=str(home))
        return MemoryCheckpointIR(
            checkpoint_id="chk_bundle001",
            repo_name="sample_repo",
            branch="main",
            parent_checkpoint_id=None,
            author="alice",
            source_agent="claude-desktop",
            source_model="claude-sonnet-4",
            source_host=fp,
            created_at="2026-11-20T00:00:00+00:00",
            message="Portable Bundle Test",
            confirmed_facts=[
                ProvenanceNode(fact_id="f1", entity_key="k1", category="arch", statement="Use pyarrow", scope=MemoryScope.SHARED.value),
                ProvenanceNode(fact_id="f2", entity_key="k2", category="pref", statement="Reply in Chinese", scope=MemoryScope.PRIVATE.value),
            ],
            execution_stack=ExecutionStack(active_goal="Portable Bundle Test"),
            conversation_history=[
                ChatTurn(turn_id="t1", role="user", content="Token sk-ant-api03-BUNDLELEAKTEST1234567890"),
                ChatTurn(turn_id="t2", role="assistant", content="Got it."),
            ],
        )

    def test_convert_backup_and_undo_restores_exact_state(self) -> None:
        home = self.tmp_dir / "home"
        ws = self.tmp_dir / "ws"
        home.mkdir()
        ws.mkdir()

        # Pre-existing GEMINI.md that should be restored verbatim after undo
        gemini_md = ws / "GEMINI.md"
        gemini_md.write_text("# Original User Notes\nKeep me untouched.\n", encoding="utf-8")

        chk = self._sample_checkpoint(ws, home)
        res = CheckpointConverter().convert_and_install(
            checkpoint=chk,
            target_agent="antigravity",
            target_model="gemini-3-pro",
            workspace_root=ws,
            user_home=home,
            verify=True,
            enable_backup=True,
        )
        self.assertIsNotNone(res.backup_id)
        self.assertIn("MEMGIT:BEGIN", gemini_md.read_text(encoding="utf-8"))
        self.assertTrue((ws / ".agent-context" / "HANDOFF.md").exists())

        undo_res = BackupManager(home).undo(res.backup_id)
        self.assertEqual(undo_res["errors"], [])
        self.assertEqual(gemini_md.read_text(encoding="utf-8"), "# Original User Notes\nKeep me untouched.\n")
        self.assertFalse((ws / ".agent-context" / "HANDOFF.md").exists())

    def test_tier2_schema_mismatch_degrades_gracefully(self) -> None:
        home = self.tmp_dir / "home_tier2"
        cli_conv = home / ".gemini" / "antigravity-cli" / "conversations"
        app_dir = home / ".gemini" / "antigravity"
        cli_conv.mkdir(parents=True)
        app_dir.mkdir(parents=True)

        # Create valid CLI trajectory .db
        cid = "22222222-3333-4444-5555-666666666666"
        src_db = cli_conv / f"{cid}.db"
        c = sqlite3.connect(str(src_db))
        c.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB)")
        c.execute("INSERT INTO steps VALUES (0, ?)", (b"hello",))
        c.commit()
        c.close()

        # Create incompatible conversation_summaries.db schema (missing expected columns)
        bad_summary_db = app_dir / "conversation_summaries.db"
        c2 = sqlite3.connect(str(bad_summary_db))
        c2.execute("CREATE TABLE conversation_summaries (id TEXT PRIMARY KEY)")
        c2.commit()
        c2.close()

        warnings = []
        out_cid, synced, steps, _proj = sync_antigravity_cli_session_to_app(
            home_resolved=home,
            before_convs={},
            conversation_title="Schema Drift Test",
            create_project=False,
            warnings_out=warnings,
        )
        self.assertEqual(out_cid, cid)
        self.assertEqual(steps, 1)
        self.assertTrue(any("schema mismatch" in w.lower() for w in warnings))
        self.assertTrue((app_dir / "conversations" / f"{cid}.db").exists())

    def test_export_and_import_bundle_with_security_checks(self) -> None:
        ws = self.tmp_dir / "ws_b"
        home = self.tmp_dir / "home_b"
        ws.mkdir()
        home.mkdir()
        chk = self._sample_checkpoint(ws, home)
        chk_file = self.tmp_dir / f"{chk.checkpoint_id}.json"
        chk_file.write_text(chk.to_json(indent=2), encoding="utf-8")

        bundle_path = self.tmp_dir / "handoff.mgit.tar.gz"
        exp = export_checkpoint_bundle(chk_file, bundle_path, strip_private=True)
        self.assertTrue(bundle_path.exists())
        self.assertGreaterEqual(exp["redacted_secrets_on_export"], 1)

        dest_dir = self.tmp_dir / "imported_checkpoints"
        imp = import_checkpoint_bundle(bundle_path, dest_dir)
        self.assertEqual(imp["checkpoint_id"], chk.checkpoint_id)
        self.assertTrue(imp["checksum_verified"])

        loaded = MemoryCheckpointIR.from_dict(json.loads(Path(imp["installed_path"]).read_text(encoding="utf-8")))
        self.assertEqual(len(loaded.confirmed_facts), 1)  # M_private stripped
        self.assertNotIn("BUNDLELEAKTEST", loaded.conversation_history[0].content)

        # Verify path-traversal archive is rejected
        evil_bundle = self.tmp_dir / "evil.mgit.tar.gz"
        with tarfile.open(evil_bundle, "w:gz") as tar:
            info = tarfile.TarInfo(name="../escape.txt")
            data = b"nope"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValueError):
            import_checkpoint_bundle(evil_bundle, dest_dir)


if __name__ == "__main__":
    unittest.main()
