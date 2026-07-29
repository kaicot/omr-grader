from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
sys.path.insert(0, str(PACKAGING))
from verify_release import smoke_portable  # noqa: E402


@pytest.mark.packaged
@pytest.mark.skipif(os.name != "nt", reason="Portable EXE smoke test requires Windows")
def test_relocated_portable_release_is_writable_and_read_only_safe() -> None:
    release = os.environ.get("OMR_GRADER_RELEASE_DIR")
    if release is None:
        pytest.skip("Set OMR_GRADER_RELEASE_DIR to a built portable release directory")
    release_dir = Path(release)
    assert (release_dir / "OMR Grader.exe").is_file()
    smoke_portable(release_dir, read_only=False)
    smoke_portable(release_dir, read_only=True)
