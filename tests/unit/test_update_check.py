"""Update check: version comparison, GitHub release parsing, failures, preferences."""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.update_check import (
    LATEST_RELEASE_URL,
    RELEASES_PAGE_URL,
    ReleaseInfo,
    UpdatePreferences,
    fetch_latest_release,
    is_newer,
    offer,
    parse_release,
    parse_version,
)


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("v4.2.1", "4.2.0", True),
        ("4.10.0", "4.9.9", True),
        ("v4.2.0", "4.2.0", False),
        ("v4.1.9", "4.2.0", False),
        ("v5", "4.2.0", False),
        ("latest", "4.2.0", False),
    ],
)
def test_versions_compare_numerically(candidate, current, newer):
    assert is_newer(candidate, current) is newer


def test_parse_version_accepts_tags_only():
    assert parse_version("v4.2.0") == (4, 2, 0)
    assert parse_version(" 4.2.0 ") == (4, 2, 0)
    assert parse_version("4.2.0-rc1") is None


def _release(**extra):
    payload = {
        "tag_name": "v4.3.0",
        "html_url": "https://github.com/kaicot/omr-grader/releases/tag/v4.3.0",
        "published_at": "2026-11-01T00:00:00Z",
        "draft": False,
        "prerelease": False,
    }
    payload.update(extra)
    return payload


def test_a_release_is_read_and_only_our_pages_are_opened():
    parsed = parse_release(_release())
    assert parsed == Ok(
        ReleaseInfo(
            "4.3.0", "https://github.com/kaicot/omr-grader/releases/tag/v4.3.0", "2026-11-01T00:00:00Z"
        )
    )
    foreign = parse_release(_release(html_url="https://example.com/evil"))
    assert isinstance(foreign, Ok) and foreign.value.page_url == RELEASES_PAGE_URL


@pytest.mark.parametrize(
    "payload",
    [[], _release(tag_name="nightly"), _release(draft=True), _release(prerelease=True)],
)
def test_odd_releases_are_not_offered(payload):
    assert isinstance(parse_release(payload), Err)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_request_carries_only_the_version():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return _Response(json.dumps(_release()).encode())

    result = fetch_latest_release("4.2.0", opener)

    assert isinstance(result, Ok) and result.value.version == "4.3.0"
    assert seen["url"] == LATEST_RELEASE_URL
    assert seen["headers"]["User-agent"] == "OMR-Grader/4.2.0"
    assert seen["timeout"] <= 5


@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("offline"), TimeoutError("slow"), OSError("blocked")],
)
def test_offline_and_slow_networks_fail_quietly(failure):
    def opener(request, timeout):
        raise failure

    result = fetch_latest_release("4.2.0", opener)

    assert isinstance(result, Err) and result.errors[0].code == "UPDATE_CHECK_FAILED"


class _BrokenBody(_Response):
    def __init__(self, failure: Exception) -> None:
        super().__init__(b"")
        self._failure = failure

    def read(self, size=-1):
        raise self._failure


@pytest.mark.parametrize(
    "failure",
    [
        http.client.IncompleteRead(b"{", 10),
        http.client.BadStatusLine("<html>"),
        http.client.RemoteDisconnected("closed"),
        http.client.HTTPException("odd"),
    ],
)
def test_http_protocol_errors_from_a_captive_portal_fail_quietly(failure):
    def raising_opener(request, timeout):
        raise failure

    def raising_body(request, timeout):
        return _BrokenBody(failure)

    for opener in (raising_opener, raising_body):
        result = fetch_latest_release("4.2.0", opener)
        assert isinstance(result, Err) and result.errors[0].code == "UPDATE_CHECK_FAILED"


def test_a_deeply_nested_answer_is_refused_not_raised():
    nested = b"[" * 100_000 + b"]" * 100_000

    assert isinstance(fetch_latest_release("4.2.0", lambda r, t: _Response(nested)), Err)


def test_garbage_and_huge_answers_are_refused():
    assert isinstance(fetch_latest_release("4.2.0", lambda r, t: _Response(b"<html>")), Err)
    big = b"{" + b" " * (600 * 1024) + b"}"
    assert isinstance(fetch_latest_release("4.2.0", lambda r, t: _Response(big)), Err)


def test_preferences_round_trip_and_tolerate_damage():
    prefs = UpdatePreferences(True, "2026-10-08T00:00:00Z", "4.3.0")
    assert UpdatePreferences.from_json(prefs.to_json()) == prefs
    assert UpdatePreferences.from_json(b"not json") == UpdatePreferences()
    assert UpdatePreferences.from_json(b'{"enabled": "yes", "skipped_version": "x"}') == (
        UpdatePreferences()
    )


def test_checks_are_at_most_daily_and_never_when_turned_off():
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    recent = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(days=2)).isoformat()
    assert UpdatePreferences().due(now)
    assert not UpdatePreferences(True, recent).due(now)
    assert UpdatePreferences(True, old).due(now)
    assert not UpdatePreferences(False, old).due(now)
    # A clock that went backwards does not silence checks forever.
    assert UpdatePreferences(True, (now + timedelta(days=3)).isoformat()).due(now)


def test_a_skipped_version_is_not_offered_again_but_a_newer_one_is():
    prefs = UpdatePreferences(skipped_version="4.3.0")
    page = RELEASES_PAGE_URL
    assert not offer(ReleaseInfo("4.3.0", page, None), "4.2.0", prefs)
    assert offer(ReleaseInfo("4.3.1", page, None), "4.2.0", prefs)
    assert not offer(ReleaseInfo("4.2.0", page, None), "4.2.0", UpdatePreferences())
