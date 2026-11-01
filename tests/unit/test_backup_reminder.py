"""The backup reminder and the free-space helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import omr_grader.infrastructure.disk_space as disk_space
from omr_grader.infrastructure.backup_prefs import BackupPreferences, reminder_text
from omr_grader.infrastructure.disk_space import (
    LOW_SPACE_BYTES,
    PAGE_BYTES,
    scan_needs,
    shortfall,
    size_text,
)

NOW = datetime(2026, 11, 1, tzinfo=UTC)
OLD_EXAM = ("session-a", 3, "260901_101500_중간고사")
NEW_EXAM = ("session-b", 1, "261030_090000_퀴즈")


def test_a_reminder_needs_both_an_old_backup_and_a_changed_exam() -> None:
    never = BackupPreferences()
    assert reminder_text(never, [OLD_EXAM], NOW) == "전체 백업을 아직 한 번도 하지 않았습니다."
    # Only recent exams and never a backup: not yet.
    assert reminder_text(never, [NEW_EXAM], NOW) is None
    assert reminder_text(never, [], NOW) is None

    stale = BackupPreferences(True, "2026-09-20T00:00:00Z", {"session-a": 3})
    assert reminder_text(stale, [OLD_EXAM], NOW) is None  # nothing changed since
    assert reminder_text(stale, [OLD_EXAM, NEW_EXAM], NOW) == "마지막 전체 백업 후 42일이 지났습니다."
    recent = BackupPreferences(True, "2026-10-20T00:00:00Z", {})
    assert reminder_text(recent, [OLD_EXAM, NEW_EXAM], NOW) is None
    off = BackupPreferences(False, None, {})
    assert reminder_text(off, [OLD_EXAM], NOW) is None


def test_the_preferences_survive_a_round_trip_and_damage() -> None:
    prefs = BackupPreferences(False, "2026-10-20T00:00:00Z", {"session-a": 3})

    assert BackupPreferences.from_json(prefs.to_json()) == prefs
    assert BackupPreferences.from_json(b"{broken") == BackupPreferences()
    assert BackupPreferences.from_json(
        b'{"enabled": "yes", "last_full_backup_at": "soon", "revisions": {"a": "1", "b": 2}}'
    ) == BackupPreferences(True, None, {"b": 2})


def test_scan_space_counts_the_sources_and_every_page(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"x" * 1000)

    assert scan_needs([str(source), str(tmp_path / "missing.pdf")], 10) == (
        1000 + 10 * PAGE_BYTES + disk_space.MARGIN_BYTES
    )


def test_shortfall_reads_in_gigabytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk_space, "free_bytes", lambda path: 800 * 1024 * 1024)

    assert shortfall(tmp_path, 512 * 1024 * 1024) is None
    assert shortfall(tmp_path, int(1.2 * 1024**3)) == "필요한 공간 약 1.2 GB / 남은 공간 800 MB"
    assert size_text(LOW_SPACE_BYTES) == "1.0 GB"
