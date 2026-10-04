from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pypdf

from .config import Config
from .state import State, utcnow


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _attachment_path(config: Config, row: Any) -> Path:
    root = config.paths.archive_root.resolve()
    relative = Path(row["object_relpath"]) / "attachments" / row["stored_name"]
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe attachment path for {row['archive_id']}/{row['ordinal']}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"missing or unsafe attachment for {row['archive_id']}/{row['ordinal']}")
    data = path.read_bytes()
    if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError(f"attachment integrity mismatch for {row['archive_id']}/{row['ordinal']}")
    return path


def parser_identity(path: Path, media_type: str) -> tuple[str, str, str]:
    with path.open("rb") as handle:
        signature = handle.read(5)
    if signature == b"%PDF-":
        return "pdf", "pypdf-layout", pypdf.__version__
    if media_type.lower() == "text/plain":
        return "text", "python-codecs", "1"
    return "unsupported", "none", "1"


def _safe(value: str) -> str:
    return _SAFE_COMPONENT.sub("_", value).strip("._") or "unknown"


def _publish(root: Path, attachment_sha256: str, parser: str, parser_version: str,
             config_hash: str, result: dict[str, Any]) -> dict[str, Any]:
    text = str(result["text"])
    data = text.encode("utf-8")
    output_sha256 = hashlib.sha256(data).hexdigest()
    relative_dir = (Path("derived") / "attachments" / attachment_sha256[:2] /
                    attachment_sha256 / _safe(parser) / _safe(parser_version) /
                    config_hash)
    destination = root / relative_dir
    manifest = {
        "schema_version": 1, "attachment_sha256": attachment_sha256,
        "parser": parser, "parser_version": parser_version,
        "config_hash": config_hash, "outcome": "success",
        "page_count": result.get("page_count"),
        "page_boundaries": result.get("page_boundaries", []),
        "output_sha256": output_sha256, "output_size": len(data),
        "warnings": result.get("warnings", []),
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
                     + "\n").encode("utf-8")
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex[:12]}"
        staging.mkdir(mode=0o700)
        try:
            (staging / "text.txt").write_bytes(data)
            (staging / "manifest.json").write_bytes(manifest_data)
            os.rename(staging, destination)
        except BaseException:
            if staging.exists():
                for item in staging.iterdir():
                    item.unlink()
                staging.rmdir()
            raise
    else:
        existing = destination / "text.txt"
        manifest_path = destination / "manifest.json"
        if (not existing.is_file() or
                hashlib.sha256(existing.read_bytes()).hexdigest() != output_sha256):
            temporary = destination / f".text.txt.tmp-{uuid.uuid4().hex[:12]}"
            temporary.write_bytes(data)
            temporary.replace(existing)
        temporary_manifest = destination / f".manifest.json.tmp-{uuid.uuid4().hex[:12]}"
        temporary_manifest.write_bytes(manifest_data)
        temporary_manifest.replace(manifest_path)
    return {
        "artifact_relpath": (relative_dir / "text.txt").as_posix(),
        "manifest_relpath": (relative_dir / "manifest.json").as_posix(),
        "output_sha256": output_sha256, "output_size": len(data),
    }


def _cached_valid(config: Config, result: Any) -> bool:
    if result["outcome"] != "success":
        return True
    if not result["artifact_relpath"] or not result["manifest_relpath"]:
        return False
    try:
        artifact = config.paths.archive_root / result["artifact_relpath"]
        manifest = config.paths.archive_root / result["manifest_relpath"]
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        data = artifact.read_bytes()
        return (artifact.is_file() and manifest.is_file() and
                len(data) == result["output_size"] and
                hashlib.sha256(data).hexdigest() == result["output_sha256"] and
                manifest_payload.get("attachment_sha256") == result["attachment_sha256"] and
                manifest_payload.get("output_sha256") == result["output_sha256"])
    except (OSError, json.JSONDecodeError):
        return False


def _run_worker(config: Config, path: Path, kind: str, charset: str | None,
                timeout: float) -> dict[str, Any]:
    extraction = config.attachment_extraction
    request = {
        "path": str(path), "kind": kind, "charset": charset,
        "timeout_seconds": timeout,
        "max_pdf_pages": extraction.max_pdf_pages,
        "max_output_bytes": extraction.max_output_bytes,
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "mailkeep.attachment_worker"],
            input=json.dumps(request), text=True, capture_output=True,
            timeout=timeout, check=False, shell=False,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "error": f"worker exceeded {timeout:g} seconds"}
    if completed.returncode != 0:
        outcome = "resource_limited" if completed.returncode < 0 else "crashed"
        detail = completed.stderr.strip()[:1000] or f"worker exit {completed.returncode}"
        return {"outcome": outcome, "error": detail}
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"outcome": "crashed", "error": f"invalid worker response: {exc}"}
    if not isinstance(result, dict) or not isinstance(result.get("outcome"), str):
        return {"outcome": "crashed", "error": "invalid worker response shape"}
    return result


def plan_archive(config: Config, state: State, archive_id: str | None = None,
                 retry_failed: bool = False) -> list[dict[str, Any]]:
    rows = state.attachment_rows(archive_id)
    if archive_id is not None:
        with state.connect() as conn:
            if conn.execute("SELECT 1 FROM archives WHERE archive_id=?", (archive_id,)).fetchone() is None:
                raise KeyError(f"unknown archive: {archive_id}")
    output: list[dict[str, Any]] = []
    supported_by_archive: dict[str, int] = {}
    for row in rows:
        path = _attachment_path(config, row)
        kind, parser, version = parser_identity(path, row["media_type"])
        config_hash = config.attachment_extraction.fingerprint()
        cached = state.extraction_result(row["sha256"], parser, version, config_hash)
        attempted = state.extraction_attempt_exists(row["id"], parser, version, config_hash)
        if cached is not None and _cached_valid(config, cached):
            action = "reuse_cache"
        elif kind == "unsupported":
            action = "already_recorded" if attempted and not retry_failed else "unsupported"
        else:
            supported_by_archive[row["archive_id"]] = supported_by_archive.get(row["archive_id"], 0) + 1
            if supported_by_archive[row["archive_id"]] > config.attachment_extraction.max_supported_attachments_per_message:
                action = "message_attachment_limit"
            else:
                if cached is not None:
                    action = "repair_cache"
                elif attempted and not retry_failed:
                    action = "already_recorded"
                else:
                    action = "extract"
        output.append({
            "archive_id": row["archive_id"], "attachment_id": row["id"],
            "ordinal": row["ordinal"], "name": row["original_name"],
            "sha256": row["sha256"], "parser": parser,
            "parser_version": version, "action": action,
        })
    return output


def extract_archive(config: Config, state: State, archive_id: str,
                    retry_failed: bool = False, force: bool = False) -> list[dict[str, Any]]:
    extraction = config.attachment_extraction
    if not extraction.enabled and not force:
        return []
    rows = {row["id"]: row for row in state.attachment_rows(archive_id)}
    plan = plan_archive(config, state, archive_id, retry_failed)
    started_message = time.monotonic()
    output: list[dict[str, Any]] = []
    for item in plan:
        row = rows[item["attachment_id"]]
        started_at = utcnow()
        config_hash = extraction.fingerprint()
        path = _attachment_path(config, row)
        if item["action"] == "already_recorded":
            output.append(item)
            continue
        if item["action"] == "reuse_cache":
            cached = state.extraction_result(row["sha256"], item["parser"],
                                             item["parser_version"], config_hash)
            assert cached is not None
            state.record_cached_extraction(row, cached)
            output.append({**item, "outcome": cached["outcome"]})
            continue
        if item["action"] in ("unsupported", "message_attachment_limit"):
            outcome = "unsupported" if item["action"] == "unsupported" else "limit_exceeded"
            state.record_extraction(
                archive_id=archive_id, attachment_id=row["id"], attachment_sha256=row["sha256"],
                parser=item["parser"], parser_version=item["parser_version"],
                config_hash=config_hash, started_at=started_at, outcome=outcome,
                error=("unsupported attachment type" if outcome == "unsupported" else
                       "supported attachment count exceeds message limit"),
                cache_reused=False, cacheable=outcome == "unsupported",
            )
            output.append({**item, "outcome": outcome})
            continue
        elapsed = time.monotonic() - started_message
        remaining = extraction.timeout_seconds_per_message - elapsed
        if remaining <= 0:
            result = {"outcome": "message_timeout", "error": "message extraction time limit exceeded"}
        elif row["size"] > (extraction.max_pdf_bytes if item["parser"] == "pypdf-layout"
                            else extraction.max_text_bytes):
            result = {"outcome": "oversized", "error": "attachment exceeds configured byte limit"}
        else:
            result = _run_worker(config, path,
                                 "pdf" if item["parser"] == "pypdf-layout" else "text",
                                 row["declared_charset"],
                                 min(extraction.timeout_seconds_per_attachment, remaining))
        published: dict[str, Any] = {}
        if result["outcome"] == "success":
            try:
                published = _publish(config.paths.archive_root, row["sha256"], item["parser"],
                                     item["parser_version"], config_hash, result)
            except Exception as exc:
                result = {"outcome": "failed", "error": f"{type(exc).__name__}: {exc}"}
        cacheable = result["outcome"] in {
            "success", "unsupported", "oversized", "encrypted", "malformed", "blank", "image_only"
        }
        state.record_extraction(
            archive_id=archive_id, attachment_id=row["id"], attachment_sha256=row["sha256"],
            parser=item["parser"], parser_version=item["parser_version"], config_hash=config_hash,
            started_at=started_at, outcome=result["outcome"], error=result.get("error"),
            cache_reused=False, page_count=result.get("page_count"),
            page_boundaries=result.get("page_boundaries", []), warnings=result.get("warnings", []),
            cacheable=cacheable, **published,
        )
        output.append({**item, "outcome": result["outcome"], "error": result.get("error")})
    return output
