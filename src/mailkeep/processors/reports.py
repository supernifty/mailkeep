from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

from ..config import Config, ProcessorInstance
from ..locking import data_lock
from ..state import State, utcnow
from .api import Definition, canonical, digest, registry
from .engine import (has_schema, instance, message_input, observations,
                     raw_message, safe_path, validate_result)

RENDERER_VERSION = "3"
OWNER = "mailkeep-email-processors-v1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_issues(state: State, plugin: ProcessorInstance) -> list[dict]:
    with state.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT archive_id,outcome,error FROM processor_attempts WHERE id IN "
            "(SELECT MAX(id) FROM processor_attempts WHERE instance_id=? AND identity_hash=? GROUP BY archive_id) "
            "AND outcome IN ('failed','unsupported') ORDER BY archive_id", (plugin.id, plugin.identity_hash())).fetchall()]


def dataset(config: Config, state: State, plugin: ProcessorInstance) -> dict:
    from .historical import include_history
    data = observations(state, plugin)
    data = include_history(config, plugin, data)
    data["issues"] = latest_issues(state, plugin)
    data["fingerprint"] = digest({"results": [{"id": r["id"], "hash": r["result_hash"],
                                               "stale": r["stale"], "note": r["note_relpath"],
                                               "object": r["object_relpath"]} for r in data["results"]],
                                   "conflicts": data["conflicts"],
                                   "accepted": [(r["start"], r["end"], r["value"],
                                                 [s["id"] for s in r["sources"]]) for r in data["accepted"]],
                                   "issues": data["issues"], "renderer": RENDERER_VERSION,
                                   "historical_source": data["historical_hash"],
                                   "definition": plugin.plugin_definition().model_dump(),
                                   "options": plugin.options_hash(),
                                   "notes_root": str(config.notes_dir.resolve())})
    return data


def report_root(config: Config, plugin: ProcessorInstance) -> Path:
    return safe_path(config.notes_dir, Path("Reports") / plugin.id)


def owned_root(root: Path, plugin: ProcessorInstance, *, create: bool) -> None:
    marker = safe_path(root, "owner.json")
    expected = {"owner": OWNER, "instance_id": plugin.id}
    if root.exists():
        if marker.is_file():
            if json.loads(marker.read_text()) != expected:
                raise ValueError("report ownership marker differs")
        elif any(root.iterdir()):
            entries = {p.name for p in root.iterdir()}
            source = safe_path(root, "Historical data.md")
            if (plugin.implementation != "rescuetime-weekly" or entries != {"Historical data.md"} or
                    not source.is_file()):
                raise ValueError("refusing to overwrite an unowned report directory")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            with marker.open("x", encoding="utf-8") as handle:
                handle.write(canonical(expected) + "\n")


def read_report(root: Path, plugin: ProcessorInstance) -> tuple[dict, dict]:
    owned_root(root, plugin, create=False)
    index = safe_path(root, "Index.md")
    content = index.read_text(encoding="utf-8")
    match = re.match(r"<!-- mailkeep:processor-report (generation-[a-f0-9]{32}) -->\n", content)
    if not match:
        raise ValueError("missing report ownership/generation marker")
    generation = match[1]
    directory = safe_path(root, generation)
    manifest = json.loads(safe_path(directory, "manifest.json").read_text())
    if (manifest.get("owner") != OWNER or manifest.get("instance_id") != plugin.id or
            manifest.get("generation") != generation or
            not isinstance(manifest.get("files"), dict) or
            set(manifest["files"]) not in ({"history.csv"}, {"history.csv", "history.png"})):
        raise ValueError("invalid report generation manifest")
    for name, expected in manifest["files"].items():
        if sha(safe_path(directory, name)) != expected:
            raise ValueError(f"report artifact hash mismatch: {name}")
    if manifest.get("index_sha256") != hashlib.sha256(content.encode()).hexdigest():
        raise ValueError("report index hash mismatch")
    return manifest, {"index": content, "generation": generation}


def escaped(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace("|", "&#124;").replace("[", "&#91;").replace(
        "]", "&#93;").replace("\n", " ").replace("\r", " ")


def source_link(config: Config, root: Path, source: dict) -> str:
    if source.get("kind") == "historical":
        return "[Historical data](Historical%20data.md)"
    note = safe_path(config.notes_dir, source["note_relpath"]) if source["note_relpath"] else None
    target = note if note and note.is_file() else safe_path(
        config.paths.archive_root, Path(source["object_relpath"]) / "original.eml")
    relative = os.path.relpath(target, root)
    return f"[Email {source['id']}]({quote(relative, safe='/')})"


def display_value(plugin: ProcessorInstance, record: dict) -> str:
    if "historical_hours" in record:
        return f"{record['historical_hours']} h"
    view = plugin.plugin_definition().view
    if plugin.implementation == "rescuetime-weekly":
        minutes = record[view.value] // 60
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{record[view.value] / view.scale:g} {escaped(view.unit)}"


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def history_csv(plugin: ProcessorInstance, data: dict) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    fields = list(plugin.plugin_definition().fields)
    writer.writerow(["series_id", *fields, "source_archive_ids", "source_type", "historical_supplied_date", "historical_hours"])
    for point in data["accepted"]:
        writer.writerow([csv_cell(plugin.series_id), *[csv_cell(point["record"][k]) for k in fields],
                         ";".join(sorted({s["archive_id"] for s in point["sources"] if s.get("archive_id")})),
                         ";".join(sorted({s.get("kind", "email") for s in point["sources"]})),
                         ";".join(s["original_date"] for s in point["sources"] if s.get("kind") == "historical"),
                         ";".join(s["hours"] for s in point["sources"] if s.get("kind") == "historical")])
    return buffer.getvalue()


def plot_history(config: Config, plugin: ProcessorInstance, data: dict, path: Path) -> None:
    # Matplotlib is imported only for explicit report writes; cache stays in workspace.
    cache = config.processors.scratch_root.resolve() / "matplotlib"
    cache.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MPLCONFIGDIR")
    os.environ["MPLCONFIGDIR"] = str(cache)
    try:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
        view = plugin.plugin_definition().view
        figure = Figure(figsize=(9, 3.8), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots()
        points = data["accepted"]
        chunks = []
        for point in points:
            if not chunks or chunks[-1][-1]["end"] != point["start"]:
                chunks.append([])
            chunks[-1].append(point)
        trend_labelled = False
        for index, chunk in enumerate(chunks):
            axes.plot([date.fromisoformat(p["start"]) for p in chunk],
                      [p["value"] / view.scale for p in chunk],
                      marker="o", markersize=3.5, color="#28789b", linewidth=1.2,
                      alpha=0.55, label="Weekly values" if index == 0 and view.period_days == 7 else
                      "Reported values" if index == 0 else None)
            if view.period_days == 7 and len(chunk) >= 4:
                averages = [sum(p["value"] for p in chunk[i - 3:i + 1]) / (4 * view.scale)
                            for i in range(3, len(chunk))]
                axes.plot([date.fromisoformat(p["start"]) for p in chunk[3:]], averages,
                          color="#cc6600", linewidth=2.5, marker=".", markersize=3,
                          label=None if trend_labelled else "4-week moving average")
                trend_labelled = True
        if points:
            axes.legend(frameon=False, fontsize=9)
        axes.set_ylim(bottom=0)
        axes.set_ylabel(f"{view.label} ({view.unit})")
        axes.set_xlabel("Week starting" if view.period_days == 7 else "Period starting")
        axes.set_title(view.label + " over time")
        axes.grid(axis="y", alpha=0.2)
        locator = AutoDateLocator()
        axes.xaxis.set_major_locator(locator)
        axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        figure.savefig(path, dpi=140, metadata={"Software": "Mailkeep"})
        figure.clear()
    finally:
        if previous is None:
            os.environ.pop("MPLCONFIGDIR", None)
        else:
            os.environ["MPLCONFIGDIR"] = previous


def index_content(config: Config, plugin: ProcessorInstance, data: dict, root: Path,
                  generation: str, published: str) -> str:
    view = plugin.plugin_definition().view
    lines = [f"<!-- mailkeep:processor-report {generation} -->", f"# {escaped(view.label)}", "",
             "Generated by Mailkeep. Keep personal commentary in a separate note.", "",
             f"Series: {escaped(plugin.series_id)}. Metric: `{escaped(view.metric)}`.",
             f"Reporting timezone: {escaped(plugin.options.get('report_timezone', 'as reported'))}.",
             "Period ends are exclusive. Missing/conflicted periods are gaps, never zeros.",
             f"Generated: {published}.", "",
             f"Coverage: {len(data['accepted'])} accepted periods; {len(data['conflicts'])} conflicts; "
             f"{len(data['issues'])} extraction issues; {data['stale_count']} stale previous results.", ""]
    if plugin.implementation == "rescuetime-weekly":
        lines += ["Metric: actual achieved **All Distracting Time**, for **All day, every day**. "
                  "Email durations are rounded to whole minutes.", ""]
    if data["historical_rows"]:
        lines += [f"Includes {len(data['historical_rows'])} user-supplied historical observations. "
                  "Saturday labels move to the following Sunday; Sunday labels are unchanged. "
                  "Original labels and rounded decimal hours are preserved in "
                  "[Historical data](Historical%20data.md). Where weeks overlap, the precise email "
                  "value is preferred and the week appears once.", ""]
    if view.period_days == 7 and data["accepted"]:
        lines += ["The orange trend is a four-week trailing average. It starts after four "
                  "contiguous observations and restarts after missing or conflicted weeks.", ""]
    if data["historical_warnings"]:
        lines += ["> Reconciliation: " + escaped(warning) for warning in data["historical_warnings"]] + [""]
    if data["stale_count"]:
        lines += ["> Warning: previous accepted values remain because current extraction settings "
                  "or plugin versions have not produced replacements.", ""]
    if data["accepted"]:
        lines += [f"![{escaped(view.label)} history]({generation}/history.png)", ""]
    else:
        lines += ["No accepted observations are available yet.", ""]
    lines += [f"[Download CSV]({generation}/history.csv)", "",
              "| Period start | Period end (exclusive) | Value | Sources |",
              "| --- | --- | --- | --- |"]
    for point in reversed(data["accepted"]):
        links = ", ".join(source_link(config, root, s) for s in point["sources"])
        lines.append(f"| {point['start']} | {point['end']} | {display_value(plugin, point['record'])} | {links} |")
    if data["conflicts"]:
        lines += ["", "## Conflicting reports", "", "These periods are excluded until explicitly resolved.", ""]
        for group in data["conflicts"]:
            for candidate in group["candidates"]:
                lines.append(f"- {candidate['start']} to {candidate['end']}: "
                             f"{display_value(plugin, candidate['record'])}, "
                             f"result {candidate['source']['id']}, {source_link(config, root, candidate['source'])}")
    if data["issues"]:
        lines += ["", "## Extraction issues", ""]
        for issue in data["issues"]:
            lines.append(f"- `{escaped(issue['archive_id'])}`: {escaped(issue['outcome'])}; "
                         f"{escaped(issue['error'] or '')}")
    return "\n".join(lines) + "\n"


def cleanup_generations(root: Path, plugin: ProcessorInstance, keep: set[str]) -> None:
    for directory in root.iterdir():
        if directory.name in keep or not re.fullmatch(r"generation-[a-f0-9]{32}", directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            manifest = json.loads(safe_path(directory, "manifest.json").read_text())
            names = {p.name for p in directory.iterdir()}
            if (manifest.get("owner") != OWNER or manifest.get("instance_id") != plugin.id or
                    names - {"history.csv", "history.png", "manifest.json"}):
                continue
            for name in names:
                safe_path(directory, name).unlink()
            directory.rmdir()
        except (OSError, ValueError):
            continue


def render_report(config: Config, state: State, plugin: ProcessorInstance) -> dict:
    try:
        data = dataset(config, state, plugin)
        root = report_root(config, plugin)
        owned_root(root, plugin, create=True)
        index = safe_path(root, "Index.md")
        previous = None
        if index.exists():
            if not index.read_text().startswith("<!-- mailkeep:processor-report "):
                raise ValueError("refusing to overwrite an unowned report index")
            try:
                manifest, _ = read_report(root, plugin)
                previous = manifest["generation"]
                if manifest["data_hash"] == data["fingerprint"]:
                    with state.transaction() as conn:
                        conn.execute("INSERT OR REPLACE INTO processor_reports VALUES (?,?,?,?,?,NULL)",
                                     (plugin.id, previous, data["fingerprint"], canonical(manifest), manifest["published_at"]))
                    return {"instance_id": plugin.id, "outcome": "report_unchanged",
                            "conflicts": len(data["conflicts"]), "issues": len(data["issues"]), "path": str(index)}
            except (OSError, ValueError, KeyError):
                # An owned damaged generation can be replaced from stored records.
                pass
        generation = "generation-" + uuid.uuid4().hex
        directory = safe_path(root, generation)
        staging = safe_path(root, ".staging-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        published = utcnow()
        try:
            (staging / "history.csv").write_text(history_csv(plugin, data), encoding="utf-8", newline="")
            if data["accepted"]:
                plot_history(config, plugin, data, staging / "history.png")
            content = index_content(config, plugin, data, root, generation, published)
            manifest = {"owner": OWNER, "instance_id": plugin.id, "generation": generation,
                        "renderer_version": RENDERER_VERSION, "data_hash": data["fingerprint"],
                        "published_at": published,
                        "index_sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "files": {p.name: sha(p) for p in staging.iterdir()}}
            (staging / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
            staging.rename(directory)
            temporary = safe_path(root, ".index-" + uuid.uuid4().hex)
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(index)
            finally:
                temporary.unlink(missing_ok=True)
            with state.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO processor_reports VALUES (?,?,?,?,?,NULL)",
                             (plugin.id, generation, data["fingerprint"], canonical(manifest), published))
            cleanup_generations(root, plugin, {generation, previous} - {None})
        finally:
            if staging.exists():
                for path in staging.iterdir():
                    path.unlink()
                staging.rmdir()
        return {"instance_id": plugin.id, "outcome": "report_written",
                "conflicts": len(data["conflicts"]), "issues": len(data["issues"]), "path": str(index)}
    except Exception as exc:
        with state.transaction() as conn:
            conn.execute("INSERT INTO processor_reports VALUES (?, '', '', '{}', ?, ?) "
                         "ON CONFLICT(instance_id) DO UPDATE SET error=excluded.error",
                         (plugin.id, utcnow(), f"{type(exc).__name__}: {str(exc)[:500]}"))
        return {"instance_id": plugin.id, "outcome": "failed", "stage": "render",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def render_reports(config: Config, name: str, *, execute: bool) -> list[dict]:
    plugin = instance(config, name)
    if not config.paths.database.exists():
        raise ValueError("database does not exist")
    if not execute:
        return [{"instance_id": name, "action": "render", "path": str(report_root(config, plugin))}]
    with data_lock(config.paths.archive_root):
        state = State(config.paths.database)
        state.initialize()
        return [render_report(config, state, plugin)]


def status(config: Config, name: str | None = None) -> list[dict]:
    plugins = [instance(config, name)] if name else list(config.processors.instances)
    state = State(config.paths.database, readonly=True)
    ready = config.paths.database.exists() and has_schema(state)
    result = []
    for plugin in plugins:
        info = {"instance_id": plugin.id, "implementation": plugin.implementation,
                "enabled": config.processors.enabled, "definition": plugin.plugin_definition().model_dump(),
                "options_hash": plugin.options_hash(), "schema_ready": ready}
        if ready:
            with state.connect() as conn:
                info["attempt_counts"] = dict(conn.execute(
                    "SELECT outcome,COUNT(*) FROM processor_attempts WHERE instance_id=? GROUP BY outcome", (plugin.id,)).fetchall())
                last = conn.execute("SELECT finished_at FROM processor_attempts WHERE instance_id=? AND result_id IS NOT NULL "
                                    "ORDER BY id DESC LIMIT 1", (plugin.id,)).fetchone()
                report = conn.execute("SELECT * FROM processor_reports WHERE instance_id=?", (plugin.id,)).fetchone()
            info["last_success_at"] = last[0] if last else None
            info["report"] = dict(report) if report else None
            data = dataset(config, state, plugin)
            info.update(accepted_periods=len(data["accepted"]), conflicts=len(data["conflicts"]),
                        historical_observations=len(data["historical_rows"]),
                        historical_warnings=data["historical_warnings"],
                        stale_results=data["stale_count"], issues=data["issues"],
                        report_stale=not report or report["data_hash"] != data["fingerprint"] or bool(report["error"]))
        result.append(info)
    return result


def verify_processors(config: Config, name: str | None = None) -> dict:
    errors, warnings = [], []
    state = State(config.paths.database, readonly=True)
    if not config.paths.database.exists() or not has_schema(state):
        return {"ok": False, "errors": ["processor database schema is not initialized"], "warnings": []}
    plugins = [instance(config, name)] if name else list(config.processors.instances)
    with state.connect() as conn:
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("database has foreign-key violations")
        rows = conn.execute("SELECT r.*,a.object_relpath,a.raw_sha256 AS archive_raw_sha256 "
                            "FROM processor_results r JOIN archives a USING(archive_id)" +
                            (" WHERE r.instance_id=?" if name else ""), (name,) if name else ()).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                if digest(payload) != row["result_hash"] or row["raw_sha256"] != row["archive_raw_sha256"]:
                    raise ValueError("result hash mismatch")
                raw = raw_message(config, row)
                validate_result(payload, Definition.model_validate_json(row["definition_json"]), message_input(raw, row))
            except Exception as exc:
                errors.append(f"result {row['id']}: {type(exc).__name__}: {exc}")
        for plugin in plugins:
            try:
                data = dataset(config, state, plugin)
                warnings.extend(f"{plugin.id}: {warning}" for warning in data["historical_warnings"])
                warnings.extend(f"{plugin.id}: {i['outcome']}: {i['error']}" for i in data["issues"])
                if data["stale_count"]:
                    warnings.append(f"{plugin.id}: {data['stale_count']} stale previous results")
                if data["conflicts"]:
                    warnings.append(f"{plugin.id}: {len(data['conflicts'])} unresolved conflicts")
                row = conn.execute("SELECT * FROM processor_reports WHERE instance_id=?", (plugin.id,)).fetchone()
                if not row and not data["results"] and not data["issues"] and not data["historical_rows"]:
                    continue
                manifest, _ = read_report(report_root(config, plugin), plugin)
                if (not row or row["error"] or row["generation"] != manifest["generation"] or
                        row["data_hash"] != data["fingerprint"] or manifest["data_hash"] != data["fingerprint"] or
                        json.loads(row["manifest_json"]) != manifest):
                    raise ValueError("report is stale or publication needs reconciliation")
            except Exception as exc:
                errors.append(f"{plugin.id}: {type(exc).__name__}: {exc}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checked": len(rows)}


def list_plugins(config: Config) -> dict:
    return {"available": {k: v.model_dump() for k, v in registry().items()},
            "configured": [i.id for i in config.processors.instances], "enabled": config.processors.enabled}


def has_failures(items: list[dict]) -> bool:
    return any(i.get("outcome") in ("failed", "unsupported") or i.get("conflicts", 0) or i.get("issues", 0)
               for i in items)
