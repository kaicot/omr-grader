"""Forms detected from scans all carry the same profile name; the lists name them by content."""

from __future__ import annotations

from omr_grader.infrastructure.form_detection import AUTO_PROFILE_NAME
from omr_grader.ui.profile_labels import form_label, form_summary
from tests.helpers.omr_engine import profile_from_page
from tests.helpers.synthetic_omr import render_sheet, sample_answers


def _profile(layout: tuple[int, ...], name: str = AUTO_PROFILE_NAME):
    image = render_sheet(sample_answers(sum(layout)), "20260001", layout=layout)
    profile, _, _ = profile_from_page(image, name)
    return profile


def test_detected_forms_are_told_apart_by_their_content() -> None:
    hundred = _profile((20, 20, 20, 20, 20))
    sixty = _profile((20, 20, 20))

    first = form_label(hundred, "자동양식_객관식100문항_891d92.omrtemplate")
    second = form_label(sixty, "자동양식_객관식60문항_fbf823.omrtemplate")

    assert first == "객관식 100문항 · 학번 8자리 (자동, 891d92)"
    assert second == "객관식 60문항 · 학번 8자리 (자동, fbf823)"
    assert form_summary(sixty) == "객관식 60문항(1~20, 21~40, 41~60) · 학번 8자리"


def test_an_imported_form_keeps_its_own_name() -> None:
    profile = _profile((20, 20, 20), "중간고사 양식")

    assert form_label(profile, "중간고사.omrtemplate") == "중간고사 양식 · 객관식 60문항"


def test_a_detected_form_without_the_usual_file_name_still_reads() -> None:
    assert form_label(_profile((20, 20, 20)), "renamed.omrtemplate") == (
        "객관식 60문항 · 학번 8자리 (자동)"
    )
