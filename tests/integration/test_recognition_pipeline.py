from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import cv2
import numpy as np
import pytest

from omr_grader.domain.enums import SourceKind
from omr_grader.domain.models import PageRef
from omr_grader.domain.profile import parse_profile_bytes
from omr_grader.recognition.pipeline import (
    PipelineFailure,
    PipelineInput,
    PipelineSuccess,
    recognize_page,
)
from omr_grader.recognition.thresholds import (
    CALIBRATION_PROVENANCE,
    thresholds_for_sensitivity,
)


def _profile():
    regions = [
        {
            "name": "student_id",
            "type": "id",
            "bbox_ratio": {"x": 0.05, "y": 0.45, "w": 0.2, "h": 0.4},
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    for index in range(5):
        regions.append(
            {
                "name": f"answers_{index}",
                "type": "answer",
                "bbox_ratio": {"x": 0.3 + index * 0.13, "y": 0.1, "w": 0.1, "h": 0.8},
                "grid": {"cols": 5, "rows": 20},
            }
        )
    parsed = parse_profile_bytes(
        json.dumps(
            {
                "profile_name": "synthetic",
                "page": {
                    "orientation": "landscape",
                    "aspect_ratio": 1.5,
                    "source_width": 300,
                    "source_height": 200,
                },
                "regions": regions,
            }
        ).encode()
    )
    return parsed.value


def _page_ref() -> PageRef:
    return PageRef(
        1,
        "session",
        "item",
        SourceKind.IMAGE,
        "0" * 64,
        "scan.png",
        "scan.png",
        None,
        None,
        0,
        0,
        "scan",
    )


def _classic_tiff_chain(frame_count: int) -> bytes:
    ifd_size = 30
    offsets = [8 + index * ifd_size for index in range(frame_count)]
    payload = bytearray(b"II*\0" + offsets[0].to_bytes(4, "little"))
    for index, offset in enumerate(offsets):
        assert len(payload) == offset
        payload.extend((2).to_bytes(2, "little"))
        for tag, value in ((256, 5), (257, 3)):
            payload.extend(tag.to_bytes(2, "little"))
            payload.extend((4).to_bytes(2, "little"))
            payload.extend((1).to_bytes(4, "little"))
            payload.extend(value.to_bytes(4, "little"))
        payload.extend((offsets[index + 1] if index + 1 < frame_count else 0).to_bytes(4, "little"))
    return bytes(payload)


def _big_tiff(width: int, height: int) -> bytes:
    payload = bytearray(b"II+\0\x08\0\0\0" + (16).to_bytes(8, "little"))
    payload.extend((2).to_bytes(8, "little"))
    for tag, value in ((256, width), (257, height)):
        payload.extend(tag.to_bytes(2, "little"))
        payload.extend((4).to_bytes(2, "little"))
        payload.extend((1).to_bytes(8, "little"))
        payload.extend(value.to_bytes(4, "little") + b"\0" * 4)
    payload.extend(b"\0" * 8)
    return bytes(payload)


def _thresholds():
    return thresholds_for_sensitivity(
        50, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
    ).value


def _outlined_image() -> np.ndarray:
    image = np.full((240, 340, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (319, 219), (0, 0, 0), 3)
    return image


def _encoded(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def test_pipeline_rejects_tampered_frozen_profiles_before_decoding() -> None:
    profile = _profile()
    empty = replace(profile, regions=())
    invalid_region = replace(
        profile.regions[0],
        bbox_ratio=replace(profile.regions[0].bbox_ratio, w=Decimal("0")),
    )
    invalid = replace(profile, regions=(invalid_region, *profile.regions[1:]))

    for tampered in (empty, invalid):
        result = recognize_page(
            PipelineInput(_page_ref(), _encoded(_outlined_image()), tampered, _thresholds())
        )
        assert isinstance(result, PipelineFailure)
        assert result.failure.errors[0].code == "INVALID_FROZEN_PROFILE"


def test_pipeline_uses_profile_landmarks_for_bottom_heavy_zero_and_180_degree_pages() -> None:
    upright = _outlined_image()
    cv2.rectangle(upright, (35, 120), (75, 150), (0, 0, 0), -1)
    cv2.rectangle(upright, (22, 185), (317, 200), (0, 0, 0), -1)

    for image, expected_rotation in (
        (upright, 0),
        (cv2.rotate(upright, cv2.ROTATE_180), 180),
    ):
        result = recognize_page(
            PipelineInput(_page_ref(), _encoded(image), _profile(), _thresholds())
        )
        assert isinstance(result, PipelineSuccess)
        assert result.page.rotation_degrees == expected_rotation


def test_pipeline_routes_nondiscriminating_profile_landmarks_to_manual_review() -> None:
    profile = _profile()
    centered = tuple(
        replace(
            region,
            bbox_ratio=replace(
                region.bbox_ratio,
                x=Decimal("0.4"),
                y=Decimal("0.4"),
                w=Decimal("0.2"),
                h=Decimal("0.2"),
            ),
        )
        for region in profile.regions
    )
    result = recognize_page(
        PipelineInput(
            _page_ref(),
            _encoded(_outlined_image()),
            replace(profile, regions=centered),
            _thresholds(),
        )
    )

    assert isinstance(result, PipelineFailure)
    assert result.failure.errors[0].code == "ORIENTATION_UNCERTAIN"


def test_pipeline_tiff_worker_preflight_accepts_bigtiff_and_rejects_excess_frames() -> None:
    task = PipelineInput(_page_ref(), _big_tiff(17, 19), _profile(), _thresholds())
    assert task.encoded_raster.startswith(b"II+\0")

    with pytest.raises(ValueError, match="TIFF_FRAME_QUOTA"):
        PipelineInput(_page_ref(), _classic_tiff_chain(257), _profile(), _thresholds())


def test_pipeline_emits_canonical_unpublished_artifacts() -> None:
    image = np.full((240, 340, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (319, 219), (0, 0, 0), 3)
    cv2.rectangle(image, (35, 120), (75, 150), (0, 0, 0), -1)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    thresholds = thresholds_for_sensitivity(
        50, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
    ).value
    result = recognize_page(PipelineInput(_page_ref(), encoded.tobytes(), _profile(), thresholds))
    assert isinstance(result, PipelineSuccess)
    assert result.page.evidence == tuple(
        cell for item in result.page.student_id.cells for cell in item.candidates
    ) + tuple(cell for answer in result.page.answers for cell in answer.cells)
    assert (
        cv2.imdecode(
            np.frombuffer(result.artifacts.normalized_png, dtype=np.uint8), cv2.IMREAD_UNCHANGED
        )
        is not None
    )
    assert json.loads(result.artifacts.coordinates_json) == result.page.to_dict()


def test_pipeline_is_deterministic_and_artifacts_remain_associated_with_the_page() -> None:
    image = np.full((240, 340, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (319, 219), (0, 0, 0), 3)
    cv2.rectangle(image, (35, 120), (75, 150), (0, 0, 0), -1)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    task = PipelineInput(
        _page_ref(),
        encoded.tobytes(),
        _profile(),
        thresholds_for_sensitivity(
            50, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
        ).value,
    )

    first = recognize_page(task)
    second = recognize_page(task)
    assert isinstance(first, PipelineSuccess)
    assert isinstance(second, PipelineSuccess)
    assert first.page.page_ref == task.page_ref
    assert first.artifacts == second.artifacts
    coordinates = json.loads(first.artifacts.coordinates_json)
    assert coordinates["page_ref"] == task.page_ref.to_dict()

    normalized = cv2.imdecode(
        np.frombuffer(first.artifacts.normalized_png, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    overlay = cv2.imdecode(
        np.frombuffer(first.artifacts.overlay_png, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    assert normalized is not None and overlay is not None
    assert normalized.shape == overlay.shape
    assert not np.array_equal(normalized, overlay)


def test_pipeline_routes_ambiguous_orientation_to_the_matching_page_failure() -> None:
    image = np.full((240, 340, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (319, 219), (0, 0, 0), 3)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page_ref = _page_ref()
    result = recognize_page(
        PipelineInput(
            page_ref,
            encoded.tobytes(),
            _profile(),
            thresholds_for_sensitivity(
                50, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
            ).value,
        )
    )
    assert isinstance(result, PipelineFailure)
    assert result.page.page_ref == page_ref
    assert result.failure.page_ref == page_ref
    assert result.failure.errors[0].code == "ORIENTATION_UNCERTAIN"
