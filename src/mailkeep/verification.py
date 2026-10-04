from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .mime import parse_message
from .notes import verify_managed
from .state import State
from .storage import stale_staging


@dataclass
class VerificationReport:
    checked: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _safe_join(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe relative path: {relative}")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes configured root: {relative}")
    return target


def verify_archive(state: State, archive_root: Path, notes_dir: Path,
                   archive_id: str, *, include_derived: bool = True,
                   include_note: bool = True) -> VerificationReport:
    report = VerificationReport(checked=1)
    with state.connect() as conn:
        archive = conn.execute("SELECT * FROM archives WHERE archive_id=?", (archive_id,)).fetchone()
        if archive is None:
            report.errors.append(f"unknown archive: {archive_id}")
            return report
        attachments = conn.execute(
            "SELECT * FROM attachments WHERE archive_id=? ORDER BY CAST(ordinal AS INTEGER)",
            (archive_id,),
        ).fetchall()
    try:
        object_dir = _safe_join(archive_root, archive["object_relpath"])
    except ValueError as exc:
        report.errors.append(str(exc))
        return report
    eml = object_dir / "original.eml"
    metadata_path = object_dir / "metadata.json"
    if not eml.is_file():
        report.errors.append(f"missing original.eml for {archive_id}")
        return report
    raw = eml.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != archive["raw_sha256"]:
        report.errors.append(f"raw SHA-256 mismatch for {archive_id}")
    if not metadata_path.is_file():
        report.errors.append(f"missing metadata.json for {archive_id}")
        return report
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"invalid metadata for {archive_id}: {exc}")
        return report
    if metadata.get("archive_id") != archive_id or metadata.get("raw_sha256") != digest:
        report.errors.append(f"metadata identity mismatch for {archive_id}")

    reparsed = parse_message(raw)
    derived = {a.ordinal: a for a in reparsed.attachments}
    db_ordinals = {row["ordinal"] for row in attachments}
    if set(derived) != db_ordinals:
        report.errors.append(f"MIME attachment manifest mismatch for {archive_id}")
    for row in attachments:
        if row["decode_error"]:
            report.errors.append(f"attachment {row['ordinal']} has a decoding error")
        try:
            path = _safe_join(object_dir / "attachments", row["stored_name"])
        except ValueError as exc:
            report.errors.append(str(exc))
            continue
        if not path.is_file():
            report.errors.append(f"missing attachment {row['stored_name']}")
            continue
        data = path.read_bytes()
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            report.errors.append(f"attachment hash/size mismatch: {row['stored_name']}")
        if row["ordinal"] in derived and derived[row["ordinal"]].sha256 != row["sha256"]:
            report.errors.append(f"attachment differs from MIME derivation: {row['stored_name']}")

    if include_derived:
        extractions = state.latest_extractions(archive_id)
        attachments_by_ordinal = {row["ordinal"]: row for row in attachments}
        for ordinal, extraction in extractions.items():
            attachment = attachments_by_ordinal.get(ordinal)
            if attachment is None or attachment["sha256"] != extraction.attachment_sha256:
                report.errors.append(f"extraction attachment identity mismatch: {archive_id}/{ordinal}")
                continue
            if extraction.outcome != "success":
                report.warnings.append(
                    f"attachment extraction {extraction.outcome}: {archive_id}/{ordinal}"
                )
                continue
            if not extraction.artifact_relpath or not extraction.manifest_relpath:
                report.errors.append(f"successful extraction has no artifacts: {archive_id}/{ordinal}")
                continue
            try:
                artifact = _safe_join(archive_root, extraction.artifact_relpath)
                manifest_path = _safe_join(archive_root, extraction.manifest_relpath)
                data = artifact.read_bytes()
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                data.decode("utf-8", "strict")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                report.errors.append(f"invalid derived extraction for {archive_id}/{ordinal}: {exc}")
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest != extraction.output_sha256 or len(data) != extraction.output_size:
                report.errors.append(f"derived extraction hash/size mismatch: {archive_id}/{ordinal}")
            expected_manifest = {
                "attachment_sha256": extraction.attachment_sha256,
                "parser": extraction.parser, "parser_version": extraction.parser_version,
                "config_hash": extraction.config_hash, "outcome": "success",
                "output_sha256": extraction.output_sha256, "output_size": extraction.output_size,
            }
            if any(manifest.get(key) != value for key, value in expected_manifest.items()):
                report.errors.append(f"derived extraction manifest mismatch: {archive_id}/{ordinal}")

    if include_note and archive["markdown_state"] == "complete":
        if not archive["note_relpath"]:
            report.errors.append(f"complete Markdown has no note path for {archive_id}")
        else:
            try:
                note = _safe_join(notes_dir, archive["note_relpath"])
                ok, expected, actual = verify_managed(note.read_text(encoding="utf-8"))
                if not ok or expected != archive["note_managed_sha256"]:
                    report.errors.append(f"managed Markdown hash mismatch for {archive_id}: {actual}")
            except (OSError, ValueError) as exc:
                report.errors.append(f"invalid note for {archive_id}: {exc}")
    return report


def verify_all(state: State, archive_root: Path, notes_dir: Path,
               archive_id: str | None = None) -> VerificationReport:
    report = VerificationReport()
    with state.connect() as conn:
        if archive_id:
            ids = [archive_id]
        else:
            ids = [row[0] for row in conn.execute("SELECT archive_id FROM archives ORDER BY archive_id")]
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            report.errors.extend(f"foreign-key violation: {tuple(row)}" for row in foreign)
        unlinked = conn.execute("SELECT id FROM remote_messages WHERE archive_id IS NULL").fetchall()
        report.warnings.extend(f"discovered remote message {row[0]} is not archived" for row in unlinked)
    for item in ids:
        child = verify_archive(state, archive_root, notes_dir, item)
        report.checked += child.checked
        report.errors.extend(child.errors)
        report.warnings.extend(child.warnings)
    report.errors.extend(f"incomplete staging directory: {path}" for path in stale_staging(archive_root))

    known_dirs: set[Path] = set()
    with state.connect() as conn:
        for row in conn.execute("SELECT object_relpath FROM archives"):
            try:
                known_dirs.add(_safe_join(archive_root, row[0]))
            except ValueError as exc:
                report.errors.append(str(exc))
    objects = archive_root / "objects"
    if objects.exists():
        for eml in objects.rglob("original.eml"):
            if eml.parent.resolve() not in known_dirs:
                report.errors.append(f"orphan archive object: {eml.parent}")
    return report
