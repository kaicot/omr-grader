"""Ask GitHub whether a newer release exists.

Only the product version goes out (in the User-Agent); nothing about exams or students. Any
network or format problem is reported as ``Err`` so the caller can stay silent when offline.
The endpoint is one constant so it can move if the source repository ever becomes private.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

LATEST_RELEASE_URL = "https://api.github.com/repos/kaicot/omr-grader/releases/latest"
RELEASES_PAGE_URL = "https://github.com/kaicot/omr-grader/releases/latest"
CHECK_INTERVAL = timedelta(hours=20)
UPDATE_PREFS_FILENAME = "update.json"
TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 512 * 1024
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_PAGE = re.compile(r"https://github\.com/kaicot/omr-grader/releases/[\w./-]+")


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """The newest published release."""

    version: str
    page_url: str
    published_at: str | None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(text.strip())
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def is_newer(candidate: str, current: str) -> bool:
    left, right = parse_version(candidate), parse_version(current)
    return left is not None and right is not None and left > right


def _failure(reason: str) -> Err:
    return Err((ErrorInfo("UPDATE_CHECK_FAILED", "error.update_check_failed", None, context={"reason": reason}),))


def parse_release(payload: Any) -> Result[ReleaseInfo]:
    """Read GitHub's latest-release JSON; drafts and pre-releases are never offered."""
    if not isinstance(payload, dict):
        return _failure("응답 형식이 올바르지 않습니다.")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return _failure("릴리즈 버전을 읽을 수 없습니다.")
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return _failure("정식 릴리즈가 아닙니다.")
    page = payload.get("html_url")
    # Only ever open this project's own release pages.
    if not isinstance(page, str) or _PAGE.fullmatch(page) is None:
        page = RELEASES_PAGE_URL
    published = payload.get("published_at")
    version = ".".join(str(part) for part in parse_version(tag) or ())
    return Ok(ReleaseInfo(version, page, published if isinstance(published, str) else None))


Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_latest_release(current_version: str, opener: Opener = _open) -> Result[ReleaseInfo]:
    """One HTTPS request to GitHub; ``Err`` when offline, slow, blocked or malformed."""
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"OMR-Grader/{current_version}",
        },
    )
    try:
        with opener(request, TIMEOUT_SECONDS) as response:
            data = response.read(_MAX_BYTES + 1)
    # HTTPException covers IncompleteRead/BadStatusLine from captive portals and proxies.
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return _failure("인터넷에 연결할 수 없거나 GitHub가 응답하지 않습니다.")
    if len(data) > _MAX_BYTES:
        return _failure("응답이 너무 큽니다.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _failure("응답 형식이 올바르지 않습니다.")
    return parse_release(payload)


@dataclass(frozen=True, slots=True)
class UpdatePreferences:
    """What the user chose about update checks, kept in ``update.json`` beside the program."""

    enabled: bool = True
    last_checked: str | None = None
    skipped_version: str | None = None

    def due(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.last_checked is None:
            return True
        try:
            checked = datetime.fromisoformat(self.last_checked.replace("Z", "+00:00"))
        except ValueError:
            return True
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        return now - checked >= CHECK_INTERVAL or checked > now

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "schema": 1,
                "enabled": self.enabled,
                "last_checked": self.last_checked,
                "skipped_version": self.skipped_version,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    @staticmethod
    def from_json(data: bytes) -> UpdatePreferences:
        """Lenient: a damaged file falls back to defaults instead of blocking start-up."""
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return UpdatePreferences()
        if not isinstance(value, dict):
            return UpdatePreferences()
        enabled = value.get("enabled")
        checked = value.get("last_checked")
        skipped = value.get("skipped_version")
        return UpdatePreferences(
            enabled if isinstance(enabled, bool) else True,
            checked if isinstance(checked, str) else None,
            skipped if isinstance(skipped, str) and parse_version(skipped) else None,
        )


def now_text(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def offer(release: ReleaseInfo, current_version: str, prefs: UpdatePreferences) -> bool:
    """Whether to show the banner: newer than us and not skipped by the user."""
    return is_newer(release.version, current_version) and release.version != prefs.skipped_version


__all__ = [
    "CHECK_INTERVAL",
    "LATEST_RELEASE_URL",
    "RELEASES_PAGE_URL",
    "UPDATE_PREFS_FILENAME",
    "ReleaseInfo",
    "UpdatePreferences",
    "fetch_latest_release",
    "is_newer",
    "now_text",
    "offer",
    "parse_release",
    "parse_version",
]
