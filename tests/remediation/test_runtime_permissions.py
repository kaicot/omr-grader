from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omr_grader.domain.errors import Ok
from omr_grader.infrastructure.capabilities import probe_root_capability
from omr_grader.infrastructure.paths import ManagedPaths, is_path_writable


@pytest.mark.skipif(os.name != "nt", reason="Windows effective-token ACL regression")
def test_existing_config_acl_denial_forces_read_only_without_a_probe_file(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    root.mkdir()
    for name in ("Profiles", "Data", "logs"):
        (root / name).mkdir()
    config = root / "config.json"
    config.write_text("{}", encoding="utf-8")
    identity = subprocess.run(
        ["whoami"], check=True, capture_output=True
    ).stdout.decode("utf-8", errors="replace").strip()
    try:
        subprocess.run(
            ["icacls", str(config), "/deny", f"{identity}:(W)"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert not is_path_writable(config)
        capability = probe_root_capability(ManagedPaths.from_root(root))
        assert isinstance(capability, Ok)
        assert not capability.value.write_enabled
        assert capability.value.token is None
        assert not tuple(root.glob("*.probe"))
    finally:
        subprocess.run(
            ["icacls", str(config), "/remove:d", identity],
            check=False,
            capture_output=True,
            text=True,
        )
    assert config.read_text(encoding="utf-8") == "{}"
