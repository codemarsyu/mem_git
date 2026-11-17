"""Semantic Merger, Provenance DAG Conflict Resolver, Memory Pull Request Generator,
and Cognitive Blame Audit Trail.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from memgit.models import (
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    MemoryScope,
    NegativeMemoryItem,
    ProvenanceNode,
    TaskLockStatus,
    TaskStep,
    TopicMemoryFile,
    UserDeferralLock,
    VerificationStatus,
    utc_now_iso,
)


@dataclass
class SemanticMergeReport:
    """Human-readable Memory Pull Request & Semantic Resolution summary."""
    merged_checkpoint_id: str
    base_checkpoint_id: str
    incoming_checkpoint_id: str
    added_facts_count: int = 0
    superseded_facts_count: int = 0
    preserved_facts_count: int = 0
    stripped_private_facts_count: int = 0
    deferred_tasks_count: int = 0
    resolutions: List[Dict[str, str]] = field(default_factory=list)
    summary_headline: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "merged_checkpoint_id": self.merged_checkpoint_id,
            "base_checkpoint_id": self.base_checkpoint_id,
            "incoming_checkpoint_id": self.incoming_checkpoint_id,
            "added_facts_count": self.added_facts_count,
            "superseded_facts_count": self.superseded_facts_count,
            "preserved_facts_count": self.preserved_facts_count,
            "stripped_private_facts_count": self.stripped_private_facts_count,
            "deferred_tasks_count": self.deferred_tasks_count,
            "resolutions": self.resolutions,
            "summary_headline": self.summary_headline,
        }


def _verification_rank(status: str) -> int:
    ranks = {
        VerificationStatus.SUPERSEDED.value: 0,
        VerificationStatus.VERIFIED_FAIL.value: 1,
        VerificationStatus.UNVERIFIED.value: 2,
        VerificationStatus.VERIFIED_PASS.value: 3,
    }
    return ranks.get(status, 1)


def semantic_merge_checkpoints(
    base_chk: MemoryCheckpointIR,
    incoming_chk: MemoryCheckpointIR,
    author: str = "team-merger",
    target_branch: str = "main",
    strip_private: bool = True,
) -> Tuple[MemoryCheckpointIR, SemanticMergeReport]:
    """Perform Provenance DAG semantic merge between base_chk (Memory A) and incoming_chk (Memory B).

    Resolves entity_key collisions using causal lineage (parent_hash), verification_status,
    and timestamp instead of naive text concatenation.
    """
    report = SemanticMergeReport(
        merged_checkpoint_id="",
        base_checkpoint_id=base_chk.checkpoint_id,
        incoming_checkpoint_id=incoming_chk.checkpoint_id,
    )

    merged_facts: List[ProvenanceNode] = []
    by_entity: Dict[str, ProvenanceNode] = {}

    # 1. Index base facts (filtering private if requested)
    for f in copy.deepcopy(base_chk.confirmed_facts):
        if strip_private and f.scope == MemoryScope.PRIVATE.value:
            report.stripped_private_facts_count += 1
            continue
        by_entity[f.entity_key] = f
        merged_facts.append(f)

    # 2. Merge incoming facts by entity_key with Provenance DAG resolution
    for inc in copy.deepcopy(incoming_chk.confirmed_facts):
        if strip_private and inc.scope == MemoryScope.PRIVATE.value:
            report.stripped_private_facts_count += 1
            continue

        if inc.entity_key not in by_entity:
            by_entity[inc.entity_key] = inc
            merged_facts.append(inc)
            report.added_facts_count += 1
            continue

        existing = by_entity[inc.entity_key]
        if existing.statement.strip() == inc.statement.strip():
            # Identical statement: upgrade verification status if incoming is stronger
            if _verification_rank(inc.verification_status) > _verification_rank(existing.verification_status):
                existing.verification_status = inc.verification_status
            report.preserved_facts_count += 1
            continue

        # Semantic Conflict on the same entity_key (e.g. "module_x.concurrency")!
        # Check causal parent, verification status, and timestamp
        inc_wins = False
        reason = ""
        if inc.parent_hash in (existing.fact_id, base_chk.checkpoint_id):
            inc_wins = True
            reason = f"Causal child of {inc.parent_hash}"
        elif _verification_rank(inc.verification_status) > _verification_rank(existing.verification_status):
            inc_wins = True
            reason = f"Higher verification status ({inc.verification_status} > {existing.verification_status})"
        elif inc.timestamp >= existing.timestamp:
            inc_wins = True
            reason = f"Newer timestamp ({inc.timestamp} >= {existing.timestamp})"

        if inc_wins:
            existing.verification_status = VerificationStatus.SUPERSEDED.value
            existing.superseded_by = inc.fact_id
            by_entity[inc.entity_key] = inc
            merged_facts.append(inc)
            report.superseded_facts_count += 1
            report.resolutions.append({
                "entity_key": inc.entity_key,
                "superseded_fact_id": existing.fact_id,
                "superseded_statement": existing.statement,
                "superseded_author_model": f"{existing.author} ({existing.source_model})",
                "winner_fact_id": inc.fact_id,
                "winner_statement": inc.statement,
                "winner_author_model": f"{inc.author} ({inc.source_model})",
                "reason": reason,
            })
        else:
            inc.verification_status = VerificationStatus.SUPERSEDED.value
            inc.superseded_by = existing.fact_id
            merged_facts.append(inc)
            report.superseded_facts_count += 1
            report.resolutions.append({
                "entity_key": inc.entity_key,
                "superseded_fact_id": inc.fact_id,
                "superseded_statement": inc.statement,
                "superseded_author_model": f"{inc.author} ({inc.source_model})",
                "winner_fact_id": existing.fact_id,
                "winner_statement": existing.statement,
                "winner_author_model": f"{existing.author} ({existing.source_model})",
                "reason": "Base fact had higher verification status / newer timestamp",
            })

    # 3. Merge User Deferrals (Pitfall 4: union of all active deferral locks)
    deferral_map: Dict[str, UserDeferralLock] = {
        d.feature_or_option: copy.deepcopy(d) for d in base_chk.user_deferrals
    }
    for d in incoming_chk.user_deferrals:
        deferral_map[d.feature_or_option] = copy.deepcopy(d)
    merged_deferrals = list(deferral_map.values())
    report.deferred_tasks_count = len(merged_deferrals)

    # 4. Merge Negative Memories (union by hypothesis)
    neg_map: Dict[str, NegativeMemoryItem] = {
        n.hypothesis: copy.deepcopy(n) for n in base_chk.negative_memories
    }
    for n in incoming_chk.negative_memories:
        neg_map[n.hypothesis] = copy.deepcopy(n)
    merged_negatives = list(neg_map.values())

    # 5. Merge Execution Stack (status progression: DONE > PARTIALLY_DONE > NOT_DONE)
    status_rank = {
        TaskLockStatus.NOT_DONE.value: 0,
        TaskLockStatus.PARTIALLY_DONE.value: 1,
        TaskLockStatus.DONE.value: 2,
    }
    step_map: Dict[str, TaskStep] = {
        s.step_id: copy.deepcopy(s) for s in base_chk.execution_stack.steps
    }
    for s in incoming_chk.execution_stack.steps:
        if s.step_id not in step_map:
            step_map[s.step_id] = copy.deepcopy(s)
        else:
            cur = step_map[s.step_id]
            if status_rank.get(s.status, 0) >= status_rank.get(cur.status, 0):
                step_map[s.step_id] = copy.deepcopy(s)

    merged_stack = ExecutionStack(
        active_goal=incoming_chk.execution_stack.active_goal or base_chk.execution_stack.active_goal,
        current_hypothesis=incoming_chk.execution_stack.current_hypothesis,
        modified_files=sorted(
            set(base_chk.execution_stack.modified_files) | set(incoming_chk.execution_stack.modified_files)
        ),
        passing_test_commands=incoming_chk.execution_stack.passing_test_commands
        or base_chk.execution_stack.passing_test_commands,
        failing_test_commands=incoming_chk.execution_stack.failing_test_commands,
        stack_trace_digest=incoming_chk.execution_stack.stack_trace_digest,
        steps=list(step_map.values()),
    )

    # 6. Merge Topic Memories (excluding private topics if strip_private=True)
    topic_map: Dict[str, TopicMemoryFile] = {}
    for t in base_chk.topic_memories + incoming_chk.topic_memories:
        if strip_private and t.scope == MemoryScope.PRIVATE.value:
            continue
        topic_map[t.topic_slug] = copy.deepcopy(t)

    now = utc_now_iso()
    msg = f"Semantic merge {incoming_chk.checkpoint_id} into {base_chk.checkpoint_id}"
    merged_id = MemoryCheckpointIR.generate_id(
        base_chk.repo_name, author, msg, now, incoming_chk.checkpoint_id
    )
    report.merged_checkpoint_id = merged_id
    report.summary_headline = (
        f"+{report.added_facts_count} architectural facts, "
        f"{report.superseded_facts_count} superseded assumption(s), "
        f"{report.deferred_tasks_count} deferred task lock(s), "
        f"{report.stripped_private_facts_count} private directive(s) sanitized"
    )

    merged_chk = MemoryCheckpointIR(
        checkpoint_id=merged_id,
        repo_name=base_chk.repo_name,
        branch=target_branch,
        parent_checkpoint_id=incoming_chk.checkpoint_id,
        author=author,
        source_agent=incoming_chk.source_agent,
        source_model=f"merged({base_chk.source_model}+{incoming_chk.source_model})",
        source_host=incoming_chk.source_host,
        created_at=now,
        message=msg,
        confirmed_facts=merged_facts,
        negative_memories=merged_negatives,
        user_deferrals=merged_deferrals,
        execution_stack=merged_stack,
        topic_memories=list(topic_map.values()),
        git_wip=incoming_chk.git_wip,
    )
    return merged_chk, report


def generate_cognitive_blame(checkpoint: MemoryCheckpointIR) -> List[Dict[str, Any]]:
    """Produce a Git-blame style cognitive audit trail for all facts and deferrals."""
    records: List[Dict[str, Any]] = []
    for fact in checkpoint.confirmed_facts:
        records.append({
            "type": "fact",
            "id": fact.fact_id,
            "entity_key": fact.entity_key,
            "status": fact.verification_status,
            "superseded_by": fact.superseded_by,
            "scope": fact.scope,
            "author": fact.author,
            "source_agent": fact.source_agent,
            "source_model": fact.source_model,
            "timestamp": fact.timestamp,
            "parent_hash": fact.parent_hash,
            "statement": fact.statement,
        })
    for deferral in checkpoint.user_deferrals:
        records.append({
            "type": "user_deferral_lock",
            "id": deferral.deferral_id,
            "entity_key": deferral.feature_or_option,
            "status": "LOCKED_DEFERRED",
            "superseded_by": None,
            "scope": MemoryScope.SHARED.value,
            "author": deferral.author,
            "source_agent": checkpoint.source_agent,
            "source_model": checkpoint.source_model,
            "timestamp": deferral.timestamp,
            "parent_hash": checkpoint.parent_checkpoint_id,
            "statement": deferral.directive,
        })
    return records
