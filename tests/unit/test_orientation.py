from __future__ import annotations

import cv2
import numpy as np
import pytest

from omr_grader.recognition.orientation import rotate_right_angle


def _marked(shape: tuple[int, ...] = (2, 3)) -> np.ndarray:
    """Every pixel distinct, so a wrong turn cannot go unnoticed."""
    count = int(np.prod(shape))
    return np.arange(1, count + 1, dtype=np.uint8).reshape(shape)


def test_rotation_is_clockwise_for_every_right_angle() -> None:
    image = _marked()  # [[1 2 3], [4 5 6]]

    assert np.array_equal(rotate_right_angle(image, 0), image)
    assert np.array_equal(rotate_right_angle(image, 90), [[4, 1], [5, 2], [6, 3]])
    assert np.array_equal(rotate_right_angle(image, 180), [[6, 5, 4], [3, 2, 1]])
    assert np.array_equal(rotate_right_angle(image, 270), [[3, 6], [2, 5], [1, 4]])


@pytest.mark.parametrize("rotation", (90, 180, 270))
def test_rotation_matches_opencv_codes_and_swaps_sides_only_for_quarter_turns(
    rotation: int,
) -> None:
    image = _marked((20, 30))
    code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}

    rotated = rotate_right_angle(image, rotation)

    assert np.array_equal(rotated, cv2.rotate(image, code[rotation]))
    assert rotated.shape == ((30, 20) if rotation in (90, 270) else (20, 30))


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_turning_back_restores_the_original_without_any_interpolation(rotation: int) -> None:
    rng = np.random.default_rng(rotation)
    image = rng.integers(0, 256, size=(24, 40, 3), dtype=np.uint8)

    restored = rotate_right_angle(rotate_right_angle(image, rotation), (360 - rotation) % 360)

    assert np.array_equal(restored, image)
    assert sorted(rotate_right_angle(image, rotation).ravel().tolist()) == sorted(
        image.ravel().tolist()
    )


def test_zero_rotation_returns_a_fresh_copy() -> None:
    image = _marked()

    copy = rotate_right_angle(image, 0)
    copy[0, 0] = 99

    assert copy is not image
    assert image[0, 0] == 1


def test_color_and_alpha_channels_are_kept_in_place() -> None:
    for channels in (3, 4):
        image = np.zeros((4, 6, channels), dtype=np.uint8)
        image[0, 0] = np.arange(1, channels + 1)

        rotated = rotate_right_angle(image, 90)

        assert rotated.shape == (6, 4, channels)
        assert tuple(rotated[0, 3]) == tuple(range(1, channels + 1))


@pytest.mark.parametrize("rotation", (-90, 45, 91, 360, 450))
def test_only_the_four_right_angles_are_accepted(rotation: int) -> None:
    with pytest.raises(ValueError, match="rotation_degrees"):
        rotate_right_angle(_marked(), rotation)


@pytest.mark.parametrize(
    "image",
    (
        np.zeros((4, 4), dtype=np.float32),
        np.zeros((4, 4), dtype=np.uint16),
        np.zeros(4, dtype=np.uint8),
        np.zeros((4, 4, 2), dtype=np.uint8),
        np.zeros((1, 4, 4, 3), dtype=np.uint8),
    ),
)
def test_only_eight_bit_gray_or_color_rasters_are_accepted(image: np.ndarray) -> None:
    with pytest.raises(ValueError, match="8-bit"):
        rotate_right_angle(image, 90)
