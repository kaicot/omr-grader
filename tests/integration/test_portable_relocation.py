from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "tools" / "smoke-portable-onedir.py"


@pytest.mark.packaged
@pytest.mark.skipif(os.name != "nt", reason="Portable EXE smoke test requires Windows")
def test_relocated_portable_release_is_writable_and_read_only_safe() -> None:
    release = os.environ.get("OMR_GRADER_RELEASE_DIR")
    if release is None:
        pytest.skip("Set OMR_GRADER_RELEASE_DIR to a built portable release directory")
    release_dir = Path(release)
    if (release_dir / "OMR Grader.exe").is_file():
        application_dir = release_dir
    else:
        application_dir = release_dir / "OMR Grader"
    assert (application_dir / "OMR Grader.exe").is_file()
    for arguments in ((), ("--read-only",)):
        subprocess.run(
            [sys.executable, str(SMOKE), "--release", str(application_dir), *arguments],
            check=True,
        )
