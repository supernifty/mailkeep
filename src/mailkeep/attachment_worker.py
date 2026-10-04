from __future__ import annotations

import json
import math
import resource
import socket
import sys
from pathlib import Path
from typing import Any


def _disable_network() -> None:
    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("network access is disabled in attachment worker")

    socket.socket = denied  # type: ignore[assignment]
    socket.create_connection = denied  # type: ignore[assignment]


def _set_limits(timeout: float, max_output: int) -> None:
    limits = (
        (getattr(resource, "RLIMIT_CPU", None), (max(1, math.ceil(timeout)),
                                                  max(2, math.ceil(timeout) + 1))),
        (getattr(resource, "RLIMIT_AS", None), (1024 * 1024 * 1024,) * 2),
        (getattr(resource, "RLIMIT_FSIZE", None), (max_output * 2,) * 2),
        (getattr(resource, "RLIMIT_NOFILE", None), (32, 32)),
    )
    for name, value in limits:
        if name is None:
            continue
        try:
            resource.setrlimit(name, value)
        except (OSError, ValueError):
            pass


def _bounded(text: str, maximum: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text, False
    return encoded[:maximum].decode("utf-8", "ignore"), True


def _plain(path: Path, charset: str | None, maximum: int) -> dict[str, Any]:
    raw = path.read_bytes()
    encoding = charset or "utf-8"
    try:
        text = raw.decode(encoding, "strict")
    except LookupError as exc:
        return {"outcome": "malformed", "error": f"unknown charset {encoding}: {exc}"}
    except UnicodeDecodeError as exc:
        return {"outcome": "malformed", "error": f"strict {encoding} decode failed: {exc}"}
    text, truncated = _bounded(text.replace("\r\n", "\n").replace("\r", "\n"), maximum)
    return {
        "outcome": "success", "text": text, "page_count": None,
        "page_boundaries": [], "warnings": ["output_truncated"] if truncated else [],
    }


def _pdf(path: Path, max_pages: int, maximum: int) -> dict[str, Any]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            return {"outcome": "encrypted", "error": "PDF is encrypted"}
        page_count = len(reader.pages)
        if page_count > max_pages:
            return {"outcome": "oversized", "error":
                    f"PDF has {page_count} pages; limit is {max_pages}",
                    "page_count": page_count}
        chunks: list[str] = []
        boundaries: list[dict[str, int]] = []
        warnings: list[str] = []
        any_images = False
        any_text = False
        for number, page in enumerate(reader.pages, start=1):
            try:
                any_images = any_images or bool(page.images)
            except Exception:
                pass
            extracted = page.extract_text(
                extraction_mode="layout",
                layout_mode_space_vertically=False,
                layout_mode_strip_rotated=False,
            ) or ""
            any_text = any_text or bool(extracted.strip())
            heading = f"# Page {number}\n\n"
            start = sum(len(chunk) for chunk in chunks)
            chunk = heading + extracted.strip() + "\n"
            candidate = "\n".join(chunks + [chunk])
            bounded, truncated = _bounded(candidate, maximum)
            if truncated:
                warnings.append("output_truncated")
                if len(bounded) > start:
                    chunks = [bounded]
                    boundaries.append({"page": number, "start": start,
                                       "end": len(bounded)})
                break
            chunks.append(chunk)
            boundaries.append({"page": number, "start": start,
                               "end": start + len(chunk)})
        text = "\n".join(chunks).strip() + ("\n" if chunks else "")
        if not any_text:
            return {"outcome": "image_only" if any_images else "blank",
                    "error": "PDF contains no extractable text", "page_count": page_count,
                    "page_boundaries": [], "warnings": []}
        return {"outcome": "success", "text": text, "page_count": page_count,
                "page_boundaries": boundaries, "warnings": warnings}
    except Exception as exc:
        return {"outcome": "malformed", "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        _disable_network()
        _set_limits(float(request["timeout_seconds"]), int(request["max_output_bytes"]))
        path = Path(request["path"])
        if request["kind"] == "pdf":
            result = _pdf(path, int(request["max_pdf_pages"]),
                          int(request["max_output_bytes"]))
        elif request["kind"] == "text":
            result = _plain(path, request.get("charset"), int(request["max_output_bytes"]))
        else:
            result = {"outcome": "unsupported", "error": "unsupported worker kind"}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
