"""Sanitization, Path Normalization, Thinking Signature Stripping,
User Deferral Isolation, and Privacy Filtering (Pitfalls 1-4 + M_private/M_shared).
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Tuple

from memgit.models import (
    HostFingerprint,
    MemoryCheckpointIR,
    MemoryScope,
    ProvenanceNode,
    SanitizationStats,
    TopicMemoryFile,
    UserDeferralLock,
)

# Regex patterns for Pitfall 1 (Thinking blocks & raw provider signatures)
THINKING_TAG_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)
ANTML_THINKING_RE = re.compile(r"<antml:thinking>.*?</antml:thinking>", re.DOTALL | re.IGNORECASE)
SIGNATURE_FIELD_RE = re.compile(r'"signature"\s*:\s*"[^"]+"')

# Regex patterns for Pitfall 2 (Host path normalization)
GENERIC_USERS_PATH_RE = re.compile(r"/(?:Users|home)/([a-zA-Z0-9_.-]+)")

# Regex patterns for Private scope detection (M_private vs M_shared)
PRIVATE_KEYWORDS_RE = re.compile(
    r"(?:respond in (?:chinese|mandarin|japanese|spanish|french)|"
    r"reply in (?:chinese|mandarin)|"
    r"ssh\s+-i\s+~|alias\s+\w+=|"
    r"scope\s*:\s*private|\[PRIVATE\]|personal preference)",
    re.IGNORECASE,
)

# Regex patterns for Pitfall 4 (User Deferral detection)
DEFERRAL_RE = re.compile(
    r"(?:defer(?:red)?|do not implement|skip option|out of scope|forbid(?:den)? from touching)\s*[:\-]?\s*(.+)",
    re.IGNORECASE,
)

# Regex patterns for Secret Redaction (never persist raw keys/tokens in checkpoints or bundles)
_SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (
        "PEM_PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("ANTHROPIC_API_KEY", re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{16,}\b")),
    ("OPENAI_API_KEY", re.compile(r"\bsk-(?:proj-|svcacct-|live-|test-)?[a-zA-Z0-9_\-]{20,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("GITHUB_PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    (
        "BEARER_TOKEN",
        re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/]{20,}=*)", re.IGNORECASE),
    ),
    (
        "ENV_SECRET_ASSIGNMENT",
        re.compile(
            r"(\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY|PASSWORD|SECRET)\s*=\s*['\"]?)([^\s'\"`,;]{12,})(['\"]?)"
        ),
    ),
]


def redact_secrets(text: str) -> Tuple[str, int]:
    """Detect and mask API keys, tokens, private keys, and secret env assignments."""
    if not text:
        return text, 0
    redacted = text
    total = 0
    for label, pattern in _SECRET_PATTERNS:
        if label == "BEARER_TOKEN":
            def _mask_bearer(m: re.Match) -> str:
                nonlocal total
                val = m.group(2)
                if "[REDACTED" in val:
                    return m.group(0)
                total += 1
                return f"{m.group(1)}[REDACTED_BEARER_TOKEN]"

            redacted = pattern.sub(_mask_bearer, redacted)
        elif label == "ENV_SECRET_ASSIGNMENT":
            def _mask_env(m: re.Match) -> str:
                nonlocal total
                val = m.group(2)
                if val.startswith("$") or "[REDACTED" in val or val.upper() in ("YOUR_API_KEY", "YOUR_KEY_HERE", "CHANGEME"):
                    return m.group(0)
                total += 1
                return f"{m.group(1)}[REDACTED_SECRET]{m.group(3)}"

            redacted = pattern.sub(_mask_env, redacted)
        else:
            def _mask_direct(m: re.Match, lbl: str = label) -> str:
                nonlocal total
                total += 1
                return f"[REDACTED_{lbl}]"

            redacted = pattern.sub(_mask_direct, redacted)
    return redacted, total


def redact_checkpoint_secrets(checkpoint: MemoryCheckpointIR) -> int:
    """In-place redact secrets across all text fields of a MemoryCheckpointIR and update stats."""
    total = 0

    def _scrub(value: str) -> str:
        nonlocal total
        cleaned, cnt = redact_secrets(value)
        total += cnt
        return cleaned

    checkpoint.message = _scrub(checkpoint.message)
    for fact in checkpoint.confirmed_facts:
        fact.statement = _scrub(fact.statement)
    for neg in checkpoint.negative_memories:
        neg.hypothesis = _scrub(neg.hypothesis)
        neg.failure_reason = _scrub(neg.failure_reason)
    for deferral in checkpoint.user_deferrals:
        deferral.feature_or_option = _scrub(deferral.feature_or_option)
        deferral.directive = _scrub(deferral.directive)

    checkpoint.execution_stack.active_goal = _scrub(checkpoint.execution_stack.active_goal)
    checkpoint.execution_stack.current_hypothesis = _scrub(checkpoint.execution_stack.current_hypothesis)
    checkpoint.execution_stack.stack_trace_digest = _scrub(checkpoint.execution_stack.stack_trace_digest)
    for step in checkpoint.execution_stack.steps:
        step.title = _scrub(step.title)
        step.notes = _scrub(step.notes)
        if step.verification_cmd:
            step.verification_cmd = _scrub(step.verification_cmd)

    for topic in checkpoint.topic_memories:
        topic.title = _scrub(topic.title)
        topic.summary = _scrub(topic.summary)
        topic.content = _scrub(topic.content)

    for turn in checkpoint.conversation_history:
        turn.content = _scrub(turn.content)
        if turn.tool_summary:
            turn.tool_summary = _scrub(turn.tool_summary)

    if checkpoint.git_wip:
        checkpoint.git_wip.staged_diff = _scrub(checkpoint.git_wip.staged_diff)
        checkpoint.git_wip.unstaged_diff = _scrub(checkpoint.git_wip.unstaged_diff)
        for rel_p, body in list(checkpoint.git_wip.untracked_files.items()):
            checkpoint.git_wip.untracked_files[rel_p] = _scrub(body)

    checkpoint.sanitization_stats.redacted_secrets += total
    return total


def strip_thinking_blocks(text: str) -> Tuple[str, int]:
    """Remove <thinking> blocks and cryptographic thought signatures from text."""
    count = len(THINKING_TAG_RE.findall(text)) + len(ANTML_THINKING_RE.findall(text))
    cleaned = THINKING_TAG_RE.sub("", text)
    cleaned = ANTML_THINKING_RE.sub("", cleaned)
    sig_matches = len(SIGNATURE_FIELD_RE.findall(cleaned))
    if sig_matches:
        cleaned = SIGNATURE_FIELD_RE.sub('"signature": "[STRIPPED_BY_MEMGIT]"', cleaned)
        count += sig_matches
    return cleaned.strip(), count


def normalize_paths_to_ir(
    text: str,
    source_host: HostFingerprint,
) -> Tuple[str, int]:
    """Replace hardcoded host paths with portable $WORKSPACE_ROOT and $USER_HOME."""
    if not text:
        return text, 0

    replacements = 0
    result = text

    ws_root = source_host.workspace_root.rstrip("/")
    user_home = source_host.user_home.rstrip("/")

    if ws_root and ws_root in result:
        occurrences = result.count(ws_root)
        result = result.replace(ws_root, "$WORKSPACE_ROOT")
        replacements += occurrences

    if user_home and user_home in result:
        occurrences = result.count(user_home)
        result = result.replace(user_home, "$USER_HOME")
        replacements += occurrences

    # Also catch any remaining /Users/<name> or /home/<name> paths from transcripts
    def _replace_generic_home(match: re.Match) -> str:
        nonlocal replacements
        replacements += 1
        return "$USER_HOME"

    result = GENERIC_USERS_PATH_RE.sub(_replace_generic_home, result)

    # Normalize macOS Homebrew Python path to portable python3
    if "/opt/homebrew/bin/python3" in result:
        replacements += result.count("/opt/homebrew/bin/python3")
        result = result.replace("/opt/homebrew/bin/python3", "python3")

    return result, replacements


def rehydrate_paths_for_target(
    text: str,
    target_host: HostFingerprint,
) -> str:
    """Replace $WORKSPACE_ROOT and $USER_HOME with the target machine's actual paths."""
    if not text:
        return text
    ws_root = target_host.workspace_root.rstrip("/")
    user_home = target_host.user_home.rstrip("/")
    result = text.replace("$WORKSPACE_ROOT", ws_root)
    result = result.replace("$USER_HOME", user_home)
    return result


def classify_memory_scope(statement: str, explicit_scope: str = "") -> str:
    """Classify a memory statement into M_private vs M_shared."""
    if explicit_scope in (MemoryScope.PRIVATE.value, MemoryScope.SHARED.value):
        return explicit_scope
    if PRIVATE_KEYWORDS_RE.search(statement):
        return MemoryScope.PRIVATE.value
    return MemoryScope.SHARED.value


def filter_checkpoint_for_target(
    checkpoint: MemoryCheckpointIR,
    target_host: HostFingerprint,
    include_private: bool = True,
) -> MemoryCheckpointIR:
    """Return a deep-copied checkpoint transformed for the target host and privacy policy."""
    transformed = copy.deepcopy(checkpoint)

    # 1. Privacy Sandboxing Filter (M_private stripped when include_private=False)
    if not include_private:
        transformed.confirmed_facts = [
            f for f in transformed.confirmed_facts if f.scope != MemoryScope.PRIVATE.value
        ]
        transformed.topic_memories = [
            t for t in transformed.topic_memories if t.scope != MemoryScope.PRIVATE.value
        ]

    # 2. Active facts only (keep superseded facts in DAG metadata, or mark clearly)
    # 3. Re-hydrate portable paths ($WORKSPACE_ROOT, $USER_HOME) for target host
    for fact in transformed.confirmed_facts:
        fact.statement = rehydrate_paths_for_target(fact.statement, target_host)

    for neg in transformed.negative_memories:
        neg.hypothesis = rehydrate_paths_for_target(neg.hypothesis, target_host)
        neg.failure_reason = rehydrate_paths_for_target(neg.failure_reason, target_host)

    for deferral in transformed.user_deferrals:
        deferral.directive = rehydrate_paths_for_target(deferral.directive, target_host)

    transformed.execution_stack.active_goal = rehydrate_paths_for_target(
        transformed.execution_stack.active_goal, target_host
    )
    transformed.execution_stack.current_hypothesis = rehydrate_paths_for_target(
        transformed.execution_stack.current_hypothesis, target_host
    )
    transformed.execution_stack.stack_trace_digest = rehydrate_paths_for_target(
        transformed.execution_stack.stack_trace_digest, target_host
    )
    for step in transformed.execution_stack.steps:
        step.title = rehydrate_paths_for_target(step.title, target_host)
        step.notes = rehydrate_paths_for_target(step.notes, target_host)
        if step.verification_cmd:
            step.verification_cmd = rehydrate_paths_for_target(step.verification_cmd, target_host)

    for topic in transformed.topic_memories:
        topic.summary = rehydrate_paths_for_target(topic.summary, target_host)
        topic.content = rehydrate_paths_for_target(topic.content, target_host)

    for turn in transformed.conversation_history:
        turn.content = rehydrate_paths_for_target(turn.content, target_host)
        if turn.tool_summary:
            turn.tool_summary = rehydrate_paths_for_target(turn.tool_summary, target_host)

    redact_checkpoint_secrets(transformed)
    return transformed
