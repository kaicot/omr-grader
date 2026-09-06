from __future__ import annotations

import numpy as np
import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.recognition.orientation import rotate_right_angle, select_orientation


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_select_orientation_scores_all_right_angles_deterministically(rotation: int) -> None:
    upright = np.zeros((20, 30), dtype=np.uint8)
    upright[0, 0] = 255
    source = rotate_right_angle(upright, (360 - rotation) % 360)

    result = select_orientation(source, lambda candidate: float(candidate[0, 0]) / 255.0)

    assert isinstance(result, Ok)
    assert result.value.rotation_degrees == rotation
    assert tuple(score.rotation_degrees for score in result.value.scores) == (0, 90, 180, 270)


def test_orientation_tie_requires_manual_review() -> None:
    result = select_orientation(np.zeros((20, 20), dtype=np.uint8), lambda _: 0.9)

    assert isinstance(result, Err)
    assert result.errors[0].code == "ORIENTATION_UNCERTAIN"
    assert result.errors[0].context["manual_review"] is True


def test_low_orientation_confidence_requires_manual_review() -> None:
    image = np.zeros((20, 20), dtype=np.uint8)
    image[0, 0] = 255
    result = select_orientation(
        image,
        lambda candidate: 0.2 if candidate[0, 0] else 0.1,
        tie_margin=0.0,
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "ORIENTATION_UNCERTAIN"


def test_preferred_upright_rotation_resolves_only_same_axis_ambiguity() -> None:
    image = np.zeros((20, 30), dtype=np.uint8)
    image[0, 0] = 2
    image[-1, -1] = 1

    def scorer(candidate):
        height, width = candidate.shape
        if width > height:
            return 0.69 if candidate[0, 0] == 2 else 0.68
        return 0.50

    result = select_orientation(image, scorer, preferred_rotation=0)

    assert isinstance(result, Ok)
    assert result.value.rotation_degrees == 0

    unresolved = select_orientation(
        image,
        lambda _: 0.9,
        preferred_rotation=0,
    )
    assert isinstance(unresolved, Err)
