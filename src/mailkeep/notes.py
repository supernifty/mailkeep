from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import AttachmentExtraction, EnrichmentResult, ParsedMessage, StoredObject
from .mime import safe_filename


GENERATED_START = "<!-- mailkeep:generated:start -->"
GENERATED_END = "<!-- mailkeep:generated:end -->"
PERSONAL_START = "<!-- mailkeep:personal:start -->"
PERSONAL_END = "<!-- mailkeep:personal:end -->"
MANAGED_HASH_RE = re.compile(r"<!-- mailkeep:managed-sha256: ([0-9a-f]{64}) -->")
PROPERTY_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED_PROPERTIES = frozenset({
    "date", "from", "to", "subject", "categories", "document_type",
})


def _safe_archive_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe derived artifact path: {relative}")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"derived artifact path escapes archive: {relative}")
    return path


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


def _address(value: Any) -> str:
    if value.name and value.email:
        return f"{value.name} <{value.email}>"
    return value.name or value.email


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _fixed_width_pages(text: str) -> str:
    parts = re.split(r"(?m)^# Page (\d+)\n\n", text)
    rendered: list[str] = []
    if parts[0].strip():
        parts = ["", "1", parts[0], *parts[1:]]
    for index in range(1, len(parts), 2):
        page = parts[index]
        content = parts[index + 1].rstrip()
        longest = max((len(match.group(0)) for match in re.finditer(r"~+", content)),
                      default=0)
        fence = "~" * max(4, longest + 1)
        rendered.append(f"#### Page {page}\n\n{fence}text\n{content}\n{fence}")
    return "\n\n".join(rendered)


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
    extractions: dict[str, AttachmentExtraction] | None = None,
    property_fields: tuple[str, ...] = (),
    note_chars_per_attachment: int = 50_000,
    note_chars_per_message: int = 100_000,
) -> tuple[str, str]:
    object_rel = stored.object_dir.relative_to(archive_root)
    attachment_urls: list[str] = []
    for item in parsed.attachments:
        relative = object_rel / "attachments" / item.stored_name
        absolute = archive_root / relative
        attachment_urls.append(absolute.resolve().as_uri())
    front: dict[str, Any] = {}
    if parsed.message_date:
        front["date"] = parsed.message_date.isoformat()
    if parsed.senders:
        front["from"] = [_address(value) for value in parsed.senders]
    if parsed.recipients:
        front["to"] = [_address(value) for value in parsed.recipients]
    front["subject"] = parsed.subject
    if enrichment.categories:
        front["categories"] = list(enrichment.categories)
    if enrichment.document_type:
        front["document_type"] = enrichment.document_type
    allowed_properties = {
        field for field in property_fields
        if PROPERTY_FIELD_RE.fullmatch(field) and field not in RESERVED_PROPERTIES
    }
    for entity in enrichment.entities:
        field = str(entity.get("field", ""))
        value = str(entity.get("value", "")).strip()
        if field in allowed_properties and value and field not in front:
            front[field] = value

    sections = ["## Message\n\n" + (parsed.body_markdown.strip() or
                                             "_No readable message body._")]
    if enrichment.entities:
        detail_lines = ["## Extracted details", ""]
        for entity in enrichment.entities:
            field = str(entity.get("field", "detail")).replace("_", " ").strip().title()
            value = str(entity.get("value", "")).strip()
            evidence = str(entity.get("evidence", "")).strip()
            line = f"- **{field}:** {value}"
            if evidence and evidence != value:
                line += f" — evidence: {evidence}"
            detail_lines.append(line)
        sections.append("\n".join(detail_lines))

    attachment_lines = ["## Attachments", ""]
    for item, file_url in zip(parsed.attachments, attachment_urls, strict=True):
        label = (item.original_name or item.stored_name).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        attachment_lines.append(
            f"- [{label}]({file_url}) — {item.media_type}; "
            f"{_human_size(len(item.data))}"
        )
    if parsed.attachments:
        sections.append("\n".join(attachment_lines))

    if extractions:
        extracted_lines = ["## Extracted attachment text", ""]
        remaining = note_chars_per_message
        for item in parsed.attachments:
            extraction = extractions.get(item.ordinal)
            if extraction is None:
                continue
            name = item.original_name or item.stored_name
            extracted_lines.extend([f"### {name}", ""])
            if extraction.outcome != "success" or not extraction.artifact_relpath:
                detail = f": {extraction.error}" if extraction.error else ""
                extracted_lines.extend([f"Extraction status: `{extraction.outcome}`{detail}", ""])
                continue
            artifact = _safe_archive_path(archive_root, extraction.artifact_relpath)
            text = artifact.read_text(encoding="utf-8")
            allowance = max(0, min(note_chars_per_attachment, remaining))
            included = text[:allowance]
            remaining -= len(included)
            # Extracted text is untrusted and must not terminate a managed region.
            included = re.sub(r"<!--\s*mailkeep:", "&lt;!-- mailkeep:", included,
                              flags=re.IGNORECASE)
            if extraction.parser == "pypdf-layout":
                included = _fixed_width_pages(included)
            else:
                included = re.sub(r"(?m)^# Page (\d+)$", r"#### Page \1", included)
            extracted_lines.extend([included.rstrip(), ""])
            if len(text) > allowance:
                link = artifact.resolve().as_uri()
                extracted_lines.extend([
                    f"_Text truncated in this note; [open the complete extracted text]({link})._",
                    "",
                ])
        sections.append("\n".join(extracted_lines).rstrip())
    sections.append(f"## Source\n\n[Open original email]({stored.eml_path.resolve().as_uri()})")
    generated_body = "\n\n".join(section for section in sections if section)
    generated_body = re.sub(
        r"<!--\s*mailkeep:", "&lt;!-- mailkeep:", generated_body,
        flags=re.IGNORECASE,
    )
    generated = f"{GENERATED_START}\n\n{generated_body}\n\n{GENERATED_END}"
    # Hash the YAML round-trip representation that verification will see. YAML
    # may normalize scalar types, so hashing the pre-serialization Python
    # objects would make a freshly written note fail its own verification.
    roundtrip_front = yaml.safe_load(
        yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    )
    digest = _managed_hash(roundtrip_front, generated)
    text = (
        "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip() +
        f"\n---\n\n<!-- mailkeep:managed-sha256: {digest} -->\n\n" +
        generated + "\n\n" + PERSONAL_START + _personal(existing) +
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
    hidden = MANAGED_HASH_RE.search(text)
    expected = hidden.group(1) if hidden else str(front.get("managed_sha256", ""))
    actual = _managed_hash(front, generated)
    return expected == actual, expected, actual
