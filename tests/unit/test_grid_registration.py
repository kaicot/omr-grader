from __future__ import annotations

import json
from decimal import Decimal

import cv2
import numpy as np

from omr_grader.domain.profile import parse_profile_bytes
from omr_grader.recognition.registration import register_profile_grid, registration_candidates


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
    return parse_profile_bytes(
        json.dumps(
            {
                "profile_name": "registration",
                "page": {
                    "orientation": "landscape",
                    "aspect_ratio": 1.5,
                    "source_width": 600,
                    "source_height": 400,
                },
                "regions": regions,
            }
        ).encode()
    ).value


def _printed_table(profile, scale_x=0.94, offset_x=18, scale_y=0.96, offset_y=13):
    width, height = 600, 400
    image = np.full((height, width, 3), 255, dtype=np.uint8)
    for region in profile.regions:
        box = region.bbox_ratio
        left = round(scale_x * float(box.x) * width + offset_x)
        right = round(scale_x * float(box.x + box.w) * width + offset_x)
        top = scale_y * float(box.y) * height + offset_y
        region_height = scale_y * float(box.h) * height
        cv2.line(image, (left, round(top)), (left, round(top + region_height)), (0, 0, 0), 2)
        cv2.line(
            image,
            (right, round(top)),
            (right, round(top + region_height)),
            (0, 0, 0),
            2,
        )
        for row in range(region.grid.rows + 1):
            y = round(top + region_height * row / region.grid.rows)
            cv2.line(image, (left, y), (right, y), (0, 0, 0), 2)
    return image


def test_grid_registration_recovers_global_print_scale_and_offset() -> None:
    profile = _profile()
    image = _printed_table(profile)

    registered = register_profile_grid(image, profile)
    answer = registered.regions[1].bbox_ratio

    assert abs(answer.x - Decimal("0.312")) < Decimal("0.005")
    assert abs(answer.y - Decimal("0.128")) < Decimal("0.005")
    assert abs(answer.w - Decimal("0.094")) < Decimal("0.005")
    assert abs(answer.h - Decimal("0.768")) < Decimal("0.005")


def test_grid_registration_keeps_an_aligned_profile_unchanged() -> None:
    profile = _profile()
    image = np.full((400, 600, 3), 255, dtype=np.uint8)

    assert register_profile_grid(image, profile) == profile


def test_registration_candidates_offer_distinct_fits_led_by_the_printed_one() -> None:
    profile = _profile()
    image = _printed_table(profile)

    candidates = registration_candidates(image, profile)

    assert 1 < len(candidates) <= 4
    assert candidates[0] == register_profile_grid(image, profile)
    lefts = [candidate.regions[1].bbox_ratio.x for candidate in candidates]
    assert all(
        abs(first - second) > Decimal("0.001")
        for index, first in enumerate(lefts)
        for second in lefts[index + 1 :]
    )


def test_registration_candidates_need_an_image_and_a_positive_limit() -> None:
    profile = _profile()

    assert registration_candidates(np.zeros((0, 0), dtype=np.uint8), profile) == ()
    assert registration_candidates(_printed_table(profile), profile, limit=0) == ()
