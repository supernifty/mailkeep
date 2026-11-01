"""Optional historical source note in Obsidian; never creates database records."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

START = "<!-- mailkeep:historical-rescuetime:start -->"
END = "<!-- mailkeep:historical-rescuetime:end -->"


def supplied_date(text: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3}|\d{1,2})-(\d{2}|\d{4})", text)
    if not match:
        raise ValueError("invalid historical supplied date")
    day, month, year = match.groups()
    names = {m.lower(): i for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
    return date(int(year) + (2000 if len(year) == 2 else 0),
                int(month) if month.isdigit() else names.get(month.lower(), 0), int(day))


def normalized_end(text: str) -> date:
    original = supplied_date(text)
    if original.weekday() not in (5, 6):
        raise ValueError("historical supplied dates must be Saturday or Sunday")
    return original + timedelta(days=original.weekday() == 5)


def parse_note(content: str, timezone: str) -> list[dict]:
    if content.count(START) != 1 or content.count(END) != 1:
        raise ValueError("historical source note must have exactly one marked table")
    before, after = content.index(START), content.index(END)
    if before >= after:
        raise ValueError("invalid historical source markers")
    lines = [line.strip() for line in content[before + len(START):after].splitlines() if line.strip()]
    if len(lines) < 3 or lines[0] != "| Supplied date | Week end (exclusive) | Distracting hours |" or not re.fullmatch(
            r"\|\s*-+\s*\|\s*-+\s*\|\s*-+\s*\|", lines[1]):
        raise ValueError("invalid historical source table header")
    result, seen = [], set()
    zone = ZoneInfo(timezone)
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != 5 or cells[0] or cells[-1]:
            raise ValueError("invalid historical source table row")
        original, ending_text, hours_text = cells[1:4]
        ending = normalized_end(original)
        if ending.isoformat() != ending_text or ending_text in seen:
            raise ValueError("duplicate or incorrectly normalised historical date")
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,4})?", hours_text):
            raise ValueError("historical hours must be a finite unsigned decimal")
        hours = Decimal(hours_text)
        seconds = hours * 3600
        precision = Decimal(10) ** hours.as_tuple().exponent * 3600
        start = ending - timedelta(days=7)
        maximum = (datetime.combine(ending, time(), zone).timestamp() -
                   datetime.combine(start, time(), zone).timestamp())
        if (seconds > maximum or seconds != seconds.to_integral_value() or
                precision < 1 or precision != precision.to_integral_value()):
            raise ValueError("invalid historical duration or precision")
        seen.add(ending_text)
        result.append({"original_date": original, "week_ending": ending_text,
                       "period_start": start.isoformat(), "hours": hours_text,
                       "seconds": int(seconds), "precision_seconds": int(precision)})
    return sorted(result, key=lambda row: row["week_ending"])


def include_history(config, plugin, data: dict) -> dict:
    from .engine import safe_path
    data.update(historical_rows=[], historical_warnings=[], historical_hash=None)
    if plugin.implementation != "rescuetime-weekly":
        return data
    source = safe_path(config.notes_dir, f"Reports/{plugin.id}/Historical data.md")
    if not source.exists():
        return data
    raw = source.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("historical source note exceeds 1 MiB")
    rows = parse_note(raw.decode("utf-8"), plugin.options["report_timezone"])
    data["historical_hash"] = hashlib.sha256(raw).hexdigest()
    data["historical_rows"] = rows
    points = {p["end"]: p for p in data["accepted"]}
    blocked = {c["end"] for group in data["conflicts"] for c in group["candidates"]}
    for index, row in enumerate(rows, 1):
        ending = row["week_ending"]
        source = {"id": f"historical-{index}", "kind": "historical",
                  "original_date": row["original_date"], "hours": row["hours"]}
        if ending in blocked:
            data["historical_warnings"].append(f"{ending}: historical value excluded while email reports conflict")
            continue
        email = points.get(ending)
        if email:
            step = row["precision_seconds"]
            if ((email["value"] * 2 + step) // (2 * step)) * step != row["seconds"]:
                data["historical_warnings"].append(f"{ending}: preferred email value differs from historical {row['hours']} h")
            email["sources"].append(source)
            continue
        record = {"period_start": row["period_start"], "period_end_exclusive": ending,
                  "report_timezone": plugin.options["report_timezone"],
                  "reported_period": row["original_date"], "metric": "total_distracting_time",
                  "distracting_seconds": row["seconds"], "precision_seconds": row["precision_seconds"],
                  "reported_label": "Historical distracting hours", "reported_value": row["hours"] + " h",
                  "historical_hours": row["hours"]}
        points[ending] = {"start": row["period_start"], "end": ending, "value": row["seconds"],
                          "record": record, "source": source, "sources": [source]}
    data["accepted"] = sorted(points.values(), key=lambda point: point["start"])
    return data
