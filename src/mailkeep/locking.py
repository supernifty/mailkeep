from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def data_lock(data_root: Path) -> Iterator[None]:
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / ".mailkeep.lock"
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another mutating Mailkeep command is running") from exc
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
