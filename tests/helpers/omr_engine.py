"""Engine-facing fixtures shared by the v4 recognition tests.

``synthetic_omr`` only draws pages. This module adds what needs the recognition engine: a
detected layout and generated profile for a reference sheet, page references and
thresholds. Detecting a full-size page takes about a second, so the reference sheets are
computed once per process and shared read-only; copy an image before drawing on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache

import numpy as np
from numpy.typing import NDArray

from omr_grader.domain.enums import SourceKind
from omr_grader.domain.errors import Err
from omr_grader.domain.models import PageRef
from omr_grader.domain.profile import Profile
from omr_grader.recognition.form_layout import FormLayout, detect_layout
from omr_grader.recognition.form_profile import build_profile
from omr_grader.recognition.thresholds import (
    CALIBRATION_PROVENANCE,
    RecognitionThresholds,
    thresholds_for_sensitivity,
)
from tests.helpers.synthetic_omr import (
    DEFAULT_LAYOUT,
    Choice,
    SheetGeometry,
    render_sheet_with_geometry,
    sample_answers,
    to_gray,
)

REFERENCE_STUDENT_ID = "20250001"


@dataclass(frozen=True, slots=True)
class ReferenceSheet:
    """A rendered, marked sheet with its true geometry, detected layout and profile."""

    image: NDArray[np.uint8]
    gray: NDArray[np.uint8]
    geometry: SheetGeometry
    answers: Mapping[int, Choice]
    student_id: str
    layout: FormLayout
    profile: Profile
    payload: bytes


def make_page_ref(name: str = "scan.png") -> PageRef:
    return PageRef(
        1,
        "session",
        "item",
        SourceKind.IMAGE,
        "0" * 64,
        name,
        name,
        None,
        None,
        0,
        0,
        "scan",
    )


def make_thresholds(sensitivity: int = 5, *, calibrated: bool = True) -> RecognitionThresholds:
    result = thresholds_for_sensitivity(
        sensitivity,
        calibrated=calibrated,
        calibration_provenance=CALIBRATION_PROVENANCE if calibrated else None,
    )
    assert not isinstance(result, Err), result
    return result.value


def profile_from_page(
    image: NDArray[np.uint8], name: str = "synthetic"
) -> tuple[Profile, bytes, FormLayout]:
    """Detect the layout of one page and build its profile, as the app does for a new form."""
    gray = to_gray(image)
    layout = detect_layout(gray)
    assert layout is not None, "no bubble layout found on the page"
    built = build_profile([(layout, (gray.shape[1], gray.shape[0]))], name)
    assert not isinstance(built, Err), built
    profile, payload = built.value
    return profile, payload, layout


@cache
def reference_sheet(layout: tuple[int, ...] = DEFAULT_LAYOUT) -> ReferenceSheet:
    """The marked sheet of ``layout`` that most tests use (one per process and layout)."""
    answers = sample_answers(sum(layout))
    image, geometry = render_sheet_with_geometry(answers, REFERENCE_STUDENT_ID, layout=layout)
    profile, payload, detected = profile_from_page(image, "reference")
    gray = to_gray(image)
    image.setflags(write=False)
    gray.setflags(write=False)
    return ReferenceSheet(
        image, gray, geometry, answers, REFERENCE_STUDENT_ID, detected, profile, payload
    )
