from decimal import Decimal

import numpy as np

from omr_grader.domain.enums import AnswerStatus, FieldStatus, StudentIdStatus
from omr_grader.domain.profile import BoundingBoxRatio, Grid, Profile, ProfileRegion
from omr_grader.recognition.grid_reader import read_grid
from omr_grader.recognition.thresholds import (
    CALIBRATION_PROVENANCE,
    thresholds_for_sensitivity,
)


def _profile() -> Profile:
    regions = [
        ProfileRegion(
            "id",
            "id",
            BoundingBoxRatio(Decimal("0"), Decimal("0"), Decimal("0.8"), Decimal("0.1")),
            Grid(8, 10),
        )
    ]
    for region in range(5):
        regions.append(
            ProfileRegion(
                f"answers-{region}",
                "answer",
                BoundingBoxRatio(
                    Decimal("0"),
                    Decimal("0.1") + Decimal(region) * Decimal("0.18"),
                    Decimal("0.4"),
                    Decimal("0.18"),
                ),
                Grid(5, 20),
                region * 20 + 1,
            )
        )
    return Profile(1, "synthetic", None, tuple(regions), "0" * 64)


def _mark(image: np.ndarray, x: int, y: int, w: int, h: int, value: int = 0) -> None:
    image[y : y + h, x : x + w] = value


def _id_mark(image: np.ndarray, column: int, digit: int, value: int = 0) -> None:
    _mark(image, column * 100 + 20, digit * 10 + 2, 60, 6, value)


def _answer_mark(image: np.ndarray, question: int, choice: int, value: int = 0) -> None:
    row = (question - 1) % 20
    region = (question - 1) // 20
    _mark(image, (choice - 1) * 80 + 16, 101 + region * 180 + row * 9, 48, 7, value)


def _read(image: np.ndarray, calibrated: bool = True):
    thresholds = thresholds_for_sensitivity(
        50,
        calibrated=calibrated,
        calibration_provenance=CALIBRATION_PROVENANCE if calibrated else None,
    ).value
    result = read_grid(image, _profile(), thresholds)
    assert hasattr(result, "value")
    return result.value


def test_synthetic_marks_cover_answer_states_and_ordered_evidence() -> None:
    image = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(image, column, column)
    _answer_mark(image, 1, 3)
    _answer_mark(image, 2, 1)
    _answer_mark(image, 2, 4)
    image[112:117, 256:304] = 255  # second selected choice has a lower, decisive score
    _answer_mark(image, 3, 2)
    _answer_mark(image, 3, 3)  # equal scores are ambiguous even though both exceed threshold
    _answer_mark(image, 4, 5, 140)  # faint but above dark-pixel threshold

    recognition = _read(image)
    assert recognition.student_id.value == "01234567"
    assert recognition.answers[0].value.choices == (3,)
    assert recognition.answers[0].value.status is AnswerStatus.NORMAL
    assert recognition.answers[1].value.choices == (1, 4)
    assert recognition.answers[1].value.status is AnswerStatus.MULTIPLE
    assert recognition.answers[2].value.status is AnswerStatus.UNCERTAIN
    assert recognition.answers[3].value.choices == (5,)
    assert recognition.answers[4].value.status is AnswerStatus.BLANK
    assert tuple(cell.index for cell in recognition.evidence) == tuple(range(580))
    assert all(
        cell.pixel_rect is not None and cell.ratio_rect is not None for cell in recognition.evidence
    )


def test_id_is_emitted_only_when_every_column_is_single() -> None:
    image = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(image, column, column)
    assert _read(image).student_id.status is StudentIdStatus.NORMAL

    blank = image.copy()
    blank[0:10, 0:100] = 255
    blank_result = _read(blank).student_id
    assert blank_result.value is None
    assert blank_result.cells[0].status is FieldStatus.BLANK

    multiple = image.copy()
    _mark(multiple, 0, 92, 100, 5)
    multiple_result = _read(multiple).student_id
    assert multiple_result.value is None
    assert multiple_result.cells[0].status is FieldStatus.MULTIPLE

    boundary = image.copy()
    _id_mark(boundary, 0, 0, 140)
    _id_mark(boundary, 0, 1, 140)
    boundary_result = _read(boundary).student_id
    assert boundary_result.value is None
    assert boundary_result.cells[0].status is FieldStatus.UNCERTAIN


def test_uncalibrated_scores_are_for_manual_review() -> None:
    image = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(image, column, column)
    _answer_mark(image, 1, 1)

    recognition = _read(image, calibrated=False)
    assert recognition.needs_manual_review
    assert recognition.student_id.value is None
    assert recognition.answers[0].value.status is AnswerStatus.UNCERTAIN


def test_old_calibration_provenance_cannot_auto_confirm() -> None:
    image = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(image, column, column)
    _answer_mark(image, 1, 1)
    thresholds = thresholds_for_sensitivity(
        50,
        calibrated=True,
        calibration_provenance="local-background-v2",
    ).value

    result = read_grid(image, _profile(), thresholds)
    assert hasattr(result, "value")
    recognition = result.value
    assert recognition.needs_manual_review
    assert recognition.student_id.value is None
    assert recognition.answers[0].value.status is AnswerStatus.UNCERTAIN


def test_background_normalized_scoring_rejects_dark_and_uneven_blanks() -> None:
    dark = np.full((1000, 1000), 24, dtype=np.uint8)
    assert _read(dark).answers[0].value.status is AnswerStatus.BLANK

    gradient = np.tile(np.linspace(80, 240, 1000, dtype=np.uint8), (1000, 1))
    assert _read(gradient).answers[0].value.status is AnswerStatus.BLANK

    saturated = np.zeros((1000, 1000), dtype=np.uint8)
    assert _read(saturated).answers[0].value.status is AnswerStatus.BLANK


def test_outline_without_inner_darkness_is_not_a_mark() -> None:
    image = np.full((1000, 1000), 255, dtype=np.uint8)
    image[100, 0:80] = 0
    image[108, 0:80] = 0
    image[100:109, 0] = 0
    image[100:109, 79] = 0

    answer = _read(image).answers[0].value
    assert answer.status is AnswerStatus.BLANK
    assert answer.choices == ()


def test_threshold_edges_partial_tiny_and_close_marks_remain_conservative() -> None:
    image = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(image, column, column)

    # The central scoring area is 48 by 7 pixels; 77 dark pixels sit at the
    # threshold boundary and therefore require review rather than selection.
    image[101:108, 176:187] = 0
    edge = _read(image).answers[0].value
    assert edge.status is AnswerStatus.UNCERTAIN
    assert edge.choices == ()

    tiny = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(tiny, column, column)
    tiny[101:102, 256:257] = 0
    tiny_result = _read(tiny).answers[0].value
    assert tiny_result.status is AnswerStatus.BLANK
    assert tiny_result.choices == ()

    close = np.full((1000, 1000), 255, dtype=np.uint8)
    for column in range(8):
        _id_mark(close, column, column)
    _answer_mark(close, 1, 1)
    _answer_mark(close, 1, 2)
    close_result = _read(close).answers[0].value
    assert close_result.status is AnswerStatus.UNCERTAIN
    assert close_result.choices == (1, 2)
