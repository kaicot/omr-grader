"""Free-space checks before work that writes a lot: scanning, a full backup, and a warning
when the program's own drive runs low."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path

# What one scanned page leaves behind: the normalized page, its scored overlay and the
# coordinates (about 1 MB + 1.4 MB measured in 4.0.2), rounded up generously.
PAGE_BYTES = 3 * 1024 * 1024
# Room kept free for everything else the program and Windows write meanwhile.
MARGIN_BYTES = 200 * 1024 * 1024
# Below this the exam list warns that the drive is nearly full.
LOW_SPACE_BYTES = 1024 * 1024 * 1024


def free_bytes(path: Path) -> int | None:
    """Free space on the drive of ``path`` (or its nearest existing parent); None if unknown."""
    candidate = Path(path)
    while not candidate.exists():
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return None


def size_text(value: int) -> str:
    """A size the way a teacher reads it: '0.8 GB' or '350 MB'."""
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GB"
    return f"{max(1, round(value / 1024**2))} MB"


def scan_needs(sources: Iterable[str], pages: int) -> int:
    """Bytes a scan of ``sources`` with ``pages`` pages may write, with room to spare."""
    total = 0
    for source in sources:
        try:
            total += os.path.getsize(source)
        except OSError:
            continue
    return total + pages * PAGE_BYTES + MARGIN_BYTES


def folder_bytes(folder: Path, *, skip: Iterable[str] = ()) -> int:
    """Total size of the files under ``folder``, leaving out the top-level names in ``skip``."""
    skipped = set(skip)
    total = 0
    if not folder.is_dir():
        return 0
    for entry in folder.iterdir():
        if entry.name in skipped or entry.is_symlink():
            continue
        if entry.is_file():
            total += entry.stat().st_size
        elif entry.is_dir():
            total += sum(
                item.stat().st_size for item in entry.rglob("*") if item.is_file() and not item.is_symlink()
            )
    return total


def shortfall(path: Path, needed: int) -> str | None:
    """A Korean explanation when the drive of ``path`` has less than ``needed`` free; else None."""
    free = free_bytes(path)
    if free is None or free >= needed:
        return None
    return f"필요한 공간 약 {size_text(needed)} / 남은 공간 {size_text(free)}"


def same_drive(left: Path, right: Path) -> bool:
    """Whether two paths sit on the same drive (a disk failure would take both)."""
    try:
        first = os.path.splitdrive(str(Path(left).resolve()))[0].lower()
        second = os.path.splitdrive(str(Path(right).resolve()))[0].lower()
    except OSError:
        return False
    return bool(first) and first == second


__all__ = [
    "LOW_SPACE_BYTES",
    "MARGIN_BYTES",
    "PAGE_BYTES",
    "folder_bytes",
    "free_bytes",
    "same_drive",
    "scan_needs",
    "shortfall",
    "size_text",
]
