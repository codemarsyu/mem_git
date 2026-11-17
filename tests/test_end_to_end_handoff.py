"""Automated End-to-End Test Suite for MemGit Checkpoint Creation,
Remote Server Transfer & Model-Specific Transformation, Git WIP Restoration,
Provenance DAG Semantic Merge, and Mock Agent Loading Verification.
"""

from __future__ import annotations

import unittest

from tests.simulate_roaming_demo import run_full_simulation


class TestMemGitEndToEndHandoff(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sim = run_full_simulation(verbose=False)

    def test_01_claude_checkpoint_extraction_and_pitfall_sanitization(self) -> None:
        """Verify Pitfalls 1, 2, 4 and Dual-Stream Git WIP are captured from Mock Claude Code."""
        chk_a = self.sim["chk_a"]
        stats = chk_a["sanitization_stats"]

        # Pitfall 1: Stripped <thinking> block and tool_use schema from session_001.jsonl
        self.assertGreaterEqual(stats["stripped_thinking_blocks"], 1)
        self.assertGreaterEqual(stats["stripped_tool_call_schemas"], 1)

        # Pitfall 2: Normalized hardcoded source machine paths to $WORKSPACE_ROOT
        self.assertGreaterEqual(stats["normalized_host_paths"], 3)
        for fact in chk_a["confirmed_facts"]:
            self.assertNotIn("node_a_macbook_claude", fact["statement"])

        # Pitfall 4: Explicit userDeferrals isolated
        self.assertEqual(len(chk_a["user_deferrals"]), 1)
        self.assertIn("Redis", chk_a["user_deferrals"][0]["feature_or_option"])

        # Dual-Stream Atomic Sync: Dirty Git working tree captured
        self.assertTrue(chk_a["git_wip"]["is_dirty"])
        self.assertIn("divmod(total_cents, count)", chk_a["git_wip"]["unstaged_diff"])
        self.assertIn("src/retry.py", chk_a["git_wip"]["untracked_files"])

    def test_02_remote_server_transfer_and_codex_transformation(self) -> None:
        """Verify Checkpoint A transfers via Mock Remote Server and transforms for OpenAI Codex CLI."""
        pull_b = self.sim["pull_b"]
        boot_b = self.sim["boot_b_codex"]

        self.assertEqual(pull_b["target_agent"], "codex")
        self.assertEqual(pull_b["target_model"], "gpt-5-codex")

        # Verify uncommitted Git WIP patch and untracked files were restored on Node B
        self.assertTrue(pull_b["git_wip_report"]["unstaged_applied"])
        self.assertEqual(pull_b["git_wip_report"]["untracked_restored_count"], 1)
        self.assertIn("divmod(total_cents, count)", self.sim["ws_b_worker_code"])
        self.assertTrue(self.sim["ws_b_retry_exists"])

        # Verify Codex CLI loading simulation
        self.assertTrue(boot_b["within_budget_limits"])
        self.assertEqual(boot_b["foreign_thinking_leaks"], 0)
        self.assertEqual(boot_b["source_path_leaks"], 0)
        self.assertEqual(boot_b["resumed_step_id"], "step_2")
        self.assertGreaterEqual(len(boot_b["user_deferrals_enforced"]), 1)

    def test_03_provenance_dag_semantic_merge_and_privacy_filter(self) -> None:
        """Verify Scenario 2: Semantic Conflict Resolution (workers=1 superseded by workers=8) and M_private stripping."""
        merge_report = self.sim["merge_report"]

        self.assertEqual(merge_report["superseded_facts_count"], 1)
        self.assertGreaterEqual(merge_report["added_facts_count"], 1)
        self.assertGreaterEqual(merge_report["stripped_private_facts_count"], 1)

        resolution = merge_report["resolutions"][0]
        self.assertEqual(resolution["entity_key"], "module_x.concurrency")
        self.assertIn("workers=1", resolution["superseded_statement"])
        self.assertIn("workers=8", resolution["winner_statement"])

    def test_04_antigravity_transformation_and_artifact_loading(self) -> None:
        """Verify merged checkpoint transforms into Google Antigravity Knowledge Items & Artifacts."""
        boot_c = self.sim["boot_c_antigravity"]

        self.assertEqual(boot_c["agent_type"], "antigravity")
        self.assertEqual(boot_c["model_name"], "gemini-4-argon")
        self.assertTrue(boot_c["within_budget_limits"])
        self.assertLessEqual(boot_c["memory_index_lines"], 150)
        self.assertEqual(boot_c["foreign_thinking_leaks"], 0)
        self.assertEqual(boot_c["source_path_leaks"], 0)

        # Verify M_private rule ("Chinese") was stripped during team merge/pull
        self.assertNotIn("Chinese", boot_c["assembled_system_context_preview"])

        # Verify superseded fact is marked superseded and step_3 is the next resumption target
        self.assertGreaterEqual(len(boot_c["superseded_constraints_blocked"]), 1)
        self.assertEqual(boot_c["resumed_step_id"], "step_3")

    def test_05_cursor_and_claude_roundtrip_loading(self) -> None:
        """Verify Cursor SQLite state.vscdb loading and Claude Code 200-line MEMORY.md cap."""
        boot_d = self.sim["boot_d_cursor"]
        self.assertEqual(boot_d["agent_type"], "cursor")
        self.assertEqual(boot_d["source_path_leaks"], 0)
        self.assertEqual(boot_d["resumed_step_id"], "step_3")

        boot_e = self.sim["boot_e_claude"]
        self.assertEqual(boot_e["agent_type"], "claude-code")
        self.assertTrue(boot_e["within_budget_limits"])
        self.assertLessEqual(boot_e["memory_index_lines"], 200)
        self.assertEqual(boot_e["foreign_thinking_leaks"], 0)
        self.assertEqual(boot_e["source_path_leaks"], 0)
        self.assertEqual(boot_e["resumed_step_id"], "step_3")


if __name__ == "__main__":
    unittest.main()
