"""First-class Claude Desktop Adapter (macOS & Linux Chromium IndexedDB LevelDB + Snappy + V8 Clone).

Discovers cached Claude Desktop conversations (`list_sessions`) and extracts clean,
secret-redacted `MemoryCheckpointIR` checkpoints (`extract_checkpoint`) without requiring
any external C extensions or third-party dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import struct
from typing import Dict, List, Optional, Tuple

from memgit.adapters.base import AgentAdapter, AgentBootSimulationReport, AgentSessionSummary
from memgit.models import (
    AgentType,
    ChatTurn,
    ExecutionStack,
    HostFingerprint,
    MemoryCheckpointIR,
    MemoryScope,
    ProvenanceNode,
    SanitizationStats,
    TaskLockStatus,
    TaskStep,
    TopicMemoryFile,
    utc_now_iso,
)
from memgit.paths import claude_desktop_indexeddb_dir
from memgit.sanitizer import normalize_paths_to_ir, redact_checkpoint_secrets, strip_thinking_blocks

LEVELDB_BLOCK_SIZE = 32768
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
UUID_BYTES_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def parse_leveldb_wal_records(raw: bytes) -> List[bytes]:
    """Parse LevelDB 32KB WAL blocks into logical payload records."""
    records: List[bytes] = []
    current = bytearray()
    pos = 0
    total = len(raw)
    while pos < total:
        block = raw[pos : pos + LEVELDB_BLOCK_SIZE]
        pos += LEVELDB_BLOCK_SIZE
        bpos = 0
        blen = len(block)
        while bpos + 7 <= blen:
            _crc, length, rtype = struct.unpack_from("<IHB", block, bpos)
            if rtype == 0:
                break
            bpos += 7
            if bpos + length > blen:
                break
            payload = block[bpos : bpos + length]
            bpos += length
            if rtype == 1:
                records.append(bytes(payload))
            elif rtype == 2:
                current = bytearray(payload)
            elif rtype == 3:
                current.extend(payload)
            elif rtype == 4:
                current.extend(payload)
                records.append(bytes(current))
                current = bytearray()
    return records


def snappy_decompress(data: bytes, max_output_bytes: int = 20_000_000) -> bytes:
    """Pure-Python Snappy stream decompressor (used for Chromium IndexedDB `\\xff\\x11\\x02` blobs)."""
    i = 0
    n = len(data)
    ulen = 0
    shift = 0
    while i < n:
        b = data[i]
        i += 1
        ulen |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 35:
            return b""
    if ulen <= 0 or ulen > max_output_bytes:
        return b""

    out = bytearray()
    while i < n and len(out) < ulen:
        tag = data[i]
        i += 1
        tt = tag & 0x03
        if tt == 0:
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                extra = ln - 59
                if i + extra > n:
                    return b""
                ln = int.from_bytes(data[i : i + extra], "little") + 1
                i += extra
            if i + ln > n:
                return b""
            out.extend(data[i : i + ln])
            i += ln
        elif tt == 1:
            if i >= n:
                return b""
            ln = ((tag >> 2) & 0x07) + 4
            offset = ((tag & 0xE0) << 3) | data[i]
            i += 1
            if offset == 0 or offset > len(out):
                return b""
            for _ in range(ln):
                out.append(out[-offset])
        elif tt == 2:
            if i + 2 > n:
                return b""
            ln = (tag >> 2) + 1
            offset = int.from_bytes(data[i : i + 2], "little")
            i += 2
            if offset == 0 or offset > len(out):
                return b""
            for _ in range(ln):
                out.append(out[-offset])
        elif tt == 3:
            if i + 4 > n:
                return b""
            ln = (tag >> 2) + 1
            offset = int.from_bytes(data[i : i + 4], "little")
            i += 4
            if offset == 0 or offset > len(out):
                return b""
            for _ in range(ln):
                out.append(out[-offset])
    return bytes(out)


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    val = 0
    shift = 0
    n = len(buf)
    while pos < n:
        c = buf[pos]
        pos += 1
        val |= (c & 0x7F) << shift
        if not (c & 0x80):
            return val, pos
        shift += 7
        if shift > 35:
            return 0, pos
    return val, pos


def extract_v8_strings(buf: bytes) -> List[Tuple[int, str]]:
    """Extract V8 Structured Clone strings (OneByte 0x22, TwoByte UTF-16LE 0x63, UTF-8 0x53)."""
    strings: List[Tuple[int, str]] = []
    i = 0
    n = len(buf)
    while i < n:
        tag = buf[i]
        if tag == 0x22:  # OneByteString (latin1)
            ln, nxt = _read_varint(buf, i + 1)
            if 0 <= ln <= 100_000 and nxt + ln <= n:
                chunk = buf[nxt : nxt + ln]
                if all(c >= 9 for c in chunk):
                    strings.append((i, chunk.decode("latin1")))
                    i = nxt + ln
                    continue
        elif tag == 0x63:  # TwoByteString (utf-16le)
            ln, nxt = _read_varint(buf, i + 1)
            if 2 <= ln <= 200_000 and ln % 2 == 0 and nxt + ln <= n:
                chunk = buf[nxt : nxt + ln]
                # Guard against ASCII text containing literal 'c' (0x63) being misread as UTF-16LE:
                # real UTF-16LE strings do not have >70% of both odd and even bytes in printable ASCII.
                odd_ascii = sum(1 for b in chunk[1::2] if 0x20 <= b <= 0x7E)
                if odd_ascii <= (len(chunk) // 2) * 0.5:
                    try:
                        decoded = chunk.decode("utf-16le")
                        if all(ch.isprintable() or ch in "\n\r\t" for ch in decoded):
                            strings.append((i, decoded))
                            i = nxt + ln
                            continue
                    except UnicodeDecodeError:
                        pass
        elif tag == 0x53:  # Utf8String
            ln, nxt = _read_varint(buf, i + 1)
            if 0 <= ln <= 100_000 and nxt + ln <= n:
                try:
                    strings.append((i, buf[nxt : nxt + ln].decode("utf-8")))
                    i = nxt + ln
                    continue
                except UnicodeDecodeError:
                    pass
        i += 1
    return strings


def parse_claude_v8_conversation(buf: bytes) -> Optional[Dict[str, object]]:
    """Parse a decompressed V8 IndexedDB payload into `{uuid, title, updated_at, turns}`."""
    if b"sender" not in buf:
        return None
    v8_strs = extract_v8_strings(buf)
    if not v8_strs:
        return None

    conv_uuid: Optional[str] = None
    title: Optional[str] = None
    updated_at: Optional[str] = None
    turns: List[ChatTurn] = []

    # Extract top-level metadata (`conversationUuid`, `uuid`, `name`, `updated_at`) BEFORE `messages`/`chat_messages`
    msg_boundary = next(
        (i for i, (_, s) in enumerate(v8_strs) if s in ("messages", "chat_messages", "sender")),
        len(v8_strs),
    )
    ignored_names = {"human", "assistant", "text", "chat_messages", "messages", "artifacts", "web_search", "Read", "Write", "Bash", "Edit"}
    for i, (_, s) in enumerate(v8_strs[:msg_boundary]):
        if s == "conversationUuid" and i + 1 < len(v8_strs):
            cand = v8_strs[i + 1][1]
            if UUID_RE.match(cand):
                conv_uuid = cand
        elif s == "uuid" and i + 1 < len(v8_strs) and conv_uuid is None:
            cand = v8_strs[i + 1][1]
            if UUID_RE.match(cand):
                conv_uuid = cand
        elif s in ("name", "title") and i + 1 < len(v8_strs) and title is None:
            cand = v8_strs[i + 1][1].strip()
            if 2 <= len(cand) <= 240 and cand not in ignored_names:
                title = cand
        elif s in ("updated_at", "conversationUpdatedAt") and i + 1 < len(v8_strs) and updated_at is None:
            cand = v8_strs[i + 1][1]
            if ISO_TS_RE.match(cand):
                updated_at = cand

    # Pair sender ("human"/"assistant") with subsequent "text" field values
    idx = 0
    while idx < len(v8_strs):
        _, s = v8_strs[idx]
        if s == "sender" and idx + 1 < len(v8_strs) and v8_strs[idx + 1][1] in ("human", "assistant"):
            role = "user" if v8_strs[idx + 1][1] == "human" else "assistant"
            texts: List[str] = []
            ts: Optional[str] = None
            j = idx + 2
            while j < len(v8_strs) and v8_strs[j][1] != "sender":
                key = v8_strs[j][1]
                if key == "created_at" and j + 1 < len(v8_strs) and ISO_TS_RE.match(v8_strs[j + 1][1]):
                    if ts is None:
                        ts = v8_strs[j + 1][1]
                elif key == "text" and j + 1 < len(v8_strs):
                    val = v8_strs[j + 1][1]
                    if len(val) > 3 and val not in ("text", "thinking", "summary", "content", "type"):
                        texts.append(val)
                j += 1
            if texts:
                best = max(texts, key=len).strip()
                if not turns or turns[-1].content != best:
                    turns.append(
                        ChatTurn(
                            turn_id=f"turn_{len(turns) + 1:03d}",
                            role=role,
                            content=best,
                            timestamp=ts or updated_at or utc_now_iso(),
                        )
                    )
            idx = j
        else:
            idx += 1

    if not turns:
        return None
    if not conv_uuid:
        m = UUID_BYTES_RE.search(buf)
        conv_uuid = m.group(0).decode("ascii") if m else "claude-desktop-session"
    if not title:
        first_user = next((t.content for t in turns if t.role == "user"), turns[0].content)
        first_line = first_user.splitlines()[0].strip()
        title = (first_line[:77] + "...") if len(first_line) > 80 else first_line

    return {
        "uuid": conv_uuid,
        "title": title,
        "updated_at": updated_at or (turns[-1].timestamp if turns else utc_now_iso()),
        "turns": turns,
    }


def _scan_indexeddb_conversations(idb_dir: Path) -> Dict[str, Dict[str, object]]:
    """Scan all LevelDB `*.log` (and `*.ldb`) files in Claude Desktop's IndexedDB folder and
    return `{conversation_uuid: {uuid, title, updated_at, turns, source_path}}` keeping the
    richest (most turns) record per conversation UUID."""
    found: Dict[str, Dict[str, object]] = {}
    if not idb_dir.exists():
        return found

    log_files = sorted(idb_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    ldb_files = sorted(idb_dir.glob("*.ldb"), key=lambda p: p.stat().st_mtime, reverse=True)

    for fpath in log_files + ldb_files:
        try:
            raw = fpath.read_bytes()
        except OSError:
            continue

        blobs: List[bytes] = []
        if fpath.suffix == ".log":
            records = parse_leveldb_wal_records(raw)
        else:
            records = [raw]

        for rec in records:
            if b"\xff\x11\x02" in rec:
                pos = 0
                while True:
                    idx = rec.find(b"\xff\x11\x02", pos)
                    if idx == -1:
                        break
                    dec = snappy_decompress(rec[idx + 3 :])
                    if dec and b"sender" in dec:
                        blobs.append(dec)
                    pos = idx + 3
            elif b"sender" in rec:
                blobs.append(rec)

        for blob in blobs:
            parsed = parse_claude_v8_conversation(blob)
            if not parsed:
                continue
            cid = str(parsed["uuid"])
            parsed["source_path"] = str(fpath)
            prev = found.get(cid)
            if prev is None or len(parsed["turns"]) >= len(prev["turns"]):  # type: ignore[arg-type]
                found[cid] = parsed

    return found


class ClaudeDesktopAdapter(AgentAdapter):
    """Read-only source adapter for Claude Desktop (macOS & Linux IndexedDB)."""

    agent_type = AgentType.CLAUDE_DESKTOP.value

    def list_sessions(
        self,
        user_home: Path,
        workspace_root: Optional[Path] = None,
        os_name: Optional[str] = None,
    ) -> List[AgentSessionSummary]:
        idb_dir = claude_desktop_indexeddb_dir(user_home.resolve(), os_name=os_name)
        convs = _scan_indexeddb_conversations(idb_dir)
        summaries: List[AgentSessionSummary] = []
        for cid, rec in convs.items():
            turns: List[ChatTurn] = rec["turns"]  # type: ignore[assignment]
            summaries.append(
                AgentSessionSummary(
                    session_id=cid,
                    agent_type=self.agent_type,
                    title=str(rec["title"]),
                    updated_at=str(rec["updated_at"]),
                    turn_count=len(turns),
                    workspace_path="",
                    source_path=str(rec.get("source_path", idb_dir)),
                )
            )
        summaries.sort(key=lambda s: (s.updated_at, s.turn_count), reverse=True)
        return summaries

    def extract_checkpoint(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        author: str,
        source_model: str,
        host_fingerprint: HostFingerprint,
        message: str = "Checkpoint",
        branch: str = "main",
        parent_checkpoint_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        title_query: Optional[str] = None,
        os_name: Optional[str] = None,
    ) -> MemoryCheckpointIR:
        idb_dir = claude_desktop_indexeddb_dir(user_home.resolve(), os_name=os_name)
        convs = _scan_indexeddb_conversations(idb_dir)
        if not convs:
            raise FileNotFoundError(
                f"No Claude Desktop conversations found in {idb_dir}. "
                f"Ensure Claude Desktop is installed and has synced conversations locally."
            )

        selected: Optional[Dict[str, object]] = None
        if conversation_id:
            if conversation_id in convs:
                selected = convs[conversation_id]
            else:
                matches = [v for k, v in convs.items() if k.lower().startswith(conversation_id.lower())]
                if len(matches) == 1:
                    selected = matches[0]
                elif len(matches) > 1:
                    raise ValueError(f"Ambiguous Claude Desktop conversation prefix '{conversation_id}'.")
                else:
                    raise FileNotFoundError(f"Claude Desktop conversation '{conversation_id}' not found.")
        elif title_query:
            q = title_query.lower().strip()
            matches = [v for v in convs.values() if q in str(v["title"]).lower()]
            if not matches:
                raise FileNotFoundError(f"No Claude Desktop conversation matching title '{title_query}'.")
            matches.sort(key=lambda m: (str(m["updated_at"]), len(m["turns"])), reverse=True)  # type: ignore[arg-type]
            selected = matches[0]
        else:
            ranked = sorted(
                convs.values(),
                key=lambda m: (str(m["updated_at"]), len(m["turns"])),  # type: ignore[arg-type]
                reverse=True,
            )
            selected = ranked[0]

        conv_uuid = str(selected["uuid"])
        conv_title = str(selected["title"])
        raw_turns: List[ChatTurn] = selected["turns"]  # type: ignore[assignment]

        stats = SanitizationStats()
        clean_turns: List[ChatTurn] = []
        for idx, turn in enumerate(raw_turns, start=1):
            body, t_cnt = strip_thinking_blocks(turn.content)
            stats.stripped_thinking_blocks += t_cnt
            body, p_cnt = normalize_paths_to_ir(body, host_fingerprint)
            stats.normalized_host_paths += p_cnt
            clean_turns.append(
                ChatTurn(
                    turn_id=f"turn_{idx:03d}",
                    role=turn.role,
                    content=body,
                    timestamp=turn.timestamp,
                )
            )
        stats.conversation_turns_captured = len(clean_turns)

        slug = re.sub(r"[^a-z0-9]+", "_", conv_title.lower()).strip("_") or "claude_desktop_session"
        final_repo = repo_name or slug
        final_msg = conv_title if message in ("Checkpoint", "") else message
        now_iso = datetime.now(timezone.utc).isoformat()
        chk_id = MemoryCheckpointIR.generate_id(final_repo, branch, final_msg, now_iso, parent_checkpoint_id)

        first_user_summary = next((t.content[:240] for t in clean_turns if t.role == "user"), conv_title)
        fact = ProvenanceNode(
            fact_id=f"fact_{slug[:24]}",
            entity_key=f"session.{slug[:32]}",
            category="context",
            statement=f"Source Claude Desktop conversation '{conv_title}' ({len(clean_turns)} turns): {first_user_summary}",
            scope=MemoryScope.SHARED.value,
            author=author,
            source_agent=self.agent_type,
            source_model=source_model,
            timestamp=now_iso,
        )
        stats.shared_facts_count = 1

        chk = MemoryCheckpointIR(
            checkpoint_id=chk_id,
            repo_name=final_repo,
            branch=branch,
            parent_checkpoint_id=parent_checkpoint_id,
            author=author,
            source_agent=self.agent_type,
            source_model=source_model,
            source_host=host_fingerprint,
            created_at=now_iso,
            message=final_msg,
            confirmed_facts=[fact],
            execution_stack=ExecutionStack(
                active_goal=conv_title,
                current_hypothesis=f"Continue work from Claude Desktop conversation '{conv_title}' ({conv_uuid}).",
                steps=[
                    TaskStep(
                        step_id=f"step_resume_{slug[:24]}",
                        title=f"Resume '{conv_title}' from the latest assistant turn",
                        status=TaskLockStatus.NOT_DONE.value,
                    )
                ],
            ),
            topic_memories=[
                TopicMemoryFile(
                    topic_slug=slug[:48],
                    title=conv_title,
                    summary=f"Extracted from Claude Desktop conversation {conv_uuid} ({len(clean_turns)} turns)",
                    content=f"Multi-turn conversation '{conv_title}' imported from Claude Desktop IndexedDB.",
                    scope=MemoryScope.SHARED.value,
                    tags=["claude-desktop", "conversation"],
                    updated_at=now_iso,
                )
            ],
            conversation_history=clean_turns,
            sanitization_stats=stats,
        )
        redact_checkpoint_secrets(chk)
        return chk

    def simulate_boot_and_load(
        self,
        workspace_root: Path,
        user_home: Path,
        repo_name: str,
        model_name: str,
        forbidden_source_paths: Optional[List[str]] = None,
    ) -> AgentBootSimulationReport:
        return AgentBootSimulationReport(
            agent_type=self.agent_type,
            model_name=model_name,
            workspace_root=str(workspace_root),
            user_home=str(user_home),
            within_budget_limits=True,
            assembled_system_context_preview="Claude Desktop is a read-only source adapter.",
        )
