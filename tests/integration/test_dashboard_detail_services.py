from __future__ import annotations

import io
import json
from hashlib import sha256

from omr_grader.application.dto import SnapshotRef
from omr_grader.domain.errors import Ok
from omr_grader.domain.models import (
    ManifestFile,
    ManifestSummary,
    OperationKind,
    SessionManifest,
    SessionState,
)
from omr_grader.infrastructure.detail_repository import DetailRepository


class _Lease:
    def __init__(self) -> None:
        self.snapshot_ref = SnapshotRef("session", 1, "generation", "0" * 64)
        self.opens: list[str] = []
        self.closes = 0
        self._files = {
            "detail_index.json": json.dumps(
                {
                    "schema_version": 1,
                    "snapshot": {
                        "session_id": "session",
                        "revision": 1,
                        "generation_id": "generation",
                    },
                    "work_items": [
                        {
                            "work_item_id": "imported",
                            "detail_path": "details/imported.json",
                            "image_path": None,
                        },
                        {
                            "work_item_id": "scanned",
                            "detail_path": "details/scanned.json",
                            "image_path": "images/scanned.png",
                        },
                    ],
                }
            ).encode(),
            "details/imported.json": json.dumps(
                {
                    "schema_version": 1,
                    "snapshot": {
                        "session_id": "session",
                        "revision": 1,
                        "generation_id": "generation",
                    },
                    "work_item_id": "imported",
                    "payload": {"source": "import"},
                }
            ).encode(),
            "details/scanned.json": json.dumps(
                {
                    "schema_version": 1,
                    "snapshot": {
                        "session_id": "session",
                        "revision": 1,
                        "generation_id": "generation",
                    },
                    "work_item_id": "scanned",
                    "payload": {"source": "scan"},
                }
            ).encode(),
            "images/scanned.png": b"image",
        }
        self.manifest = SessionManifest(
            1,
            "session",
            1,
            "generation",
            None,
            None,
            None,
            "operation",
            OperationKind.RECOGNIZE,
            "2.1.1",
            "2026-09-06T00:00:00.000000Z",
            SessionState.RECOGNIZED,
            (),
            None,
            "0" * 64,
            "0" * 64,
            None,
            None,
            tuple(
                ManifestFile(
                    path,
                    len(payload),
                    sha256(payload).hexdigest(),
                    "application/json" if path.endswith(".json") else "image/png",
                )
                for path, payload in sorted(self._files.items(), key=lambda item: item[0].encode())
            ),
            ManifestSummary(2, 1, 0, None),
        )

    def open_allowlisted(self, path: str):
        self.opens.append(path)
        if path not in self._files:
            from omr_grader.domain.errors import Err, ErrorInfo

            return Err((ErrorInfo("MISSING", "error.missing"),))
        return Ok(io.BytesIO(self._files[path]))

    def close(self):
        self.closes += 1
        return Ok(None)


class _Coordinator:
    def __init__(self, lease: _Lease) -> None:
        self.lease = lease

    def open_committed_snapshot(self, request):
        return Ok(self.lease)


def test_detail_is_lazy_and_import_rows_have_no_image() -> None:
    lease = _Lease()
    repository = DetailRepository(_Coordinator(lease))

    opened = repository.open_detail("session")
    assert isinstance(opened, Ok)
    handle, rows = opened.value
    assert [row.work_item_id for row in rows] == ["imported", "scanned"]
    assert lease.opens == ["detail_index.json", "review_geometry.json"]

    imported = repository.load_work_item(handle.handle_id, "imported")
    assert isinstance(imported, Ok)
    assert imported.value.image is None
    assert lease.opens == [
        "detail_index.json",
        "review_geometry.json",
        "details/imported.json",
    ]

    scanned = repository.load_work_item(handle.handle_id, "scanned")
    assert isinstance(scanned, Ok)
    assert scanned.value.image == b"image"
    assert lease.opens[-2:] == ["details/scanned.json", "images/scanned.png"]
    assert isinstance(repository.close_detail(handle.handle_id), Ok)
    assert lease.closes == 1
