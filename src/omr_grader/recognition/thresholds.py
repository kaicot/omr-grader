"""Versioned, validated local-background mark-recognition thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Final

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

THRESHOLD_VERSION: Final = 3
CALIBRATION_PROVENANCE: Final = "local-background-v3"
MIN_SENSITIVITY: Final = 0
MAX_SENSITIVITY: Final = 100


@dataclass(frozen=True, slots=True)
class RecognitionThresholds:
    """Pure values controlling a single recognition pass.

    ``calibrated`` is not sufficient for automatic recognition: its provenance
    must identify this threshold version.  This prevents an uncalibrated or
    differently-calibrated threshold set from silently confirming values.
    """

    version: int
    sensitivity: int
    mark_threshold: float
    ambiguity_margin: float
    inner_margin_ratio: float
    dark_pixel_threshold: int
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
    """Map user sensitivity to stable, versioned local-background thresholds."""
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

    # Greater sensitivity accepts a smaller locally compensated dark fraction.
    mark_threshold = 0.32 - (0.18 * sensitivity / MAX_SENSITIVITY)
    return Ok(
        RecognitionThresholds(
            version=version,
            sensitivity=sensitivity,
            mark_threshold=mark_threshold,
            ambiguity_margin=0.05,
            inner_margin_ratio=0.20,
            dark_pixel_threshold=160,
            calibrated=calibrated,
            calibration_provenance=calibration_provenance,
        )
    )


def validate_thresholds(value: RecognitionThresholds) -> Result[RecognitionThresholds]:
    """Validate externally supplied thresholds without exposing exceptions."""
    if not isinstance(value, RecognitionThresholds):
        return _error("INVALID_THRESHOLDS", "thresholds")
    numeric_values = (value.mark_threshold, value.ambiguity_margin, value.inner_margin_ratio)
    if (
        type(value.version) is not int
        or value.version != THRESHOLD_VERSION
        or type(value.sensitivity) is not int
        or not MIN_SENSITIVITY <= value.sensitivity <= MAX_SENSITIVITY
        or any(type(item) is not float or not isfinite(item) for item in numeric_values)
        or not 0.0 < value.mark_threshold < 1.0
        or not 0.0 <= value.ambiguity_margin < 1.0
        or not 0.0 <= value.inner_margin_ratio < 0.5
        or type(value.dark_pixel_threshold) is not int
        or not 0 <= value.dark_pixel_threshold <= 255
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
