"""Orientation and registration are confirmed by reading the grid when they mislead."""

from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import omr_grader.recognition.pipeline as pipeline
import omr_grader.recognition.registration as registration
from omr_grader.domain.enums import AnswerStatus, SourceKind
from omr_grader.domain.errors import ErrorInfo, Ok
from omr_grader.domain.models import PageRef
from omr_grader.domain.profile import Profile, parse_profile_bytes
from omr_grader.recognition.orientation import OrientationDecision, OrientationScore
from omr_grader.recognition.pipeline import PipelineInput, PipelineSuccess, recognize_page
from omr_grader.recognition.thresholds import (
    CALIBRATION_PROVENANCE,
    thresholds_for_sensitivity,
)

_WIDTH, _HEIGHT = 1500, 1000
_ANSWER_COLUMNS = (0.28, 0.42, 0.56, 0.70, 0.83)
_STUDENT_ID = "20250007"
_ANSWERS = tuple((question * 7) % 5 + 1 for question in range(100))


def _profile() -> Profile:
    """A Tmot_OMR100-like layout: a sparse ID block left of a dense answer table."""
    regions: list[dict[str, object]] = [
        {
            "name": "student_id",
            "type": "id",
            "bbox_ratio": {"x": 0.05, "y": 0.49, "w": 0.19, "h": 0.31},
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    for index, left in enumerate(_ANSWER_COLUMNS):
        regions.append(
            {
                "name": f"answers_{index}",
                "type": "answer",
                "bbox_ratio": {"x": left, "y": 0.12, "w": 0.11, "h": 0.79},
                "grid": {"cols": 5, "rows": 20},
            }
        )
    parsed = parse_profile_bytes(
        json.dumps(
            {
                "profile_name": "dense-table",
                "page": {
                    "orientation": "landscape",
                    "aspect_ratio": 1.5,
                    "source_width": _WIDTH,
                    "source_height": _HEIGHT,
                },
                "regions": regions,
            }
        ).encode()
    )
    assert isinstance(parsed, Ok)
    return parsed.value


def _cell(profile: Profile, region_index: int, column: int, row: int) -> tuple[int, int, int, int]:
    region = profile.regions[region_index]
    box = region.bbox_ratio
    width = float(box.w) * _WIDTH / region.grid.cols
    height = float(box.h) * _HEIGHT / region.grid.rows
    return (
        int(float(box.x) * _WIDTH + (column + 0.5) * width),
        int(float(box.y) * _HEIGHT + (row + 0.5) * height),
        int(width * 0.4),
        int(height * 0.4),
    )


def _sheet(profile: Profile) -> np.ndarray:
    """Upright filled sheet whose printed table outweighs the ID block, like the real form."""
    image = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    cv2.rectangle(
        image,
        (int(0.02 * _WIDTH), int(0.03 * _HEIGHT)),
        (int(0.98 * _WIDTH), int(0.97 * _HEIGHT)),
        0,
        4,
    )
    for region in profile.regions[1:]:
        box = region.bbox_ratio
        left, top = float(box.x) * _WIDTH, float(box.y) * _HEIGHT
        width = float(box.w) * _WIDTH / region.grid.cols
        height = float(box.h) * _HEIGHT / region.grid.rows
        right, bottom = left + region.grid.cols * width, top + region.grid.rows * height
        for row in range(region.grid.rows + 1):
            y = int(top + row * height)
            cv2.line(image, (int(left), y), (int(right), y), 120, 2)
        for column in range(region.grid.cols + 1):
            x = int(left + column * width)
            cv2.line(image, (x, int(top)), (x, int(bottom)), 120, 2)
    for region_index, region in enumerate(profile.regions):
        for row in range(region.grid.rows):
            for column in range(region.grid.cols):
                x, y, rx, ry = _cell(profile, region_index, column, row)
                cv2.ellipse(image, (x, y), (rx, ry), 0, 0, 360, 150, 1)
    for column, digit in enumerate(_STUDENT_ID):
        x, y, rx, ry = _cell(profile, 0, column, int(digit))
        cv2.ellipse(image, (x, y), (rx, ry), 0, 0, 360, 20, -1)
    for question, choice in enumerate(_ANSWERS):
        x, y, rx, ry = _cell(profile, 1 + question // 20, choice - 1, question % 20)
        cv2.ellipse(image, (x, y), (rx, ry), 0, 0, 360, 20, -1)
    return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)


def _task(image: np.ndarray, profile: Profile) -> PipelineInput:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    page_ref = PageRef(
        1,
        "session",
        "item",
        SourceKind.PDF,
        "0" * 64,
        "scan.pdf",
        "scan.pdf p1",
        1,
        None,
        0,
        0,
        "scan",
    )
    thresholds = thresholds_for_sensitivity(
        5, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
    ).value
    return PipelineInput(page_ref, encoded.tobytes(), profile, thresholds)


def _assert_reads_every_mark(result: object) -> None:
    assert isinstance(result, PipelineSuccess)
    assert result.page.student_id.value == _STUDENT_ID
    assert all(answer.value.status is AnswerStatus.NORMAL for answer in result.page.answers)
    assert tuple(answer.value.choices for answer in result.page.answers) == tuple(
        (choice,) for choice in _ANSWERS
    )


def test_upright_sheet_stays_upright_when_landmarks_favor_the_180_degree_turn() -> None:
    profile = _profile()
    sheet = _sheet(profile)
    gray = cv2.cvtColor(sheet, cv2.COLOR_BGR2GRAY)
    scorer = pipeline._orientation_scorer(profile)
    # Precondition: the landmark heuristic alone would pick the wrong turn decisively.
    assert scorer(cv2.rotate(gray, cv2.ROTATE_180)) - scorer(gray) > 0.05

    result = recognize_page(_task(sheet, profile))

    _assert_reads_every_mark(result)
    assert isinstance(result, PipelineSuccess)
    assert result.page.rotation_degrees == 0


def test_upside_down_sheet_is_turned_by_the_grid_that_reads_cleanly() -> None:
    profile = _profile()

    result = recognize_page(_task(cv2.rotate(_sheet(profile), cv2.ROTATE_180), profile))

    _assert_reads_every_mark(result)
    assert isinstance(result, PipelineSuccess)
    assert result.page.rotation_degrees == 180


@pytest.mark.parametrize("rotation", [0, 180])
def test_a_confident_first_read_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch, rotation: int
) -> None:
    profile = _profile()
    sheet = _sheet(profile) if rotation == 0 else cv2.rotate(_sheet(profile), cv2.ROTATE_180)
    scores = tuple(
        OrientationScore(degrees, 0.9 if degrees == rotation else 0.1)
        for degrees in (0, 90, 180, 270)
    )
    monkeypatch.setattr(
        pipeline,
        "select_orientation",
        lambda *_args, **_kwargs: Ok(OrientationDecision(rotation, 0.9, scores)),
    )
    reads: list[int] = []
    original = pipeline._read_oriented

    def counted(image: np.ndarray, degrees: int, task: PipelineInput) -> object:
        reads.append(degrees)
        return original(image, degrees, task)

    monkeypatch.setattr(pipeline, "_read_oriented", counted)

    result = recognize_page(_task(sheet, profile))

    _assert_reads_every_mark(result)
    assert reads == [rotation]


def test_a_flipped_read_keeps_the_score_of_the_rotation_it_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    scores = (
        OrientationScore(0, 0.67),
        OrientationScore(90, 0.51),
        OrientationScore(180, 0.74),
        OrientationScore(270, 0.49),
    )
    monkeypatch.setattr(
        pipeline,
        "select_orientation",
        lambda *_args, **_kwargs: Ok(OrientationDecision(180, 0.74, scores)),
    )

    result = recognize_page(_task(_sheet(profile), profile))

    _assert_reads_every_mark(result)
    assert isinstance(result, PipelineSuccess)
    assert result.page.rotation_degrees == 0
    assert result.page.orientation_confidence == "0.67"


def _read_with(clear: int, questions: int = 100) -> tuple[None, SimpleNamespace]:
    answers = tuple(
        SimpleNamespace(
            value=SimpleNamespace(
                status=AnswerStatus.NORMAL if index < clear else AnswerStatus.BLANK
            )
        )
        for index in range(questions)
    )
    return None, SimpleNamespace(answers=answers)


@pytest.mark.parametrize(
    ("clear", "confident"),
    ((100, True), (90, True), (89, False), (60, False), (0, False)),
)
def test_only_a_confident_read_may_overturn_the_landmark_orientation(
    clear: int, confident: bool
) -> None:
    # Real upright scans read upside down still produced up to 60 clear answers, so
    # anything short of a confident read keeps the landmark decision.
    assert pipeline._confident_read(_read_with(clear)) is confident  # type: ignore[arg-type]


def test_failed_or_empty_reads_are_never_confident() -> None:
    failure = ErrorInfo("IMAGE_DECODE_FAILED", "error.image_decode_failed")
    assert not pipeline._confident_read(failure)
    assert not pipeline._confident_read(_read_with(0, 0))  # type: ignore[arg-type]


def _fixed_orientation(monkeypatch: pytest.MonkeyPatch, rotation: int = 0) -> None:
    scores = tuple(
        OrientationScore(degrees, 0.9 if degrees == rotation else 0.1)
        for degrees in (0, 90, 180, 270)
    )
    monkeypatch.setattr(
        pipeline,
        "select_orientation",
        lambda *_args, **_kwargs: Ok(OrientationDecision(rotation, 0.9, scores)),
    )


def test_a_grid_fitted_to_a_shrunken_alias_is_replaced_by_a_confident_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real scans locked onto a grid shrunk to 0.904 and shifted right; reproduce that fit.
    profile = _profile()
    _fixed_orientation(monkeypatch)
    alias = registration._moved(profile, _WIDTH, _HEIGHT, 0.904, 60, 1.0, 0)
    assert alias is not None
    monkeypatch.setattr(pipeline, "register_profile_grid", lambda _image, _profile: alias)

    result = recognize_page(_task(_sheet(profile), profile))

    _assert_reads_every_mark(result)
    assert isinstance(result, PipelineSuccess)
    assert result.page.rotation_degrees == 0


def test_a_confident_default_fit_is_never_second_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _fixed_orientation(monkeypatch)
    searched: list[object] = []
    monkeypatch.setattr(
        pipeline,
        "registration_candidates",
        lambda *args, **kwargs: searched.append(args) or (),
    )

    result = recognize_page(_task(_sheet(profile), profile))

    _assert_reads_every_mark(result)
    assert searched == []


def test_unconfident_alternative_fits_leave_the_default_read_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _fixed_orientation(monkeypatch)
    alias = registration._moved(profile, _WIDTH, _HEIGHT, 0.904, 60, 1.0, 0)
    assert alias is not None
    monkeypatch.setattr(pipeline, "register_profile_grid", lambda _image, _profile: alias)
    monkeypatch.setattr(pipeline, "registration_candidates", lambda *_args, **_kwargs: (alias,))

    result = recognize_page(_task(_sheet(profile), profile))

    assert isinstance(result, PipelineSuccess)
    clear = sum(answer.value.status is AnswerStatus.NORMAL for answer in result.page.answers)
    assert clear < 90
