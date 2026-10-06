"""Versioned, validated bubble-disk mark-recognition thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Final

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

THRESHOLD_VERSION: Final = 5
CALIBRATION_PROVENANCE: Final = "bubble-disk-v5"
MIN_SENSITIVITY: Final = 1
MAX_SENSITIVITY: Final = 10
DEFAULT_SENSITIVITY: Final = 5

# Calibrated on 24 real exam pages (two parts of 12): fitted on one part, checked on the
# other. A mark is ink density above the unmarked level of the same printed digit, in a
# disk of 0.75 x the measured printed-ring radius. Unmarked bubbles reached at most 0.043
# (part 1) / 0.045 (part 2) and the faintest real mark, a 1.5 mm dot, at least 0.292 /
# 0.324. Both parts' fitted ranges contain the constants below, and two older printer
# test sets (black print, pencil marks) stay within 0.058 and above 0.337.
_BLANK_CEILING: Final = 0.11
_MARK_THRESHOLD: Final = 0.24
_MARK_STEP: Final = 0.012
_DISK_RATIO: Final = 0.75
_INK_FLOOR: Final = 0.20


@dataclass(frozen=True, slots=True)
class RecognitionThresholds:
    """Pure values controlling a single recognition pass.

    ``fill`` is the mean ink density (0 for paper, 1 at or below ``ink_floor`` of the local
    paper white) in a disk of ``disk_ratio`` times the printed ring radius, minus the
    unmarked level of the same printed digit on the page. A choice is read only when it
    reaches ``mark_threshold`` while every other choice stays at or below ``blank_ceiling``.

    ``calibrated`` is not sufficient for automatic recognition: its provenance must
    identify this threshold version, so an uncalibrated threshold set cannot silently
    confirm values.
    """

    version: int
    sensitivity: int
    mark_threshold: float
    blank_ceiling: float
    disk_ratio: float
    ink_floor: float
    calibrated: bool
    calibration_provenance: str | None = None

    @property
    def has_valid_calibration_provenance(self) -> bool:
        return self.calibrated and self.calibration_provenance == CALIBRATION_PROVENANCE


def thresholds_for_sensitivity(
    sensitivity: int,
    *,
    calibrated: bool,
    calibration_provenance: str | None = None,
    version: int = THRESHOLD_VERSION,
) -> Result[RecognitionThresholds]:
    """Map user sensitivity to stable, versioned thresholds.

    Higher sensitivity accepts a fainter mark; the blank ceiling never moves, so a
    stray speck cannot become an answer at any setting.
    """
    if type(sensitivity) is not int or not MIN_SENSITIVITY <= sensitivity <= MAX_SENSITIVITY:
        return _error("INVALID_SENSITIVITY", "sensitivity")
    if type(calibrated) is not bool:
        return _error("INVALID_CALIBRATION", "calibrated")
    if calibration_provenance is not None and (
        type(calibration_provenance) is not str or not calibration_provenance
    ):
        return _error("INVALID_CALIBRATION_PROVENANCE", "calibration_provenance")
    if type(version) is not int or version != THRESHOLD_VERSION:
        return _error("UNSUPPORTED_THRESHOLD_VERSION", "version")
    mark_threshold = _MARK_THRESHOLD - _MARK_STEP * (sensitivity - DEFAULT_SENSITIVITY)
    return Ok(
        RecognitionThresholds(
            version=version,
            sensitivity=sensitivity,
            mark_threshold=round(mark_threshold, 6),
            blank_ceiling=_BLANK_CEILING,
            disk_ratio=_DISK_RATIO,
            ink_floor=_INK_FLOOR,
            calibrated=calibrated,
            calibration_provenance=calibration_provenance,
        )
    )


def validate_thresholds(value: RecognitionThresholds) -> Result[RecognitionThresholds]:
    """Validate externally supplied thresholds without exposing exceptions."""
    if not isinstance(value, RecognitionThresholds):
        return _error("INVALID_THRESHOLDS", "thresholds")
    numeric_values = (value.mark_threshold, value.blank_ceiling, value.disk_ratio, value.ink_floor)
    if (
        type(value.version) is not int
        or value.version != THRESHOLD_VERSION
        or type(value.sensitivity) is not int
        or not MIN_SENSITIVITY <= value.sensitivity <= MAX_SENSITIVITY
        or any(type(item) is not float or not isfinite(item) for item in numeric_values)
        or not 0.0 < value.blank_ceiling < value.mark_threshold < 1.0
        or not 0.3 <= value.disk_ratio <= 1.0
        or not 0.0 <= value.ink_floor <= 0.9
        or type(value.calibrated) is not bool
        or (
            value.calibration_provenance is not None
            and (type(value.calibration_provenance) is not str or not value.calibration_provenance)
        )
    ):
        return _error("INVALID_THRESHOLDS", "thresholds")
    return Ok(value)


def _error(code: str, field_path: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path),))
