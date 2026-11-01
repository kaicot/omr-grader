"""``backup.json`` next to the program: when the last full backup ran, and the reminder.

The reminder appears when a full backup is more than 30 days old (or was never made and the
oldest exam is that old) and some exam changed since.  Deleting the file only brings back the
defaults: reminder on, no backup recorded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

BACKUP_PREFS_FILENAME = "backup.json"
REMINDER_DAYS = 30
_FOLDER_DATE = re.compile(r"(\d{6})_\d{6}_")


@dataclass(frozen=True, slots=True)
class BackupPreferences:
    enabled: bool = True
    last_full_backup_at: str | None = None
    # session_id -> revision at the last full backup, to tell whether anything changed since.
    revisions: Mapping[str, int] = field(default_factory=dict)

    def to_json(self) -> bytes:
        return (
            json.dumps(
                {
                    "schema": 1,
                    "enabled": self.enabled,
                    "last_full_backup_at": self.last_full_backup_at,
                    "revisions": dict(sorted(self.revisions.items())),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> BackupPreferences:
        """Lenient: anything unreadable falls back to the defaults for that field."""
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(value, dict):
            return cls()
        enabled = value.get("enabled")
        last = value.get("last_full_backup_at")
        revisions = value.get("revisions")
        return cls(
            enabled if isinstance(enabled, bool) else True,
            last if isinstance(last, str) and _parse(last) is not None else None,
            {
                str(key): item
                for key, item in (revisions.items() if isinstance(revisions, dict) else ())
                if type(item) is int
            },
        )


def now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _oldest_exam(folders: Sequence[str]) -> datetime | None:
    """The earliest date in exam folder names (<yymmdd>_<HHMMSS>_<exam>)."""
    dates = []
    for name in folders:
        match = _FOLDER_DATE.match(name)
        if match is None:
            continue
        try:
            dates.append(datetime.strptime(match.group(1), "%y%m%d").replace(tzinfo=UTC))
        except ValueError:
            continue
    return min(dates, default=None)


def reminder_text(
    prefs: BackupPreferences,
    exams: Sequence[tuple[str, int, str]],
    now: datetime,
) -> str | None:
    """The reminder for exams given as (session id, revision, folder name), or None."""
    if not prefs.enabled or not exams:
        return None
    if all(prefs.revisions.get(session_id) == revision for session_id, revision, _ in exams):
        return None
    limit = timedelta(days=REMINDER_DAYS)
    last = _parse(prefs.last_full_backup_at) if prefs.last_full_backup_at else None
    if last is not None:
        days = (now - last).days
        return f"마지막 전체 백업 후 {days}일이 지났습니다." if now - last >= limit else None
    oldest = _oldest_exam([folder for _, _, folder in exams])
    if oldest is not None and now - oldest >= limit:
        return "전체 백업을 아직 한 번도 하지 않았습니다."
    return None


__all__ = [
    "BACKUP_PREFS_FILENAME",
    "REMINDER_DAYS",
    "BackupPreferences",
    "now_text",
    "reminder_text",
]
