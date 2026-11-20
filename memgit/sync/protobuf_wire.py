"""Pure-Python protobuf wire-format helpers for safe Tier-2 Antigravity metadata rewriting."""

from __future__ import annotations

from pathlib import Path
import time
from typing import List, Optional, Tuple


def pb_encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    val = 0
    shift = 0
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint overflow")
    raise ValueError("truncated varint")


def pb_iter_fields(buf: bytes) -> List[Tuple[int, int, bytes, bytes]]:
    """Parse top-level protobuf fields as (field_number, wire_type, raw_field_bytes, payload_bytes)."""
    fields: List[Tuple[int, int, bytes, bytes]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        start = pos
        tag, pos = pb_read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 7
        if field_num == 0:
            raise ValueError("invalid field number 0")
        if wire_type == 0:
            _, pos = pb_read_varint(buf, pos)
            payload = buf[start:pos]
        elif wire_type == 1:
            if pos + 8 > n:
                raise ValueError("truncated 64-bit field")
            payload = buf[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = pb_read_varint(buf, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            payload = buf[pos : pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > n:
                raise ValueError("truncated 32-bit field")
            payload = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields.append((field_num, wire_type, buf[start:pos], payload))
    return fields


def pb_encode_bytes_field(field_num: int, payload: bytes) -> bytes:
    return (
        pb_encode_varint((field_num << 3) | 2)
        + pb_encode_varint(len(payload))
        + payload
    )


def pb_encode_varint_field(field_num: int, value: int) -> bytes:
    return pb_encode_varint((field_num << 3) | 0) + pb_encode_varint(value)


def pb_patch_trajectory_metadata_standalone(data: bytes, conv_id: Optional[str] = None) -> bytes:
    """Rewrite Antigravity's trajectory metadata protobuf so it matches a native standalone
    conversation under top-level Conversations (`project_id = "outside-of-project"`, no
    workspace info `f1` or workspace URI `f7`)."""
    try:
        fields = pb_iter_fields(data) if data else []
    except ValueError:
        return data
    out = bytearray()
    saw_cid = False
    saw_project = False
    for field_num, wire_type, raw, _payload in fields:
        if field_num in (1, 7):
            continue
        if field_num == 6 and wire_type == 2:
            saw_cid = True
            out.extend(raw)
        elif field_num == 18 and wire_type == 2:
            out.extend(pb_encode_bytes_field(18, b"outside-of-project"))
            saw_project = True
        else:
            out.extend(raw)
    if not saw_cid and conv_id:
        out.extend(pb_encode_bytes_field(6, conv_id.encode("utf-8")))
    if not saw_project:
        out.extend(pb_encode_bytes_field(18, b"outside-of-project"))
    return bytes(out)


def pb_patch_raw_summary_standalone(
    data: bytes,
    title: Optional[str] = None,
    conv_id: Optional[str] = None,
) -> bytes:
    """Rewrite Antigravity's `conversation_summaries.raw_summary` protobuf so it matches a
    native Desktop App standalone conversation: no workspace block (`f9`), no CLI source enum
    (`f20`), `outside-of-project` inside `f17` with exact length varints, and the original
    conversation title in both `f1` and annotations block `f15`."""
    try:
        fields = pb_iter_fields(data)
    except ValueError:
        return data
    out = bytearray()
    saw_title = False
    saw_f15 = False
    saw_f17 = False
    title_bytes = title.encode("utf-8") if title else None
    ann_msg = b""
    if title_bytes is not None:
        ts_msg = pb_encode_varint_field(1, int(time.time()))
        ann_msg = pb_encode_bytes_field(1, title_bytes) + pb_encode_bytes_field(7, ts_msg)

    for field_num, wire_type, raw, payload in fields:
        if field_num in (9, 20):
            continue
        if field_num == 1 and wire_type == 2 and title_bytes is not None:
            out.extend(pb_encode_bytes_field(1, title_bytes))
            saw_title = True
        elif field_num == 15 and wire_type == 2 and title_bytes is not None:
            out.extend(pb_encode_bytes_field(15, ann_msg))
            saw_f15 = True
        elif field_num == 17 and wire_type == 2:
            patched_meta = pb_patch_trajectory_metadata_standalone(payload, conv_id=conv_id)
            out.extend(pb_encode_bytes_field(17, patched_meta))
            saw_f17 = True
        else:
            out.extend(raw)
    if title_bytes is not None and not saw_title:
        out = bytearray(pb_encode_bytes_field(1, title_bytes)) + out
    if title_bytes is not None and not saw_f15:
        out.extend(pb_encode_bytes_field(15, ann_msg))
    if not saw_f17:
        patched_meta = pb_patch_trajectory_metadata_standalone(b"", conv_id=conv_id)
        out.extend(pb_encode_bytes_field(17, patched_meta))
    return bytes(out)


def sync_agyhub_summaries_proto(
    proto_path: Path,
    conv_id: str,
    raw_summary: bytes,
    strip_cli_marker: bool = True,
) -> bool:
    """Atomically upsert `(conv_id, raw_summary)` into `~/.gemini/antigravity/agyhub_summaries_proto.pb`."""
    if not proto_path.exists():
        return False
    try:
        existing = proto_path.read_bytes()
        fields = pb_iter_fields(existing)
    except (OSError, ValueError):
        return False

    cid_bytes = conv_id.encode("utf-8")
    new_entry_payload = pb_encode_bytes_field(1, cid_bytes) + pb_encode_bytes_field(2, raw_summary)
    new_entry = pb_encode_bytes_field(1, new_entry_payload)

    out = bytearray()
    replaced = False
    for field_num, wire_type, raw, payload in fields:
        if wire_type == 2 and field_num in (1, 2):
            try:
                sub = pb_iter_fields(payload)
            except ValueError:
                out.extend(raw)
                continue
            entry_cid = next((sp for sfn, swt, _, sp in sub if sfn == 1 and swt == 2), None)
            if field_num == 1 and entry_cid == cid_bytes:
                out.extend(new_entry)
                replaced = True
                continue
            if field_num == 2 and strip_cli_marker and entry_cid == cid_bytes:
                continue
        out.extend(raw)

    if not replaced:
        out.extend(new_entry)

    tmp_path = proto_path.with_suffix(".pb.tmp")
    try:
        tmp_path.write_bytes(bytes(out))
        tmp_path.replace(proto_path)
        return True
    except OSError:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return False
