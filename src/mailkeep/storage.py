from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from .models import ParsedMessage, StoredObject


ARCHIVE_NAMESPACE = uuid.UUID("2addb9e4-2351-4b43-a367-6de726cd2f42")
STAGING_RE = re.compile(r"^\.[0-9a-f-]{36}\.staging-[0-9a-f]{12}$")


def identity(raw: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(raw).hexdigest()
    return digest, str(uuid.uuid5(ARCHIVE_NAMESPACE, digest))


def object_relative_path(raw_sha256: str, archive_id: str) -> Path:
    return Path("objects") / raw_sha256[:2] / raw_sha256[2:4] / archive_id


def _write_fsynced(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def metadata_for(archive_id: str, raw_sha256: str, parsed: ParsedMessage) -> dict:
    return {
        "schema_version": 1,
        "archive_id": archive_id,
        "raw_sha256": raw_sha256,
        "message_id": parsed.message_id,
        "subject": parsed.subject,
        "message_date": parsed.message_date.isoformat() if parsed.message_date else None,
        "date_source": parsed.date_source,
        "senders": [a.as_dict() for a in parsed.senders],
        "recipients": [a.as_dict() for a in parsed.recipients],
        "body_markdown": parsed.body_markdown,
        "diagnostics": list(parsed.diagnostics),
        "attachments": [
            {
                "ordinal": a.ordinal, "original_name": a.original_name,
                "stored_name": a.stored_name, "media_type": a.media_type,
                "disposition": a.disposition, "content_id": a.content_id,
                "size": len(a.data), "sha256": a.sha256,
                "decode_error": a.decode_error,
            }
            for a in parsed.attachments
        ],
    }


def store(root: Path, raw: bytes, parsed: ParsedMessage) -> StoredObject:
    raw_sha256, archive_id = identity(raw)
    relative = object_relative_path(raw_sha256, archive_id)
    destination = root / relative
    if destination.is_dir():
        existing = destination / "original.eml"
        if existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest() == raw_sha256:
            return StoredObject(archive_id, raw_sha256, destination, existing,
                                destination / "metadata.json", True)
        raise RuntimeError(f"archive object collision or damage at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    staging = destination.parent / f".{archive_id}.staging-{token}"
    staging.mkdir(mode=0o700)
    try:
        attachments_dir = staging / "attachments"
        attachments_dir.mkdir()
        _write_fsynced(staging / "original.eml", raw)
        for attachment in parsed.attachments:
            _write_fsynced(attachments_dir / attachment.stored_name, attachment.data)
        metadata = json.dumps(
            metadata_for(archive_id, raw_sha256, parsed),
            ensure_ascii=False, sort_keys=True, indent=2,
        ).encode("utf-8") + b"\n"
        _write_fsynced(staging / "metadata.json", metadata)
        _fsync_dir(attachments_dir)
        _fsync_dir(staging)
        os.rename(staging, destination)
        _fsync_dir(destination.parent)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return StoredObject(archive_id, raw_sha256, destination,
                        destination / "original.eml", destination / "metadata.json", False)


def stale_staging(root: Path) -> list[Path]:
    objects = root / "objects"
    if not objects.exists():
        return []
    return [path for path in objects.rglob(".*.staging-*")
            if path.is_dir() and STAGING_RE.fullmatch(path.name)]


def reconcile_staging(root: Path) -> list[Path]:
    removed = stale_staging(root)
    for path in removed:
        shutil.rmtree(path)
    return removed
