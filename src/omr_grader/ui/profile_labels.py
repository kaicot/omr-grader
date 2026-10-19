"""Readable names for answer-sheet forms in the profile lists.

Every form the scan page detects is saved with the same profile name, so the lists describe
a form by what it holds instead: question count, question ranges and ID width.
"""

from __future__ import annotations

import re

from omr_grader.domain.profile import Profile
from omr_grader.infrastructure.form_detection import AUTO_PROFILE_NAME

_AUTO_FILE_SUFFIX = re.compile(r"_([0-9a-f]{6})\.omrtemplate$")


def _questions(profile: Profile) -> tuple[int, list[str]]:
    ranges: list[str] = []
    total = 0
    next_start = 1
    for region in profile.answer_regions:
        start = int(region.question_start or next_start)
        rows = region.grid.rows
        ranges.append(f"{start}~{start + rows - 1}")
        total += rows
        next_start = start + rows
    return total, ranges


def _id_digits(profile: Profile) -> int | None:
    try:
        return profile.id_region.grid.cols
    except StopIteration:
        return None


def form_summary(profile: Profile) -> str:
    """``객관식 60문항(1~20, 21~40, 41~60) · 학번 8자리``"""
    total, ranges = _questions(profile)
    digits = _id_digits(profile)
    summary = f"객관식 {total}문항({', '.join(ranges)})"
    return summary if digits is None else f"{summary} · 학번 {digits}자리"


def form_label(profile: Profile, filename: str) -> str:
    """The list entry: a detected form by its content, an imported one by its own name."""
    total, _ = _questions(profile)
    if profile.profile_name != AUTO_PROFILE_NAME:
        return f"{profile.profile_name} · 객관식 {total}문항"
    digits = _id_digits(profile)
    content = f"객관식 {total}문항" if digits is None else f"객관식 {total}문항 · 학번 {digits}자리"
    match = _AUTO_FILE_SUFFIX.search(filename)
    return f"{content} (자동, {match.group(1)})" if match else f"{content} (자동)"


__all__ = ["form_label", "form_summary"]
