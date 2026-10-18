"""The files the plugin keeps its state in, on the disk before they count: a file written or
grown in the cache only can come back full of zeros after a crash, and the plugin can't read it.
That stopped the event stream on 2026-10-17."""

import os
from pathlib import Path


def write_file(path: Path, data: str | bytes) -> None:
    """Whole or not at all: written beside it, then put in its place."""
    part = path.with_suffix(".part")
    with part.open("wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
        f.flush()
        os.fsync(f.fileno())
    part.replace(path)


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
