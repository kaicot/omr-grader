from __future__ import annotations

from dataclasses import replace
from math import inf, nan

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.recognition.thresholds import (
    CALIBRATION_PROVENANCE,
    DEFAULT_SENSITIVITY,
    MAX_SENSITIVITY,
    MIN_SENSITIVITY,
    THRESHOLD_VERSION,
    RecognitionThresholds,
    thresholds_for_sensitivity,
    validate_thresholds,
)


def _calibrated(sensitivity: int = DEFAULT_SENSITIVITY) -> RecognitionThresholds:
    result = thresholds_for_sensitivity(
        sensitivity, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
    )
    assert isinstance(result, Ok)
    return result.value


def test_default_sensitivity_uses_the_calibrated_bubble_disk_constants() -> None:
    thresholds = _calibrated()

    assert THRESHOLD_VERSION == 5
    assert CALIBRATION_PROVENANCE == "bubble-disk-v5"
    assert DEFAULT_SENSITIVITY == 5
    assert thresholds.version == 5
    assert thresholds.sensitivity == 5
    assert thresholds.mark_threshold == 0.24
    assert thresholds.blank_ceiling == 0.11
    assert thresholds.disk_ratio == 0.75
    assert thresholds.ink_floor == 0.2
    assert thresholds.has_valid_calibration_provenance
    assert not hasattr(thresholds, "dark_ratio")  # renamed to ink_floor


def test_higher_sensitivity_accepts_fainter_marks_but_never_moves_the_blank_ceiling() -> None:
    thresholds = [_calibrated(level) for level in range(MIN_SENSITIVITY, MAX_SENSITIVITY + 1)]

    marks = [item.mark_threshold for item in thresholds]
    assert marks == sorted(marks, reverse=True)
    assert len(set(marks)) == len(marks)
    assert marks[0] == pytest.approx(0.288)
    assert marks[-1] == pytest.approx(0.18)
    assert {item.blank_ceiling for item in thresholds} == {0.11}
    assert {item.disk_ratio for item in thresholds} == {0.75}
    assert all(item.blank_ceiling < item.mark_threshold for item in thresholds)


def test_only_the_matching_calibration_provenance_may_confirm_values() -> None:
    uncalibrated = thresholds_for_sensitivity(5, calibrated=False)
    foreign = thresholds_for_sensitivity(
        5, calibrated=True, calibration_provenance="local-background-v2"
    )
    anonymous = thresholds_for_sensitivity(5, calibrated=True)

    for result in (uncalibrated, foreign, anonymous):
        assert isinstance(result, Ok)
        assert not result.value.has_valid_calibration_provenance
    assert isinstance(uncalibrated, Ok) and isinstance(foreign, Ok)
    assert foreign.value.mark_threshold == uncalibrated.value.mark_threshold == 0.24


@pytest.mark.parametrize(
    ("arguments", "code"),
    (
        ({"sensitivity": 0, "calibrated": True}, "INVALID_SENSITIVITY"),
        ({"sensitivity": 11, "calibrated": True}, "INVALID_SENSITIVITY"),
        ({"sensitivity": True, "calibrated": True}, "INVALID_SENSITIVITY"),
        ({"sensitivity": 5.0, "calibrated": True}, "INVALID_SENSITIVITY"),
        ({"sensitivity": 5, "calibrated": "yes"}, "INVALID_CALIBRATION"),
        (
            {"sensitivity": 5, "calibrated": True, "calibration_provenance": 5},
            "INVALID_CALIBRATION_PROVENANCE",
        ),
        (
            {"sensitivity": 5, "calibrated": True, "calibration_provenance": ""},
            "INVALID_CALIBRATION_PROVENANCE",
        ),
        ({"sensitivity": 5, "calibrated": True, "version": 4}, "UNSUPPORTED_THRESHOLD_VERSION"),
    ),
)
def test_invalid_requests_are_typed_errors(arguments: dict[str, object], code: str) -> None:
    result = thresholds_for_sensitivity(**arguments)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.errors[0].code == code
    assert result.errors[0].message_key == f"error.{code.lower()}"


def test_validation_accepts_the_calibrated_values_and_the_inclusive_limits() -> None:
    good = _calibrated()

    assert validate_thresholds(good) == Ok(good)
    for limits in (
        {"ink_floor": 0.0},
        {"ink_floor": 0.9},
        {"disk_ratio": 0.3},
        {"disk_ratio": 1.0},
    ):
        assert isinstance(validate_thresholds(replace(good, **limits)), Ok), limits


@pytest.mark.parametrize(
    "changes",
    (
        {"mark_threshold": 0.11},  # not above the blank ceiling
        {"mark_threshold": 0.05},
        {"mark_threshold": 1.0},
        {"blank_ceiling": 0.0},
        {"blank_ceiling": 0.3},
        {"disk_ratio": 0.29},
        {"disk_ratio": 1.01},
        {"ink_floor": -0.01},
        {"ink_floor": 0.91},
        {"ink_floor": nan},
        {"mark_threshold": inf},
        {"disk_ratio": 1},  # integers are not floats
        {"sensitivity": 0},
        {"sensitivity": 11},
        {"version": 4},
        {"calibrated": 1},
        {"calibration_provenance": ""},
        {"calibration_provenance": 5},
    ),
    ids=lambda changes: ",".join(f"{key}={value}" for key, value in changes.items()),
)
def test_validation_rejects_inconsistent_values(changes: dict[str, object]) -> None:
    result = validate_thresholds(replace(_calibrated(), **changes))  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_THRESHOLDS"


def test_validation_rejects_anything_that_is_not_a_threshold_set() -> None:
    result = validate_thresholds({"mark_threshold": 0.24})  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_THRESHOLDS"
