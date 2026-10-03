from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import EnrichmentResult, ParsedMessage, StoredObject
from .mime import safe_filename


GENERATED_START = "<!-- mailkeep:generated:start -->"
GENERATED_END = "<!-- mailkeep:generated:end -->"
PERSONAL_START = "<!-- mailkeep:personal:start -->"
PERSONAL_END = "<!-- mailkeep:personal:end -->"


def _managed_hash(front: dict[str, Any], generated: str) -> str:
    clean = dict(front)
    clean.pop("managed_sha256", None)
    def normalize(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    normalized_generated = generated.replace("\r\n", "\n").replace("\r", "\n")
    canonical = json.dumps(normalize(clean), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n" + normalized_generated.strip() + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _personal(existing: str | None) -> str:
    if not existing:
        return "\n"
    pattern = re.compile(
        re.escape(PERSONAL_START) + r"(.*?)" + re.escape(PERSONAL_END), re.S
    )
    match = pattern.search(existing)
    return match.group(1) if match else "\n"


def note_filename(parsed: ParsedMessage, archive_id: str) -> str:
    date = parsed.message_date.date().isoformat() if parsed.message_date else "undated"
    sender = parsed.senders[0].name or parsed.senders[0].email if parsed.senders else "unknown sender"
    sender = safe_filename(sender, "unknown-sender")[:50]
    subject = safe_filename(parsed.subject, "no-subject")[:90]
    return f"{date} - {sender} - {subject} - {archive_id[:8]}.md"


def note_relative_path(parsed: ParsedMessage, archive_id: str) -> Path:
    directory = (Path(f"{parsed.message_date.year:04d}") /
                 f"{parsed.message_date.month:02d}") if parsed.message_date else Path("undated")
    return directory / note_filename(parsed, archive_id)


def build_note(
    parsed: ParsedMessage,
    stored: StoredObject,
    account_id: str,
    archive_root: Path,
    enrichment: EnrichmentResult,
    existing: str | None = None,
    processed_at: str | None = None,
) -> tuple[str, str]:
    object_rel = stored.object_dir.relative_to(archive_root)
    eml_rel = object_rel / "original.eml"
    attachments: list[dict[str, Any]] = []
    for item in parsed.attachments:
        relative = object_rel / "attachments" / item.stored_name
        absolute = archive_root / relative
        attachments.append({
            "name": item.original_name,
            "media_type": item.media_type,
            "size": len(item.data),
            "sha256": item.sha256,
            "path": relative.as_posix(),
            "file_url": absolute.resolve().as_uri(),
        })
    chosen = [name for name, score in enrichment.categories.items() if score >= 0]
    front: dict[str, Any] = {
        "mailkeep_schema": 1,
        "archive_id": stored.archive_id,
        "message_date": parsed.message_date.isoformat() if parsed.message_date else None,
        "date_source": parsed.date_source,
        "senders": [a.as_dict() for a in parsed.senders],
        "recipients": [a.as_dict() for a in parsed.recipients],
        "subject": parsed.subject,
        "message_id": parsed.message_id,
        "account": account_id,
        "categories": chosen,
        "document_type": enrichment.document_type,
        "entities": list(enrichment.entities),
        "attachments": attachments,
        "eml_sha256": stored.raw_sha256,
        "eml_path": eml_rel.as_posix(),
        "eml_file_url": stored.eml_path.resolve().as_uri(),
        "enrichment_provider": enrichment.provider,
        "enrichment_model": enrichment.model,
        "processed_at": processed_at or datetime.now(UTC).isoformat(),
    }
    generated = f"{GENERATED_START}\n\n{parsed.body_markdown.strip()}\n\n{GENERATED_END}"
    # Hash the YAML round-trip representation that verification will see. YAML
    # may normalize scalar types, so hashing the pre-serialization Python
    # objects would make a freshly written note fail its own verification.
    roundtrip_front = yaml.safe_load(
        yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    )
    digest = _managed_hash(roundtrip_front, generated)
    front["managed_sha256"] = digest
    text = (
        "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip() +
        "\n---\n\n" + generated + "\n\n" + PERSONAL_START + _personal(existing) +
        PERSONAL_END + "\n"
    )
    return text, digest


def write_note(notes_dir: Path, relative_path: str | Path, content: str) -> Path:
    destination = notes_dir / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)
    return destination


def parse_managed(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("note has no YAML front matter")
    try:
        front_text, rest = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("note front matter is unterminated") from exc
    front = yaml.safe_load(front_text)
    if not isinstance(front, dict):
        raise ValueError("note front matter is not a mapping")
    start = rest.find(GENERATED_START)
    end = rest.find(GENERATED_END)
    if start < 0 or end < start:
        raise ValueError("note generated region is missing")
    generated = rest[start:end + len(GENERATED_END)]
    return front, generated


def verify_managed(text: str) -> tuple[bool, str, str]:
    front, generated = parse_managed(text)
    expected = str(front.get("managed_sha256", ""))
    actual = _managed_hash(front, generated)
    return expected == actual, expected, actual
