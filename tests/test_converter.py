"""Unit tests for CheckpointConverter (Claude Code & OpenAI Codex CLI format conversion)."""

from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

from memgit.adapters import get_adapter
from memgit.converter import CheckpointConverter, encode_claude_project_key
from memgit.models import (
    ChatTurn,
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    ProvenanceNode,
    TaskLockStatus,
    TaskStep,
    TopicMemoryFile,
    utc_now_iso,
)

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

    def _make_sample_checkpoint(self) -> MemoryCheckpointIR:
        now = utc_now_iso()
        chk = MemoryCheckpointIR(
            checkpoint_id="chk_test_dual_cli_001",
            parent_checkpoint_id=None,
            repo_name="video_uploader_api",
            branch="main",
            created_at=now,
            author="developer",
            source_agent="claude-code",
            source_model="claude-4-opus",
            source_host=HostFingerprint(
                os_name="Darwin",
                arch="arm64",
                workspace_root="/Users/oldhost/projects/video_uploader_api",
                user_home="/Users/oldhost",
            ),
            message="Debug 422 error on video upload endpoint",
            confirmed_facts=[
                ProvenanceNode(
                    fact_id="fact_422_multipart",
                    entity_key="api.upload_endpoint",
                    category="bug_diagnosis",
                    statement="FastAPI UploadFile field name must be 'file' (not 'video') or endpoint returns HTTP 422.",
                    author="developer",
                    source_agent="claude-code",
                    source_model="claude-4-opus",
                    timestamp=now,
                )
            ],
            execution_stack=ExecutionStack(
                active_goal="Fix HTTP 422 on video upload to backend",
                current_hypothesis="Client sends multipart field 'video' while FastAPI expects 'file'.",
                steps=[
                    TaskStep(
                        step_id="step_fix_upload_field",
                        title="Update frontend FormData key to 'file' and add regression test",
                        status=TaskLockStatus.NOT_DONE.value,
                        relative_files=["frontend/upload.ts", "backend/routes.py"],
                    )
                ],
            ),
            topic_memories=[
                TopicMemoryFile(
                    topic_slug="upload_422_diagnosis",
                    title="Video Upload 422 Diagnosis",
                    summary="Root cause of 422 Unprocessable Entity on multipart upload",
                    content="FastAPI validates multipart field name strictly as 'file'.",
                    tags=["fastapi", "422"],
                )
            ],
            conversation_history=[
                ChatTurn(
                    turn_id="turn_001",
                    role="user",
                    content="Video upload to backend returning 422 error. Can you help debug?",
                    timestamp=now,
                ),
                ChatTurn(
                    turn_id="turn_002",
                    role="assistant",
                    content="Found it: backend/routes.py expects multipart field 'file', but frontend sends 'video'.",
                    timestamp=now,
                ),
            ],
        )
        return chk

    def _write_mock_cli(self, name: str, script_body: str) -> Path:
        cli_path = self.tmp_dir / f"mock_{name}"
        cli_path.write_text(f"#!{sys.executable}\n{script_body}", encoding="utf-8")
        cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC)
        return cli_path

    def test_resume_scenario_1_antigravity_via_agy_cli(self) -> None:
        """Scenario 1: Destination = Antigravity App, bootstrapped automatically via `agy` CLI."""
        chk = self._make_sample_checkpoint()
        ws_agy = self.tmp_dir / "ws_antigravity" / "video_uploader_api"
        home_agy = self.tmp_dir / "home_antigravity"
        ws_agy.mkdir(parents=True)
        home_agy.mkdir(parents=True)

        # Mock `agy` CLI verifies workspace + Antigravity brain chat_history.md and creates a WAL SQLite conversation DB
        mock_agy = self._write_mock_cli(
            "agy",
            """
import json, os, sqlite3, sys
from pathlib import Path

assert "--new-project" not in sys.argv, f"Default standalone resume should not pass --new-project: {sys.argv}"
assert "--project" in sys.argv and sys.argv[sys.argv.index("--project") + 1] == "outside-of-project", f"Expected --project outside-of-project: {sys.argv}"
assert "-p" in sys.argv, f"Expected -p flag, got {sys.argv}"
prompt = sys.argv[sys.argv.index("-p") + 1]
assert not prompt.startswith("MemGit Checkpoint Resumption"), f"Unexpected MemGit prefix in prompt: {prompt[:80]}"
assert prompt.startswith("Debug 422 error on video upload endpoint"), f"Expected original title at start of prompt: {prompt[:80]}"
cwd = Path.cwd()
home = Path(os.environ["HOME"])
chk_id = os.environ["MEMGIT_CHECKPOINT_ID"]
assert os.environ["MEMGIT_TARGET_AGENT"] == "antigravity"

chat_md = (cwd / ".agent-context" / "CHAT_HISTORY.md").read_text(encoding="utf-8")
brain_chat = (home / ".gemini" / "antigravity" / "brain" / chk_id / "chat_history.md").read_text(encoding="utf-8")
assert "Video upload to backend returning 422 error" in chat_md
assert "Video upload to backend returning 422 error" in brain_chat

# Simulate agy CLI storing its WAL-mode trajectory DB in ~/.gemini/antigravity-cli/conversations/
conv_id = "11111111-2222-3333-4444-555555555555"
cli_conv_dir = home / ".gemini" / "antigravity-cli" / "conversations"
cli_conv_dir.mkdir(parents=True, exist_ok=True)
db_path = cli_conv_dir / f"{conv_id}.db"
conn = sqlite3.connect(str(db_path))
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
conn.execute("INSERT INTO steps VALUES (0, 14, ?)", (b"user_turn_restored",))
conn.execute("INSERT INTO steps VALUES (1, 17, ?)", (b"assistant_restored_history",))
conn.commit()
conn.close()

cli_brain = home / ".gemini" / "antigravity-cli" / "brain" / conv_id
cli_brain.mkdir(parents=True, exist_ok=True)
(cli_brain / "transcript.jsonl").write_text('{"role":"assistant","content":"Restored"}\\n', encoding="utf-8")
print(f"AGY_SESSION_READY:{chk_id}")
""",
        )

        res = self.converter.convert_and_install(
            checkpoint=chk,
            target_agent="antigravity",
            target_model="gemini-3-pro",
            workspace_root=ws_agy,
            user_home=home_agy,
            verify=True,
            bootstrap_session=True,
            bootstrap_bin=str(mock_agy),
        )

        # Verify Antigravity-specific artifacts (and NOT Gemini CLI global ~/.gemini/GEMINI.md)
        self.assertTrue((ws_agy / "GEMINI.md").exists())
        self.assertTrue((ws_agy / ".agent-context" / "CHAT_HISTORY.md").exists())
        self.assertTrue((home_agy / ".gemini" / "antigravity" / "brain" / chk.checkpoint_id / "chat_history.md").exists())
        self.assertTrue((home_agy / ".gemini" / "antigravity" / "knowledge" / "upload_422_diagnosis" / "metadata.json").exists())
        self.assertFalse((home_agy / ".gemini" / "GEMINI.md").exists())

        # Verify `agy` CLI bootstrap succeeded AND synced WAL-merged .db (2 steps) + brain/ to ~/.gemini/antigravity/
        boot_cli = res.bootstrap_session
        self.assertIsNotNone(boot_cli)
        assert boot_cli is not None
        self.assertEqual(boot_cli.agent, "antigravity")
        self.assertTrue(boot_cli.executed)
        self.assertTrue(boot_cli.succeeded)
        self.assertEqual(boot_cli.conversation_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(boot_cli.conversation_url, "conversation://11111111-2222-3333-4444-555555555555")
        self.assertEqual(boot_cli.project_id, "outside-of-project")
        self.assertEqual(boot_cli.synced_steps, 2)
        self.assertIn(f"AGY_SESSION_READY:{chk.checkpoint_id}", boot_cli.stdout_preview)
        synced_db = home_agy / ".gemini" / "antigravity" / "conversations" / "11111111-2222-3333-4444-555555555555.db"
        self.assertTrue(synced_db.exists())
        self.assertTrue((home_agy / ".gemini" / "antigravity" / "brain" / "11111111-2222-3333-4444-555555555555" / "transcript.jsonl").exists())
        ann_file = home_agy / ".gemini" / "antigravity" / "annotations" / "11111111-2222-3333-4444-555555555555.pbtxt"
        self.assertTrue(ann_file.exists())
        self.assertIn('title:"Debug 422 error on video upload endpoint"', ann_file.read_text(encoding="utf-8"))

        # Also test empty 0-step .db + .pb fallback sync (does NOT copy empty .db, copies .pb instead)
        cli_conv_dir = home_agy / ".gemini" / "antigravity-cli" / "conversations"
        fb_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        import sqlite3
        fb_db = cli_conv_dir / f"{fb_id}.db"
        conn = sqlite3.connect(str(fb_db))
        conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        (cli_conv_dir / f"{fb_id}.pb").write_bytes(b"mock_pb_payload_bytes")
        cid, synced, steps, _proj = self.converter._sync_antigravity_cli_session_to_app(
            home_resolved=home_agy,
            before_convs={"11111111-2222-3333-4444-555555555555.db": (cli_conv_dir / "11111111-2222-3333-4444-555555555555.db").stat().st_mtime},
            conversation_title="Debug 422 error on video upload endpoint",
        )
        self.assertEqual(cid, fb_id)
        self.assertEqual(steps, 0)
        self.assertFalse((home_agy / ".gemini" / "antigravity" / "conversations" / f"{fb_id}.db").exists())
        self.assertTrue((home_agy / ".gemini" / "antigravity" / "conversations" / f"{fb_id}.pb").exists())

        # Verify boot simulation report
        self.assertIsNotNone(res.boot_verification)
        assert res.boot_verification is not None
        self.assertTrue(res.boot_verification.within_budget_limits)
        self.assertEqual(res.boot_verification.source_path_leaks, 0)
        self.assertEqual(res.boot_verification.resumed_step_id, "step_fix_upload_field")

    def test_resume_scenario_2_gemini_app_via_gemini_cli(self) -> None:
        """Scenario 2: Destination = Gemini App / CLI, bootstrapped automatically via `gemini` CLI."""
        chk = self._make_sample_checkpoint()
        ws_gem = self.tmp_dir / "ws_gemini" / "video_uploader_api"
        home_gem = self.tmp_dir / "home_gemini"
        ws_gem.mkdir(parents=True)
        home_gem.mkdir(parents=True)

        # Mock `gemini` CLI verifies workspace + ~/.gemini/memories/chat_history.md and creates a native Gemini session
        mock_gemini = self._write_mock_cli(
            "gemini",
            """
import json, os, sys
from pathlib import Path

assert "--new-project" not in sys.argv, f"Unexpected --new-project for gemini CLI: {sys.argv}"
assert "-p" in sys.argv, f"Expected -p flag, got {sys.argv}"
prompt = sys.argv[sys.argv.index("-p") + 1]
cwd = Path.cwd()
home = Path(os.environ["HOME"])
chk_id = os.environ["MEMGIT_CHECKPOINT_ID"]
assert os.environ["MEMGIT_TARGET_AGENT"] == "gemini-cli"

chat_md = (cwd / ".agent-context" / "CHAT_HISTORY.md").read_text(encoding="utf-8")
global_chat = (home / ".gemini" / "memories" / "chat_history.md").read_text(encoding="utf-8")
assert "Video upload to backend returning 422 error" in chat_md
assert "Video upload to backend returning 422 error" in global_chat

chats_dir = home / ".gemini" / "tmp" / "proj_hash" / "chats"
chats_dir.mkdir(parents=True, exist_ok=True)
(chats_dir / f"session-{chk_id}.json").write_text(json.dumps({
    "sessionId": chk_id,
    "messages": [
        {"type": "user", "content": "Video upload to backend returning 422 error. Can you help debug?"},
        {"type": "gemini", "content": "Restored checkpoint and ready to update frontend FormData key to 'file'."},
    ],
}), encoding="utf-8")
print(f"GEMINI_CLI_SESSION_READY:{chk_id}")
""",
        )

        res = self.converter.convert_and_install(
            checkpoint=chk,
            target_agent="gemini-cli",
            target_model="gemini-3-pro",
            workspace_root=ws_gem,
            user_home=home_gem,
            verify=True,
            bootstrap_session=True,
            bootstrap_bin=str(mock_gemini),
        )

        # Verify Gemini CLI-specific files (and NOT Antigravity brain/knowledge directories)
        self.assertTrue((ws_gem / "GEMINI.md").exists())
        self.assertTrue((ws_gem / ".agent-context" / "CHAT_HISTORY.md").exists())
        self.assertTrue((home_gem / ".gemini" / "GEMINI.md").exists())
        self.assertTrue((home_gem / ".gemini" / "memories" / "chat_history.md").exists())
        self.assertFalse((home_gem / ".gemini" / "antigravity" / "brain").exists())

        # Verify `gemini` CLI bootstrap succeeded and persisted native session JSON
        boot_cli = res.bootstrap_session
        self.assertIsNotNone(boot_cli)
        assert boot_cli is not None
        self.assertEqual(boot_cli.agent, "gemini-cli")
        self.assertTrue(boot_cli.executed)
        self.assertTrue(boot_cli.succeeded)
        self.assertIn(f"GEMINI_CLI_SESSION_READY:{chk.checkpoint_id}", boot_cli.stdout_preview)

        session_json = home_gem / ".gemini" / "tmp" / "proj_hash" / "chats" / f"session-{chk.checkpoint_id}.json"
        self.assertTrue(session_json.exists())

        # Verify GeminiCLIAdapter can also round-trip extract the bootstrapped session
        gemini_adapter = get_adapter("gemini-cli")
        roundtrip_chk = gemini_adapter.extract_checkpoint(
            workspace_root=ws_gem,
            user_home=home_gem,
            repo_name="video_uploader_api",
            author="developer",
            source_model="gemini-3-pro",
            host_fingerprint=HostFingerprint(
                os_name="Darwin",
                arch="arm64",
                workspace_root=str(ws_gem.resolve()),
                user_home=str(home_gem.resolve()),
            ),
        )
        self.assertEqual(len(roundtrip_chk.conversation_history), 2)
        self.assertIn("422 error", roundtrip_chk.conversation_history[0].content)

        # Verify boot simulation report
        self.assertIsNotNone(res.boot_verification)
        assert res.boot_verification is not None
        self.assertTrue(res.boot_verification.within_budget_limits)
        self.assertEqual(res.boot_verification.source_path_leaks, 0)
        self.assertEqual(res.boot_verification.resumed_step_id, "step_fix_upload_field")

    def test_bootstrap_graceful_fallback_when_cli_missing(self) -> None:
        """If `agy` or `gemini` is not installed on PATH, files still install and report a clear note."""
        chk = self._make_sample_checkpoint()
        ws = self.tmp_dir / "ws_missing_cli"
        home = self.tmp_dir / "home_missing_cli"
        ws.mkdir(parents=True)
        home.mkdir(parents=True)

        with mock.patch("memgit.converter.shutil.which", return_value=None):
            res_agy = self.converter.convert_and_install(
                checkpoint=chk,
                target_agent="antigravity",
                target_model="gemini-3-pro",
                workspace_root=ws,
                user_home=home,
                verify=True,
                bootstrap_session=True,
            )
            res_gem = self.converter.convert_and_install(
                checkpoint=chk,
                target_agent="gemini-cli",
                target_model="gemini-3-pro",
                workspace_root=ws,
                user_home=home,
                verify=True,
                bootstrap_session=True,
            )

        self.assertIsNotNone(res_agy.bootstrap_session)
        assert res_agy.bootstrap_session is not None
        self.assertEqual(res_agy.bootstrap_session.cli_binary, "agy")
        self.assertFalse(res_agy.bootstrap_session.executed)
        self.assertFalse(res_agy.bootstrap_session.succeeded)
        self.assertIn("not found on PATH", res_agy.bootstrap_session.note)

        self.assertIsNotNone(res_gem.bootstrap_session)
        assert res_gem.bootstrap_session is not None
        self.assertEqual(res_gem.bootstrap_session.cli_binary, "gemini")
        self.assertFalse(res_gem.bootstrap_session.executed)
        self.assertFalse(res_gem.bootstrap_session.succeeded)
        self.assertIn("not found on PATH", res_gem.bootstrap_session.note)

    def test_antigravity_standalone_protobuf_patching(self) -> None:
        """Verify protobuf wire-format rewriter strips workspace/CLI fields and sets outside-of-project."""
        enc_b = CheckpointConverter._pb_encode_bytes_field
        enc_v = CheckpointConverter._pb_encode_varint_field

        # Build a mock trajectory_metadata_blob with f1 (workspace), f6 (cid), f7 (uri), f18 (default-cli-project)
        meta_blob = (
            enc_b(1, b"workspace_info_payload")
            + enc_b(6, b"4dccdb40-4f0e-4e59-824d-c49f73626638")
            + enc_b(7, b"file:///tmp/workspace")
            + enc_b(18, b"default-cli-project")
        )
        patched_meta = CheckpointConverter._pb_patch_trajectory_metadata_standalone(meta_blob)
        meta_fields = {fn: payload for fn, _wt, _raw, payload in CheckpointConverter._pb_iter_fields(patched_meta)}
        self.assertNotIn(1, meta_fields)
        self.assertNotIn(7, meta_fields)
        self.assertEqual(meta_fields[6], b"4dccdb40-4f0e-4e59-824d-c49f73626638")
        self.assertEqual(meta_fields[18], b"outside-of-project")

        # Build a mock raw_summary with f9 (workspace), f17 (meta_blob), f20 (CLI source=17)
        raw_summary = (
            enc_v(2, 7)
            + enc_b(9, b"workspace_block")
            + enc_b(17, meta_blob)
            + enc_v(20, 17)
        )
        patched_summary = CheckpointConverter._pb_patch_raw_summary_standalone(
            raw_summary, title="Gradio Parquet DataFrame Viewer"
        )
        summary_fields = {fn: payload for fn, _wt, _raw, payload in CheckpointConverter._pb_iter_fields(patched_summary)}
        self.assertNotIn(9, summary_fields)
        self.assertNotIn(20, summary_fields)
        self.assertEqual(summary_fields[1], b"Gradio Parquet DataFrame Viewer")
        self.assertIn(15, summary_fields)
        f15_fields = {fn: payload for fn, _wt, _raw, payload in CheckpointConverter._pb_iter_fields(summary_fields[15])}
        self.assertEqual(f15_fields[1], b"Gradio Parquet DataFrame Viewer")
        f17_fields = {fn: payload for fn, _wt, _raw, payload in CheckpointConverter._pb_iter_fields(summary_fields[17])}
        self.assertEqual(f17_fields[18], b"outside-of-project")
        self.assertNotIn(1, f17_fields)
        self.assertNotIn(7, f17_fields)

        # Verify agyhub_summaries_proto.pb upsert replaces f1 entry and strips f2 CLI marker
        proto_path = self.tmp_dir / "agyhub_summaries_proto.pb"
        other_entry = enc_b(1, enc_b(1, b"other-cid") + enc_b(2, b"other_raw"))
        cli_entry = enc_b(1, enc_b(1, b"target-cid") + enc_b(2, raw_summary))
        cli_marker = enc_b(2, enc_b(1, b"target-cid") + enc_v(2, 6))
        proto_path.write_bytes(other_entry + cli_entry + cli_marker)

        ok = CheckpointConverter._sync_agyhub_summaries_proto(
            proto_path,
            conv_id="target-cid",
            raw_summary=patched_summary,
            strip_cli_marker=True,
        )
        self.assertTrue(ok)
        updated_fields = CheckpointConverter._pb_iter_fields(proto_path.read_bytes())
        f1_entries = {}
        f2_cids = []
        for fn, wt, _raw, payload in updated_fields:
            sub = {sfn: sp for sfn, _swt, _sr, sp in CheckpointConverter._pb_iter_fields(payload)}
            if fn == 1:
                f1_entries[sub[1]] = sub[2]
            elif fn == 2:
                f2_cids.append(sub[1])
        self.assertEqual(f1_entries[b"other-cid"], b"other_raw")
        self.assertEqual(f1_entries[b"target-cid"], patched_summary)
        self.assertNotIn(b"target-cid", f2_cids)


if __name__ == "__main__":
    unittest.main()
