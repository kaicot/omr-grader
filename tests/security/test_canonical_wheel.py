from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
from validate_canonical_wheel import (  # noqa: E402
    WheelValidationError,
    normalize_wheel,
    validate_wheel,
)

EPOCH = 315532800


def make_wheel(tmp_path: Path) -> Path:
    wheel = tmp_path / "omr_grader-0.1.0-py3-none-any.whl"
    members = {
        "omr_grader/__init__.py": b"",
        "omr_grader-0.1.0.dist-info/METADATA": b"Name: omr-grader\nVersion: 0.1.0\n",
        "omr_grader-0.1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "omr_grader-0.1.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nomr-grader=omr_grader.bootstrap:main\n"
        ),
        "omr_grader-0.1.0.dist-info/RECORD": b"placeholder\n",
    }
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    normalize_wheel(wheel, EPOCH)
    return wheel


def rewrite(wheel: Path, mutate) -> None:
    with zipfile.ZipFile(wheel) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, content in mutate(entries):
            archive.writestr(info, content)


def test_accepts_deterministic_valid_fixture(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path)
    assert validate_wheel(wheel, EPOCH, "0.1.0")


def test_rejects_timestamp_and_member_order_mismatches(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path)
    rewrite(wheel, lambda entries: list(reversed(entries)))
    with pytest.raises(WheelValidationError, match="order"):
        validate_wheel(wheel, EPOCH)

    wheel = make_wheel(tmp_path)

    def bad_timestamp(entries):
        entries[0][0].date_time = (1981, 1, 1, 0, 0, 0)
        return entries

    rewrite(wheel, bad_timestamp)
    with pytest.raises(WheelValidationError, match="timestamp"):
        validate_wheel(wheel, EPOCH)


def test_rejects_record_and_payload_tampering(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path)

    def tamper(entries):
        return [
            (info, b"altered" if info.filename.endswith("__init__.py") else content)
            for info, content in entries
        ]

    rewrite(wheel, tamper)
    with pytest.raises(WheelValidationError, match="RECORD mismatch"):
        validate_wheel(wheel, EPOCH)


@pytest.mark.parametrize(
    "bad_name", ["../escape.py", "omr_grader/../escape.py", "omr_grader/evil.txt"]
)
def test_rejects_unsafe_or_unexpected_paths(tmp_path: Path, bad_name: str) -> None:
    wheel = tmp_path / "omr_grader-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(bad_name, b"bad")
    with pytest.raises(WheelValidationError):
        normalize_wheel(wheel, EPOCH)


def test_rejects_casefold_aliases(tmp_path: Path) -> None:
    wheel = tmp_path / "omr_grader-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("omr_grader/A.py", b"")
        archive.writestr("omr_grader/a.py", b"")
    with pytest.raises(WheelValidationError, match="alias"):
        normalize_wheel(wheel, EPOCH)
