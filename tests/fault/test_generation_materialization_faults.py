from __future__ import annotations

from pathlib import Path

import pytest

from omr_grader.infrastructure.generation_materializer import StagingToken, _validate_xlsx


def test_materializer_rejects_non_bytes_recognition_artifact(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()

    with pytest.raises(TypeError, match="pinned bytes"):
        StagingToken(root).write_bytes("recognition/page.png", b"".decode())


def test_materializer_cannot_escape_staging_with_nested_traversal(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()

    with pytest.raises(ValueError, match="escapes staging"):
        StagingToken(root).write_json("artifacts/../../CURRENT.json", {"revision": 2})
    assert not (tmp_path / "CURRENT.json").exists()


def test_materializer_rejects_structurally_invalid_workbook_before_publication(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "artifacts" / "score.xlsx"
    workbook.parent.mkdir()
    workbook.write_bytes(b"not an xlsx")

    with pytest.raises(ValueError, match="XLSX structural validation failed"):
        _validate_xlsx(workbook)
