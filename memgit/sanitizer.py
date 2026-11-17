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

    return transformed
