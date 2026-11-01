"""Example plugin: deterministic extraction from RescueTime weekly goal reports."""
from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .api import Definition, normalized


DEFINITION = Definition.model_validate({
    "id": "rescuetime-weekly", "processor_version": "1", "schema_version": "1",
    "options": {
        "report_timezone": {"type": "string"},
        "metric": {"type": "string", "required": False, "default": "total_distracting_time"},
        "layout": {"type": "string", "required": False, "default": "weekly-v1"},
    },
    "fields": {
        "period_start": {"type": "date"}, "period_end_exclusive": {"type": "date"},
        "reported_period": {"type": "string"}, "report_timezone": {"type": "string"},
        "metric": {"type": "string"}, "distracting_seconds": {"type": "integer"},
        "reported_label": {"type": "string"}, "reported_value": {"type": "string"},
        "precision_seconds": {"type": "integer"},
    },
    "view": {"start": "period_start", "end_exclusive": "period_end_exclusive",
             "value": "distracting_seconds", "unit": "hours", "scale": 3600,
             "label": "Distracting time", "metric": "total_distracting_time"},
})

_MONTHS = {name: i for i, name in enumerate(("January", "February", "March", "April", "May",
          "June", "July", "August", "September", "October", "November", "December"), 1)}
_MONTH = "(" + "|".join(_MONTHS) + ")"
_PERIOD = re.compile(_MONTH + r"\s+(\d{1,2}),\s+(\d{4})\s*[-–]\s*" +
                     _MONTH + r"\s+(\d{1,2}),\s+(\d{4})")
_DURATION = re.compile(r"(?:(\d+)h(?:\s+(\d+)m)?|(\d+)m)")
_GOAL = re.compile(r"(?:Less|More) than\s+.+?\s+on All Distracting Time$")


def duration(value: str) -> int:
    match = _DURATION.fullmatch(value)
    if not match:
        raise ValueError("unknown duration format")
    hours, minutes, minutes_only = match.groups()
    if hours is not None and minutes is not None and int(minutes) >= 60:
        raise ValueError("invalid minutes component")
    return int(hours or 0) * 3600 + int(minutes or minutes_only or 0) * 60


def visible_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(("head", "script", "style")):
        node.decompose()
    for node in soup.find_all(style=True):
        if node.attrs is not None and any(x in re.sub(r"\s+", "", node["style"]).lower()
                                          for x in ("display:none", "visibility:hidden")):
            node.decompose()
    # Lines are semantic fields, not individual inline spans (e.g. goal threshold).
    for node in soup.find_all(("h1", "h2", "h3", "h4", "p", "small", "br")):
        node.insert_before("\n")
        node.insert_after("\n")
    return soup.get_text(" ")


def extract(text: str, timezone: str) -> tuple[dict, dict]:
    periods = list(_PERIOD.finditer(text))
    if not periods:
        raise ValueError("missing explicit full-year reporting period")
    dates = set()
    for match in periods:
        a, b, c, d, e, f = match.groups()
        dates.add((date(int(c), _MONTHS[a], int(b)), date(int(f), _MONTHS[d], int(e))))
    if len(dates) != 1:
        raise ValueError("contradictory reporting periods")
    start, end = dates.pop()
    if (end - start).days != 7:
        raise ValueError("weekly boundaries must be seven days apart")
    lines = [normalized(line) for line in text.splitlines() if normalized(line)
             and not re.fullmatch(r"[-*]+", normalized(line))]
    if "YOUR GOALS FOR THE WEEK" not in lines:
        raise ValueError("missing weekly goals section")
    goal_start = lines.index("YOUR GOALS FOR THE WEEK")
    candidates = []
    for i, line in enumerate(lines[goal_start + 1:], goal_start + 1):
        if not _GOAL.fullmatch(line):
            continue
        following = lines[i + 1:i + 4]
        if len(following) < 3 or following[0] != "All day, every day":
            raise ValueError("distracting goal is not all day, every day")
        value = following[1]
        seconds = duration(value)
        if not re.fullmatch(r".+ avg per day", following[2]):
            raise ValueError("unknown distracting goal layout")
        candidates.append((seconds, value, line))
    if not candidates:
        raise ValueError("missing All Distracting Time goal")
    if len({v[0] for v in candidates}) != 1:
        raise ValueError("contradictory distracting goals")
    seconds, value, heading = candidates[0]
    tz = ZoneInfo(timezone)
    maximum = (datetime.combine(end, time(), tz).timestamp() -
               datetime.combine(start, time(), tz).timestamp())
    if seconds > maximum:
        raise ValueError("duration exceeds reporting period")
    record = {
        "period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
        "reported_period": periods[0].group(), "report_timezone": timezone,
        "metric": "total_distracting_time", "distracting_seconds": seconds,
        "reported_label": "All Distracting Time", "reported_value": value,
        "precision_seconds": 60,
    }
    return record, {"duration": value, "period": periods[0].group(), "goal": heading}


class RescueTime:
    definition = DEFINITION

    def process(self, message: dict, options: dict) -> dict:
        if options["metric"] != "total_distracting_time" or options["layout"] != "weekly-v1":
            return {"outcome": "unsupported", "reason": "unsupported metric/layout"}
        try:
            values = []
            evidence = []
            for part in message["parts"]:
                text = visible_html(part["text"]) if part["media_type"] == "text/html" else part["text"]
                if part["diagnostics"]:
                    raise ValueError("MIME decoding defects in report alternative")
                record, snippets = extract(text, options["report_timezone"])
                values.append(record)
                for field, snippet in snippets.items():
                    evidence.append({"part": part["ordinal"], "field": field, "text": snippet})
            if not values:
                raise ValueError("no supported body parts")
            # Whitespace variations in the literal period/value are not disagreements.
            comparable = [{k: normalized(v) if isinstance(v, str) else v for k, v in row.items()}
                          for row in values]
            if any(row != comparable[0] for row in comparable[1:]):
                raise ValueError("plain-text and HTML alternatives disagree")
            return {"outcome": "success", "records": [values[0]], "evidence": evidence}
        except (ValueError, KeyError) as exc:
            return {"outcome": "unsupported", "reason": str(exc)}


def create() -> RescueTime:
    return RescueTime()
