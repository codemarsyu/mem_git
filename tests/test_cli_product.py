"""End-to-end tests for MemGit v1.0 CLI commands (`doctor`, `ls`, `handoff`, `resume`, `show`, `rm`, `undo`, `export`, `import`, `completion`)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from memgit import __version__
from memgit.cli import main as cli_main
from memgit.paths import claude_desktop_indexeddb_dir
from tests.test_claude_desktop import _snappy_compress_literals, _v8_str, _wrap_leveldb_wal_full


class TestMemGitProductCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="memgit_cli_test_"))
        self.home = self.tmp_dir / "home"
        self.ws = self.tmp_dir / "workspace"
        self.home.mkdir()
        self.ws.mkdir()

        # Seed a realistic Claude Desktop IndexedDB conversation inside isolated test home
        idb_dir = claude_desktop_indexeddb_dir(self.home)
        idb_dir.mkdir(parents=True, exist_ok=True)
        self.cid = "aaaa1111-2222-3333-4444-bbbb55556666"
        blob = b"".join(
            [
                _v8_str("uuid"),
                _v8_str(self.cid),
                _v8_str("name"),
                _v8_str("Parquet Grid Demo"),
                _v8_str("updated_at"),
                _v8_str("2026-11-20T01:00:00Z"),
                _v8_str("sender"),
                _v8_str("human"),
                _v8_str("text"),
                _v8_str("Help me build a Parquet viewer in Python."),
                _v8_str("sender"),
                _v8_str("assistant"),
                _v8_str("text"),
                _v8_str("Use pandas + pyarrow with Gradio DataFrame."),
            ]
        )
        (idb_dir / "000001.log").write_bytes(
            _wrap_leveldb_wal_full(self.cid.encode() + b"\xff\x11\x02" + _snappy_compress_literals(blob))
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_cli(self, argv: list) -> tuple:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli_main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_doctor_ls_handoff_export_import_resume_undo_rm(self) -> None:
        # 1. doctor --json
        rc, out, err = self._run_cli(["doctor", "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["memgit_version"], __version__)
        cd_agent = next(a for a in doc["agents"] if a["agent_id"] == "claude-desktop")
        self.assertTrue(cd_agent["installed"])

        # 2. ls --agent claude-desktop --json
        rc, out, err = self._run_cli(["ls", "--agent", "claude-desktop", "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        sessions = json.loads(out)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["title"], "Parquet Grid Demo")

        # 3. handoff --dry-run (writes nothing)
        rc, out, err = self._run_cli(
            [
                "handoff",
                "Parquet Grid",
                "--from",
                "claude-desktop",
                "--to",
                "antigravity",
                "--workspace",
                str(self.ws),
                "--home",
                str(self.home),
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual(rc, 0, err)
        dry = json.loads(out)
        self.assertTrue(dry["conversion"]["dry_run"])
        self.assertFalse((self.ws / "GEMINI.md").exists())

        # 4. Real 1-step handoff claude-desktop -> antigravity
        rc, out, err = self._run_cli(
            [
                "handoff",
                "Parquet Grid",
                "--from",
                "claude-desktop",
                "--to",
                "antigravity",
                "--workspace",
                str(self.ws),
                "--home",
                str(self.home),
                "--json",
            ]
        )
        self.assertEqual(rc, 0, err)
        ho = json.loads(out)
        chk_id = ho["checkpoint_id"]
        backup_id = ho["conversion"]["backup_id"]
        self.assertTrue((self.ws / "GEMINI.md").exists())
        self.assertIsNotNone(backup_id)

        # 5. ls --checkpoints & show
        rc, out, err = self._run_cli(["ls", "--checkpoints", "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        cks = json.loads(out)
        self.assertTrue(any(c["checkpoint_id"] == chk_id for c in cks))

        rc, out, err = self._run_cli(["show", chk_id, "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["message"], "Parquet Grid Demo")

        # 6. export -> import on a second "Linux work node" home
        bundle_file = self.tmp_dir / "transfer.mgit.tar.gz"
        rc, out, err = self._run_cli(["export", chk_id, "-o", str(bundle_file), "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(bundle_file.exists())

        linux_home = self.tmp_dir / "linux_home"
        linux_ws = self.tmp_dir / "linux_ws"
        linux_home.mkdir()
        linux_ws.mkdir()
        rc, out, err = self._run_cli(["import", str(bundle_file), "--home", str(linux_home), "--json"])
        self.assertEqual(rc, 0, err)

        # 7. resume imported checkpoint by ID on the Linux node into codex
        rc, out, err = self._run_cli(
            [
                "resume",
                chk_id,
                "--to",
                "codex",
                "--workspace",
                str(linux_ws),
                "--home",
                str(linux_home),
                "--target-os",
                "Linux",
                "--json",
            ]
        )
        self.assertEqual(rc, 0, err)
        self.assertTrue((linux_ws / "AGENTS.md").exists())

        # 8. undo reverts the handoff files cleanly
        rc, out, err = self._run_cli(["undo", backup_id, "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.ws / "GEMINI.md").exists())

        # 9. rm deletes checkpoint cleanly
        rc, out, err = self._run_cli(["rm", chk_id, "--home", str(self.home), "--json"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads(out)["deleted"])

        # 10. completion scripts emit cleanly
        for sh in ("zsh", "bash", "fish"):
            rc, out, err = self._run_cli(["completion", sh])
            self.assertEqual(rc, 0, err)
            self.assertIn("memgit", out)


if __name__ == "__main__":
    unittest.main()
