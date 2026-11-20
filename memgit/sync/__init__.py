"""Modular agent-specific native storage sync helpers (protobuf wire format, Antigravity WAL/DB sync)."""

from memgit.sync.protobuf_wire import (
    pb_encode_bytes_field,
    pb_encode_varint,
    pb_encode_varint_field,
    pb_iter_fields,
    pb_patch_raw_summary_standalone,
    pb_patch_trajectory_metadata_standalone,
    pb_read_varint,
    sync_agyhub_summaries_proto,
)
from memgit.sync.antigravity_sync import (
    backup_sqlite_if_nonempty,
    patch_trajectory_db_standalone,
    sync_antigravity_cli_session_to_app,
)

__all__ = [
    "pb_encode_varint",
    "pb_read_varint",
    "pb_iter_fields",
    "pb_encode_bytes_field",
    "pb_encode_varint_field",
    "pb_patch_trajectory_metadata_standalone",
    "pb_patch_raw_summary_standalone",
    "sync_agyhub_summaries_proto",
    "backup_sqlite_if_nonempty",
    "patch_trajectory_db_standalone",
    "sync_antigravity_cli_session_to_app",
]
