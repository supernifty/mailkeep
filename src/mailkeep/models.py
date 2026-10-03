from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RemoteMessage:
    account_id: str
    mailbox: str
    uidvalidity: int
    uid: int
    gmail_msgid: str | None
    size: int | None = None


@dataclass(frozen=True)
class Address:
    name: str
    email: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "email": self.email}


@dataclass(frozen=True)
class Attachment:
    ordinal: str
    original_name: str | None
    stored_name: str
    media_type: str
    disposition: str | None
    content_id: str | None
    data: bytes
    sha256: str
    decode_error: str | None = None


@dataclass(frozen=True)
class ParsedMessage:
    subject: str
    message_id: str | None
    message_date: datetime | None
    date_source: str
    senders: tuple[Address, ...]
    recipients: tuple[Address, ...]
    body_markdown: str
    attachments: tuple[Attachment, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnrichmentResult:
    provider: str
    model: str
    categories: dict[str, float] = field(default_factory=dict)
    document_type: str | None = None
    document_type_confidence: float | None = None
    entities: tuple[dict[str, str], ...] = ()
    request_id: str | None = None
    input_hash: str = ""
    config_hash: str = ""
    duration_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredObject:
    archive_id: str
    raw_sha256: str
    object_dir: Path
    eml_path: Path
    metadata_path: Path
    deduplicated: bool
