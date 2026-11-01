from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from email import policy
from email.parser import BytesParser
from pathlib import Path

from bs4 import BeautifulSoup

from ..config import Config, ProcessorInstance
from ..locking import data_lock
from ..mime import parse_message
from ..state import State, utcnow
from ..verification import verify_archive
from .api import Definition, canonical, digest, normalized, validate_fields


def instance(config: Config, name: str) -> ProcessorInstance:
    for value in config.processors.instances:
        if value.id == name:
            return value
    raise ValueError(f"unknown processor instance: {name}")


def safe_path(root: Path, relative: str | Path) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe processor path")
    path = root
    if path.is_symlink():
        raise ValueError("symbolic links are not allowed in processor paths")
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symbolic links are not allowed in processor paths")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("processor path escapes configured root")
    return path


def has_schema(state: State) -> bool:
    with state.connect() as conn:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name='processor_attempts'").fetchone() is not None


def cache_key(row, plugin: ProcessorInstance) -> str:
    return digest({"archive": row["archive_id"], "raw": row["raw_sha256"],
                   "instance": plugin.id, "implementation": plugin.implementation,
                   "definition": plugin.plugin_definition().model_dump(),
                   "options": plugin.options_hash(), "input_contract": 1})


def matches(raw: bytes, plugin: ProcessorInstance) -> bool:
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    if len(msg.get_all("From", [])) != 1:
        return False
    header = msg.get("From")
    if header is None or header.defects or len(header.addresses) != 1:
        return False
    sender = header.addresses[0].addr_spec.casefold()
    return (sender in {s.casefold() for s in plugin.match.from_addresses} and
            any(s.casefold() in str(msg.get("Subject", "")).casefold()
                for s in plugin.match.subject_contains))


def raw_message(config: Config, row) -> bytes:
    path = safe_path(config.paths.archive_root, Path(row["object_relpath"]) / "original.eml")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
        raise ValueError(f"source hash mismatch: {row['archive_id']}")
    return raw


def message_input(raw: bytes, row) -> dict:
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    parsed = parse_message(raw)
    parts = []
    excluded = set()
    for node in mime.walk():
        if (node.get_content_disposition() == "attachment" or node.get_filename() or
                node.get_content_type() == "message/rfc822"):
            excluded.update(id(child) for child in node.walk())
    for ordinal, part in enumerate(mime.walk(), 1):
        if (id(part) in excluded or part.get_content_type() not in ("text/plain", "text/html") or
                part.get_content_disposition() == "attachment" or part.get_filename()):
            continue
        diagnostics = [str(type(d).__name__) for d in part.defects]
        try:
            data = part.get_payload(decode=True)
            text = data.decode(part.get_content_charset() or "utf-8", "strict")
        except (UnicodeError, LookupError, AttributeError):
            text = ""
            diagnostics.append("body decoding failed")
        diagnostics.extend(str(type(d).__name__) for d in part.defects if type(d).__name__ not in diagnostics)
        if text.strip() or diagnostics:
            parts.append({"ordinal": ordinal, "media_type": part.get_content_type(),
                          "text": text, "diagnostics": diagnostics})
    return {"archive_id": row["archive_id"], "raw_sha256": row["raw_sha256"],
            "subject": parsed.subject, "senders": [a.as_dict() for a in parsed.senders],
            "recipients": [a.as_dict() for a in parsed.recipients],
            "date": parsed.message_date.isoformat() if parsed.message_date else None,
            "body_markdown": parsed.body_markdown, "parts": parts}


def run_worker(config: Config, plugin: ProcessorInstance, message: dict) -> dict:
    request = canonical({"implementation": plugin.implementation,
                         "definition": plugin.plugin_definition().model_dump(),
                         "options": plugin.options, "message": message}).encode()
    if len(request) > 5 * 1024 * 1024:
        raise ValueError("processor input exceeds 5 MiB")
    scratch = config.processors.scratch_root.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="worker-", dir=scratch) as directory:
        work = Path(directory)
        env = {"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
               "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8", "TMPDIR": directory,
               "PYTHONDONTWRITEBYTECODE": "1"}
        with (work / "output.json").open("w+b") as output, (work / "stderr").open("w+b") as errors:
            process = subprocess.run([sys.executable, "-m", "mailkeep.processors.worker"],
                                     input=request, stdout=output, stderr=errors, cwd=work,
                                     env=env, timeout=10, check=False)
            if process.returncode:
                raise RuntimeError(f"processor worker exited with status {process.returncode}")
            output.seek(0)
            response = output.read(1024 * 1024 + 1)
            if len(response) > 1024 * 1024:
                raise ValueError("processor output exceeds 1 MiB")
            return json.loads(response)


def validate_result(payload: dict, definition: Definition, message: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("processor response must be an object")
    if payload.get("outcome") == "unsupported":
        if set(payload) != {"outcome", "reason"} or not isinstance(payload["reason"], str):
            raise ValueError("invalid unsupported response")
        return {"outcome": "unsupported", "reason": payload["reason"][:1000]}
    if set(payload) != {"outcome", "records", "evidence"} or payload["outcome"] != "success":
        raise ValueError("invalid processor result envelope")
    if not isinstance(payload["records"], list) or not 1 <= len(payload["records"]) <= 1000:
        raise ValueError("processor must return 1–1000 records")
    records = [validate_fields(r, definition.fields) for r in payload["records"]]
    view = definition.view
    periods = []
    for record in records:
        start, end = date.fromisoformat(record[view.start]), date.fromisoformat(record[view.end_exclusive])
        if (end - start).days != view.period_days or record[view.value] < 0:
            raise ValueError("invalid record period/value")
        periods.append((start, end))
    periods.sort()
    if any(a[1] > b[0] for a, b in zip(periods, periods[1:])):
        raise ValueError("overlapping records within a result")
    if not isinstance(payload["evidence"], list) or not 1 <= len(payload["evidence"]) <= 1000:
        raise ValueError("missing or excessive evidence")
    sources = {p["ordinal"]: normalized(BeautifulSoup(p["text"], "html.parser").get_text(" "))
               if p["media_type"] == "text/html" else normalized(p["text"])
               for p in message["parts"]}
    for item in payload["evidence"]:
        if (not isinstance(item, dict) or set(item) != {"part", "field", "text"} or
                type(item["part"]) is not int or not isinstance(item["field"], str) or
                not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 2000 or
                item["part"] not in sources or not normalized(item["text"]) or
                normalized(item["text"]) not in sources[item["part"]]):
            raise ValueError("invalid or unsupported source evidence")
    return {"outcome": "success", "records": records, "evidence": payload["evidence"]}


def plan(config: Config, state: State, plugin: ProcessorInstance, archive_id: str | None,
         retry_failed: bool) -> list[dict]:
    schema = has_schema(state)
    with state.connect() as conn:
        rows = conn.execute("SELECT * FROM archives" + (" WHERE archive_id=?" if archive_id else "") +
                            " ORDER BY archive_id", (archive_id,) if archive_id else ()).fetchall()
        if archive_id and not rows:
            raise ValueError(f"unknown archive: {archive_id}")
        plans = []
        for row in rows:
            if row["preservation_state"] != "preservation_verified":
                continue
            raw = raw_message(config, row)
            if not matches(raw, plugin):
                continue
            key = cache_key(row, plugin)
            latest = conn.execute("SELECT * FROM processor_attempts WHERE cache_key=? ORDER BY id DESC LIMIT 1",
                                  (key,)).fetchone() if schema else None
            action = "process"
            if latest:
                action = "reuse" if latest["result_id"] else "retry" if retry_failed else "skip"
            plans.append({"archive_id": row["archive_id"], "instance_id": plugin.id,
                          "cache_key": key, "action": action,
                          "previous_outcome": latest["outcome"] if latest else None,
                          "result_id": latest["result_id"] if latest else None})
    return plans


def process_one(config: Config, state: State, plugin: ProcessorInstance, row, *,
                retry_failed: bool = False) -> dict:
    raw = raw_message(config, row)
    if not matches(raw, plugin):
        return {"archive_id": row["archive_id"], "outcome": "no_match"}
    key = cache_key(row, plugin)
    started = utcnow()
    definition = plugin.plugin_definition()
    with state.connect() as conn:
        latest = conn.execute("SELECT * FROM processor_attempts WHERE cache_key=? ORDER BY id DESC LIMIT 1",
                              (key,)).fetchone()
    if latest and not latest["result_id"] and not retry_failed:
        return {"archive_id": row["archive_id"], "instance_id": plugin.id,
                "outcome": latest["outcome"], "cached": True, "error": latest["error"]}
    result_id = latest["result_id"] if latest else None
    payload = None
    error = None
    try:
        if result_id:
            with state.connect() as conn:
                existing = conn.execute("SELECT * FROM processor_results WHERE id=?", (result_id,)).fetchone()
            if (existing is None or existing["archive_id"] != row["archive_id"] or
                    existing["instance_id"] != plugin.id or existing["cache_key"] != key or
                    digest(json.loads(existing["payload_json"])) != existing["result_hash"]):
                raise ValueError("cached processor result is corrupt")
            outcome = "cached"
        else:
            report = verify_archive(state, config.paths.archive_root, config.notes_dir, row["archive_id"],
                                    include_derived=False, include_note=False)
            if not report.ok:
                raise ValueError("source preservation verification failed")
            message = message_input(raw, row)
            payload = validate_result(run_worker(config, plugin, message), definition, message)
            outcome = payload["outcome"]
            error = payload.get("reason")
    except Exception as exc:
        outcome = "failed"
        # No arbitrary worker output is copied into logs.
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        result_id = None
    with state.transaction() as conn:
        if outcome == "success":
            cursor = conn.execute(
                "INSERT INTO processor_results(instance_id,archive_id,implementation,definition_json,series_id,"
                "identity_hash,options_hash,raw_sha256,cache_key,payload_json,result_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (plugin.id, row["archive_id"], plugin.implementation, canonical(definition.model_dump()),
                 plugin.series_id, plugin.identity_hash(), plugin.options_hash(), row["raw_sha256"],
                 key, canonical(payload), digest(payload), utcnow()))
            result_id = cursor.lastrowid
        conn.execute("INSERT INTO processor_attempts(instance_id,archive_id,cache_key,definition_json,series_id,identity_hash,"
                     "options_hash,raw_sha256,started_at,finished_at,outcome,error,result_id) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (plugin.id, row["archive_id"], key, canonical(definition.model_dump()), plugin.series_id,
                      plugin.identity_hash(), plugin.options_hash(), row["raw_sha256"], started, utcnow(), outcome, error, result_id))
        if result_id:
            conn.execute("INSERT INTO processor_selections(instance_id,archive_id,result_id,selected_at) VALUES (?,?,?,?)",
                         (plugin.id, row["archive_id"], result_id, utcnow()))
    return {"archive_id": row["archive_id"], "instance_id": plugin.id,
            "outcome": outcome, "result_id": result_id, "error": error}


def process_archive(config: Config, state: State, archive_id: str) -> list[dict]:
    if not config.processors.enabled:
        return []
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM archives WHERE archive_id=?", (archive_id,)).fetchone()
    results = []
    for plugin in config.processors.instances:
        try:
            item = process_one(config, state, plugin, row)
        except Exception as exc:
            item = {"instance_id": plugin.id, "archive_id": archive_id,
                    "outcome": "failed", "error": f"processor persistence/input error: {type(exc).__name__}"}
        if item["outcome"] != "no_match":
            results.append(item)
            if item["outcome"] in ("failed", "unsupported"):
                print(f"processor {plugin.id}: {item['outcome']}: {item.get('error')}", file=sys.stderr)
    return results


def process_emails(config: Config, name: str, *, execute: bool, archive_id: str | None = None,
                   retry_failed: bool = False) -> list[dict]:
    plugin = instance(config, name)
    if not config.paths.database.exists():
        raise ValueError("database does not exist; archive messages first")
    if retry_failed and not execute:
        raise ValueError("--retry-failed requires --execute")
    if not execute:
        return plan(config, State(config.paths.database, readonly=True), plugin, archive_id, retry_failed)
    if not config.processors.enabled:
        raise ValueError("processors are disabled; set processors.enabled: true")
    with data_lock(config.paths.archive_root):
        state = State(config.paths.database)
        state.initialize()
        items = plan(config, state, plugin, archive_id, retry_failed)
        results = []
        for item in items:
            with state.connect() as conn:
                row = conn.execute("SELECT * FROM archives WHERE archive_id=?", (item["archive_id"],)).fetchone()
            results.append(process_one(config, state, plugin, row, retry_failed=retry_failed))
        from .reports import render_report
        results.append(render_report(config, state, plugin))
        return results


def selected(state: State, plugin: ProcessorInstance) -> list[dict]:
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT r.*,a.note_relpath,a.object_relpath,a.raw_sha256 AS archive_raw_sha256 "
            "FROM processor_results r "
            "JOIN processor_selections s ON s.result_id=r.id JOIN archives a ON a.archive_id=r.archive_id "
            "WHERE s.id IN (SELECT MAX(id) FROM processor_selections WHERE instance_id=? GROUP BY archive_id) "
            "ORDER BY r.archive_id,r.id",
            (plugin.id,)).fetchall()
    current = []
    for row in rows:
        if row["identity_hash"] != plugin.identity_hash() or row["series_id"] != plugin.series_id:
            continue
        result = dict(row)
        definition = Definition.model_validate_json(row["definition_json"])
        payload = json.loads(row["payload_json"])
        if (digest(payload) != row["result_hash"] or row["raw_sha256"] != row["archive_raw_sha256"]):
            raise ValueError(f"corrupt processor result: {row['id']}")
        if definition.view != plugin.plugin_definition().view:
            raise ValueError("previous result has an incompatible time-series view")
        result["records"] = [validate_fields(r, definition.fields) for r in payload["records"]]
        result["stale"] = row["cache_key"] != cache_key(row, plugin)
        current.append(result)
    return current


def observations(state: State, plugin: ProcessorInstance) -> dict:
    results = selected(state, plugin)
    view = plugin.plugin_definition().view
    candidates = []
    for result in results:
        for record in result["records"]:
            candidates.append({"record": record, "source": result,
                               "start": record[view.start], "end": record[view.end_exclusive],
                               "value": record[view.value]})
    candidates.sort(key=lambda c: (c["start"], c["end"], c["source"]["id"]))
    groups = []
    for candidate in candidates:
        if not groups or candidate["start"] >= max(c["end"] for c in groups[-1]):
            groups.append([candidate])
        else:
            groups[-1].append(candidate)
    accepted, conflicts = [], []
    with state.connect() as conn:
        for group in groups:
            keys = {(c["start"], c["end"], c["value"]) for c in group}
            group_hash = digest([{"id": c["source"]["id"], "record": c["record"]} for c in group])
            resolution = conn.execute("SELECT * FROM processor_resolutions WHERE instance_id=? AND group_hash=? "
                                      "ORDER BY id DESC LIMIT 1", (plugin.id, group_hash)).fetchone()
            chosen = group
            if len(keys) > 1:
                if resolution:
                    chosen = [c for c in group if c["source"]["id"] == resolution["result_id"]]
                else:
                    conflicts.append({"group_hash": group_hash, "candidates": group})
                    continue
            if not chosen:
                raise ValueError("invalid conflict resolution")
            point = dict(chosen[0])
            point["sources"] = [c["source"] for c in group
                                if (c["start"], c["end"], c["value"]) ==
                                (point["start"], point["end"], point["value"])]
            accepted.append(point)
    return {"accepted": accepted, "conflicts": conflicts,
            "stale_count": sum(bool(r["stale"]) for r in results), "results": results}


def resolve(config: Config, name: str, result_id: int, reason: str) -> dict:
    plugin = instance(config, name)
    if not reason.strip() or len(reason) > 1000:
        raise ValueError("resolution requires a reason of 1–1000 characters")
    with data_lock(config.paths.archive_root):
        state = State(config.paths.database)
        state.initialize()
        dataset = observations(state, plugin)
        groups = [g for g in dataset["conflicts"] if any(c["source"]["id"] == result_id for c in g["candidates"])]
        if len(groups) != 1:
            raise ValueError("result must identify a candidate in exactly one unresolved conflict")
        with state.transaction() as conn:
            conn.execute("INSERT INTO processor_resolutions(instance_id,group_hash,result_id,reason,selected_at) "
                         "VALUES (?,?,?,?,?)", (name, groups[0]["group_hash"], result_id, reason.strip(), utcnow()))
        from .reports import render_report
        return render_report(config, state, plugin)
