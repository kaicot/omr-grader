from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr_grader.domain.enums import OperationKind
from omr_grader.infrastructure.generation_materializer import StagingToken, _read_combined
from omr_grader.infrastructure.session_store import _preserved_artifact


def test_store_owned_staging_token_rejects_traversal_operation_paths(tmp_path: Path) -> None:
    token = StagingToken(tmp_path / "staging")
    token._root.mkdir()

    with pytest.raises(ValueError, match="escapes staging"):
        token.path("../published")


def test_store_owned_staging_token_writes_only_beneath_its_staging_tree(tmp_path: Path) -> None:
    token = StagingToken(tmp_path / "staging")
    token._root.mkdir()
    token.write_json("sidecars/provenance.json", {"schema_version": 1})

    assert json.loads((token._root / "sidecars" / "provenance.json").read_text()) == {
        "schema_version": 1
    }
    assert not (tmp_path / "sidecars" / "provenance.json").exists()


def test_malformed_parent_semantic_inputs_fail_closed(tmp_path: Path) -> None:
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / "semantic_inputs.json").write_text('{"combined":[]}', encoding="utf-8")

    with pytest.raises(ValueError, match="committed semantic inputs are corrupt"):
        _read_combined(generation)


@pytest.mark.parametrize(
    "path, preserved",
    (
        ("recognition/page-1.json", True),
        ("evidence/page-1.png", True),
        ("images/page-1.png", True),
        ("correction_history.json", True),
        ("artifacts/02_score_old.xlsx", False),
        ("artifacts/03_final_old.xlsx", False),
        ("corrected_overlay.json", False),
        ("detail_index.json", False),
        ("detail/work-item-1.json", False),
        ("semantic_inputs.json", False),
    ),
)
def test_lifecycle_materialization_copies_only_immutable_or_audit_artifacts(
    path: str, preserved: bool
) -> None:
    assert _preserved_artifact(path, OperationKind.CORRECT) is preserved
