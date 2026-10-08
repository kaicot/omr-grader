from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from omr_grader.domain.errors import Ok
from omr_grader.infrastructure.capabilities import probe_root_capability
from omr_grader.infrastructure.paths import ManagedPaths, is_path_writable


def _current_user_sid() -> str:
    output = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], check=True, capture_output=True
    ).stdout.decode("mbcs", errors="replace")
    match = re.search(r"S-1-[0-9-]+", output)
    assert match is not None, output
    return match.group(0)


@pytest.mark.skipif(os.name != "nt", reason="Windows effective-token ACL regression")
def test_existing_config_acl_denial_forces_read_only_without_a_probe_file(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    root.mkdir()
    for name in ("Profiles", "Data", "logs"):
        (root / name).mkdir()
    config = root / "config.json"
    config.write_text("{}", encoding="utf-8")
    # Use the SID, not the account name: `whoami` prints the name in the console code page
    # (cp949 on Korean Windows), and icacls cannot map a mis-decoded name back to an account.
    identity = _current_user_sid()
    try:
        subprocess.run(
            ["icacls", str(config), "/deny", f"*{identity}:(W)"],
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
            ["icacls", str(config), "/remove:d", f"*{identity}"],
            check=False,
            capture_output=True,
            text=True,
        )
    assert config.read_text(encoding="utf-8") == "{}"
