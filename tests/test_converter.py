"""Unit tests for CheckpointConverter (Claude Code & OpenAI Codex CLI format conversion)."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from memgit.converter import CheckpointConverter, encode_claude_project_key
from memgit.models import HostFingerprint

CHECKPOINT_3D = Path(__file__).resolve().parent.parent / ".memgit" / "checkpoints" / "chk_ee994e7fab13.json"


class TestCheckpointConverter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="memgit_conv_test_"))
        self.converter = CheckpointConverter()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_claude_project_key_encoding(self) -> None:
        key = encode_claude_project_key(Path("/Users/ruoyuli/Documents/blender_scene/3d_scene"))
        self.assertEqual(key, "-Users-ruoyuli-Documents-blender-scene-3d-scene")

    @unittest.skipUnless(CHECKPOINT_3D.exists(), "Real 3d_scene checkpoint not present")
    def test_convert_3d_scene_to_claude_and_codex(self) -> None:
        chk = self.converter.load_checkpoint_file(CHECKPOINT_3D)

        # 1. Convert for Claude Code on a target macOS directory
        ws_claude = self.tmp_dir / "ws_claude" / "3d_scene"
        home_claude = self.tmp_dir / "home_claude"
        ws_claude.mkdir(parents=True)
        home_claude.mkdir(parents=True)
        # Pre-seed an existing CLAUDE.md to verify non-destructive delimited upsert
        (ws_claude / "CLAUDE.md").write_text("# User Custom Rules\n- Keep my custom rule!\n", encoding="utf-8")

        res_claude = self.converter.convert_and_install(
            checkpoint=chk,
            target_agent="claude-code",
            target_model="claude-4-opus",
            workspace_root=ws_claude,
            user_home=home_claude,
            verify=True,
        )
        claude_md_text = (ws_claude / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Keep my custom rule!", claude_md_text)
        self.assertIn("<!-- MEMGIT:BEGIN -->", claude_md_text)

        mem_dir = home_claude / ".claude" / "projects" / res_claude.encoded_project_key / "memory"
        self.assertTrue((mem_dir / "MEMORY.md").exists())
        self.assertTrue((mem_dir / "project_architecture.md").exists())
        self.assertTrue((mem_dir / "feedback_guardrails.md").exists())
        self.assertTrue((mem_dir / "project_execution_stack.md").exists())
        self.assertIn("type: feedback", (mem_dir / "feedback_guardrails.md").read_text(encoding="utf-8"))

        # Verify Zero-Setup App Registration (.claude.json, history.jsonl, and resumable session .jsonl)
        import json
        self.assertIsNotNone(res_claude.session_id)
        claude_cfg = json.loads((home_claude / ".claude.json").read_text(encoding="utf-8"))
        proj_cfg = claude_cfg["projects"][str(ws_claude.resolve())]
        self.assertTrue(proj_cfg["hasTrustDialogAccepted"])
        self.assertEqual(proj_cfg["lastSessionId"], res_claude.session_id)

        sess_file = home_claude / ".claude" / "projects" / res_claude.encoded_project_key / f"{res_claude.session_id}.jsonl"
        self.assertTrue(sess_file.exists())
        sess_records = [json.loads(line) for line in sess_file.read_text(encoding="utf-8").splitlines()]
        self.assertGreaterEqual(len(sess_records), 5)
        self.assertEqual(sess_records[0]["type"], "user")
        self.assertIsNone(sess_records[0]["parentUuid"])
        # Verify unbroken parentUuid chain across all user & assistant turns
        for idx in range(1, len(sess_records) - 1):
            self.assertIn(sess_records[idx]["type"], ("user", "assistant"))
            self.assertEqual(sess_records[idx]["parentUuid"], sess_records[idx - 1]["uuid"])
            self.assertEqual(sess_records[idx]["session_id"], res_claude.session_id)
        self.assertEqual(sess_records[-1]["type"], "last-prompt")
        self.assertEqual(sess_records[-1]["leafUuid"], sess_records[-2]["uuid"])

        # Verify portable CHAT_HISTORY.md was also materialized
        self.assertTrue((ws_claude / ".agent-context" / "CHAT_HISTORY.md").exists())
        self.assertIn("can you access blender", (ws_claude / ".agent-context" / "CHAT_HISTORY.md").read_text(encoding="utf-8"))

        hist_text = (home_claude / ".claude" / "history.jsonl").read_text(encoding="utf-8")
        self.assertIn(res_claude.session_id, hist_text)

        boot_c = res_claude.boot_verification
        self.assertIsNotNone(boot_c)
        assert boot_c is not None
        self.assertTrue(boot_c.within_budget_limits)
        self.assertEqual(boot_c.foreign_thinking_leaks, 0)
        self.assertEqual(boot_c.source_path_leaks, 0)
        self.assertEqual(boot_c.resumed_step_id, "step_next_resume")
        self.assertGreaterEqual(len(boot_c.loaded_files), 6)

        # 2. Convert for OpenAI Codex CLI on a target Linux GPU host
        ws_codex = self.tmp_dir / "ws_codex" / "3d_scene"
        home_codex = self.tmp_dir / "home_codex"
        ws_codex.mkdir(parents=True)
        home_codex.mkdir(parents=True)
        linux_fp = HostFingerprint(
            os_name="Linux",
            arch="x86_64",
            workspace_root=str(ws_codex.resolve()),
            user_home=str(home_codex.resolve()),
            shell="bash",
            cuda_available=True,
            gpu_device="NVIDIA RTX 4090",
        )
        res_codex = self.converter.convert_and_install(
            checkpoint=chk,
            target_agent="codex",
            target_model="gpt-5-codex",
            workspace_root=ws_codex,
            user_home=home_codex,
            target_host=linux_fp,
            verify=True,
        )
        agents_text = (ws_codex / "AGENTS.md").read_text(encoding="utf-8")
        # macOS Blender.app path should be adapted to Linux `blender`
        self.assertNotIn("/Applications/Blender.app", (home_codex / ".codex" / "memories" / "memory_summary.md").read_text(encoding="utf-8"))
        self.assertTrue((home_codex / ".codex" / "AGENTS.md").exists())
        self.assertTrue((home_codex / ".codex" / "memories" / "memory_summary.md").exists())

        boot_x = res_codex.boot_verification
        self.assertIsNotNone(boot_x)
        assert boot_x is not None
        self.assertTrue(boot_x.within_budget_limits)
        self.assertEqual(boot_x.foreign_thinking_leaks, 0)
        self.assertEqual(boot_x.source_path_leaks, 0)
        self.assertEqual(boot_x.resumed_step_id, "step_next_resume")


if __name__ == "__main__":
    unittest.main()
