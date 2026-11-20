"""Tests for mandatory secret redaction in memgit.sanitizer."""

from __future__ import annotations

import unittest

from memgit.models import (
    ChatTurn,
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    ProvenanceNode,
)
from memgit.sanitizer import (
    filter_checkpoint_for_target,
    redact_checkpoint_secrets,
    redact_secrets,
)


class TestSecretRedaction(unittest.TestCase):
    def test_redact_common_api_keys_and_tokens(self) -> None:
        sample = (
            "Anthropic: sk-ant-api03-ABCDEF1234567890abcdef1234567890\n"
            "OpenAI: sk-proj-9876543210abcdefghijklmnop\n"
            "Google: AIzaSyD1234567890abcdefghijklmnopqrs\n"
            "GitHub: ghp_1234567890abcdefghij1234567890abcd\n"
            "AWS: AKIAIOSFODNN7EXAMPLE\n"
            "Auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload\n"
            "Env: MY_SERVICE_SECRET=supersecretvalue9999\n"
            "SafePlaceholder: MY_API_KEY=$MY_API_KEY\n"
        )
        cleaned, count = redact_secrets(sample)
        self.assertGreaterEqual(count, 7)
        self.assertNotIn("sk-ant-api03", cleaned)
        self.assertNotIn("sk-proj-987654", cleaned)
        self.assertNotIn("AIzaSyD123", cleaned)
        self.assertNotIn("ghp_123456", cleaned)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", cleaned)
        self.assertNotIn("eyJhbGciOiJIUzI1Ni", cleaned)
        self.assertNotIn("supersecretvalue9999", cleaned)
        self.assertIn("MY_API_KEY=$MY_API_KEY", cleaned)

    def test_redact_pem_private_key(self) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAtest123\n-----END RSA PRIVATE KEY-----"
        cleaned, count = redact_secrets(pem)
        self.assertEqual(count, 1)
        self.assertEqual(cleaned, "[REDACTED_PEM_PRIVATE_KEY]")

    def test_redact_checkpoint_and_filter_target(self) -> None:
        fp = HostFingerprint(os_name="Darwin", arch="arm64", workspace_root="/tmp/ws", user_home="/tmp/home")
        chk = MemoryCheckpointIR(
            checkpoint_id="chk_sec001",
            repo_name="demo",
            branch="main",
            parent_checkpoint_id=None,
            author="dev",
            source_agent="claude-desktop",
            source_model="claude-sonnet-4",
            source_host=fp,
            created_at="2026-11-20T00:00:00+00:00",
            message="Share key sk-ant-api03-SECRETSECRETSECRET12345",
            confirmed_facts=[
                ProvenanceNode(
                    fact_id="f1",
                    entity_key="auth",
                    category="config",
                    statement="Use ANTHROPIC_API_KEY=sk-ant-api03-SECRETSECRETSECRET12345",
                )
            ],
            execution_stack=ExecutionStack(active_goal="Test"),
            conversation_history=[
                ChatTurn(turn_id="t1", role="user", content="My token is ghp_1234567890abcdefghij1234567890abcd"),
            ],
        )
        n = redact_checkpoint_secrets(chk)
        self.assertGreaterEqual(n, 3)
        self.assertEqual(chk.sanitization_stats.redacted_secrets, n)
        self.assertNotIn("SECRETSECRETSECRET", chk.message)
        self.assertNotIn("ghp_123456", chk.conversation_history[0].content)

        # Verify filter_checkpoint_for_target also redacts defensively
        chk.conversation_history.append(
            ChatTurn(turn_id="t2", role="assistant", content="AWS key AKIAIOSFODNN7EXAMPLE")
        )
        filtered = filter_checkpoint_for_target(chk, fp)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", filtered.conversation_history[-1].content)


if __name__ == "__main__":
    unittest.main()
