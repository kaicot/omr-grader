from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import fitz
import numpy as np
import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.profile import parse_profile_bytes
from omr_grader.infrastructure import form_detection
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.form_detection import FormDetection, FormDetector
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore
from omr_grader.recognition.form_alignment import align_page
from omr_grader.recognition.form_profile import FRAME_BUBBLE_RADIUS
from tests.helpers.omr_engine import reference_sheet
from tests.helpers.synthetic_omr import (
    encode_png,
    render_sheet_with_geometry,
    sample_answers,
    to_gray,
    write_png,
)

ANSWER_BLOCKS = ((1, 20), (21, 20), (41, 20), (61, 20), (81, 20))
SUMMARY = "학번 8자리 · 객관식 100문항 (1~20, 21~40, 41~60, 61~80, 81~100)"


def _store(root: Path) -> ProfileStore:
    paths = ManagedPaths.from_root(root)
    paths.profiles_dir.mkdir()
    return ProfileStore(paths, CapabilityToken.for_testing(root))


@dataclass(frozen=True, slots=True)
class Scans:
    """Synthetic scans of two exam forms, written as PNG files."""

    folder: Path
    first: Path
    second: Path
    sideways: Path
    short_form: Path
    blank: Path
    pdf: Path


@dataclass(frozen=True, slots=True)
class SavedForm:
    """A store holding the profile generated from the two scans of the 100-question form."""

    store: ProfileStore
    detector: FormDetector
    first_detection: FormDetection
    stored_name: str


@pytest.fixture(scope="module")
def scans(tmp_path_factory: pytest.TempPathFactory) -> Scans:
    folder = tmp_path_factory.mktemp("scans")
    sheet = reference_sheet()
    other_page, _ = render_sheet_with_geometry(
        sample_answers(), "20250002", rotation=0.8, shift=(20.0, -10.0), seed=3
    )
    sideways, _ = render_sheet_with_geometry(sample_answers(), "20250003", rotation=90, seed=4)
    pdf = folder / "scan.pdf"
    document = fitz.open()
    document.new_page(width=842, height=595).insert_image(
        fitz.Rect(0, 0, 842, 595), stream=encode_png(sheet.image)
    )
    document.save(str(pdf))
    document.close()
    return Scans(
        folder,
        write_png(folder / "first.png", sheet.gray),
        write_png(folder / "second.png", to_gray(other_page)),
        write_png(folder / "sideways.png", to_gray(sideways)),
        write_png(folder / "short.png", reference_sheet((20, 20, 10)).gray),
        write_png(folder / "blank.png", np.full((600, 900), 245, dtype=np.uint8)),
        pdf,
    )


@pytest.fixture(scope="module")
def saved(tmp_path_factory: pytest.TempPathFactory, scans: Scans) -> SavedForm:
    store = _store(tmp_path_factory.mktemp("portable"))
    detector = FormDetector(store)
    detected = detector.detect((str(scans.first), str(scans.second)))
    assert isinstance(detected, Ok)
    assert detected.value.generated_profile is not None
    stored = store.save_generated(
        detected.value.generated_profile, detected.value.suggested_filename
    )
    assert isinstance(stored, Ok)
    return SavedForm(store, detector, detected.value, stored.value.stored_name)


def test_the_first_detection_describes_a_new_form_and_offers_its_profile(
    saved: SavedForm,
) -> None:
    detection = saved.first_detection

    assert detection.is_new
    assert detection.profile_filename is None
    assert detection.id_digits == 8
    assert detection.question_count == 100
    assert detection.answer_blocks == ANSWER_BLOCKS
    assert (detection.pages_checked, detection.pages_matching) == (2, 2)
    assert detection.summary == SUMMARY
    assert re.fullmatch(
        r"자동양식_객관식100문항_[0-9a-f]{6}\.omrtemplate", detection.suggested_filename
    )
    assert detection.generated_profile is not None
    profile = parse_profile_bytes(detection.generated_profile)
    assert isinstance(profile, Ok)
    starts = [region.question_start for region in profile.value.answer_regions]
    assert starts == [start for start, _ in ANSWER_BLOCKS]
    # The preview is the first page, long side 1600, with the found bubbles drawn on it.
    preview = cv2.imdecode(np.frombuffer(detection.preview_png, np.uint8), cv2.IMREAD_COLOR)
    assert preview.shape == (1131, 1600, 3)
    assert not np.array_equal(preview[..., 0], preview[..., 1])  # colored marks were added


def test_saving_the_generated_profile_stores_it_beside_the_portable_data(saved: SavedForm) -> None:
    detection = saved.first_detection
    stored = saved.store.paths.profiles_dir / saved.stored_name

    assert saved.stored_name == detection.suggested_filename
    assert stored.read_bytes() == detection.generated_profile
    assert saved.store.discover() == Ok((saved.stored_name,))


def test_the_saved_profile_is_found_again_instead_of_generating_a_new_one(
    saved: SavedForm, scans: Scans
) -> None:
    again = saved.detector.detect((str(scans.first),))

    assert isinstance(again, Ok)
    detection = again.value
    assert not detection.is_new
    assert detection.profile_filename == saved.stored_name
    assert detection.generated_profile is None
    assert (detection.question_count, detection.answer_blocks) == (100, ANSWER_BLOCKS)
    assert (detection.pages_checked, detection.pages_matching) == (1, 1)
    assert detection.summary == SUMMARY


def test_a_page_scanned_sideways_matches_the_saved_profile_and_previews_upright(
    saved: SavedForm, scans: Scans
) -> None:
    result = saved.detector.detect((str(scans.sideways),))

    assert isinstance(result, Ok)
    assert result.value.profile_filename == saved.stored_name
    preview = cv2.imdecode(np.frombuffer(result.value.preview_png, np.uint8), cv2.IMREAD_COLOR)
    assert preview.shape[1] > preview.shape[0]  # landscape again, not the sideways scan


def test_every_image_in_a_folder_is_sampled_and_other_files_are_ignored(
    saved: SavedForm, scans: Scans, tmp_path: Path
) -> None:
    (tmp_path / "scan1.png").write_bytes(scans.first.read_bytes())
    (tmp_path / "notes.txt").write_text("not a scan")

    result = saved.detector.detect((str(tmp_path),))

    assert isinstance(result, Ok)
    assert result.value.pages_checked == 1
    assert result.value.profile_filename == saved.stored_name


def test_the_pages_of_a_pdf_are_rendered_and_detected(saved: SavedForm, scans: Scans) -> None:
    result = saved.detector.detect((str(scans.pdf),))

    assert isinstance(result, Ok)
    assert result.value.question_count == 100
    assert result.value.pages_checked == 1
    assert result.value.profile_filename == saved.stored_name


def test_only_the_first_pages_of_a_large_selection_are_sampled(
    saved: SavedForm, scans: Scans, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(form_detection, "MAX_SAMPLE_PAGES", 1)

    result = saved.detector.detect((str(scans.first), str(scans.second), str(scans.sideways)))

    assert isinstance(result, Ok)
    assert result.value.pages_checked == 1


def test_a_page_of_another_form_among_the_samples_is_counted_but_does_not_block_reuse(
    saved: SavedForm, scans: Scans
) -> None:
    selection = (str(scans.first), str(scans.second), str(scans.short_form))

    result = saved.detector.detect(selection)

    assert isinstance(result, Ok)
    detection = result.value
    assert (detection.pages_checked, detection.pages_matching) == (3, 2)
    assert detection.question_count == 100  # the form most pages have
    # Only pages of the detected form are fitted against saved profiles: the short page
    # is counted as different and sent to review when read, but the saved form is reused.
    assert detection.profile_filename == saved.stored_name
    assert detection.generated_profile is None


def test_a_saved_profile_that_fits_only_loosely_is_not_reused(
    saved: SavedForm, scans: Scans, tmp_path: Path
) -> None:
    # Like a hand-drawn template of the same form: each block drawn up to 0.2 radius off in
    # its own direction, close enough that every bubble still pairs with its printed ring.
    wire = json.loads((saved.store.paths.profiles_dir / saved.stored_name).read_bytes())
    width, height = wire["page"]["source_width"], wire["page"]["source_height"]
    offsets = ((0.2, 0.0), (-0.2, 0.0), (0.0, 0.2), (0.0, -0.2), (0.15, 0.15), (-0.15, -0.15))
    for region, (dx, dy) in zip(wire["regions"], offsets, strict=True):
        box = region["bbox_ratio"]
        box["x"] = round(box["x"] + dx * FRAME_BUBBLE_RADIUS / width, 8)
        box["y"] = round(box["y"] + dy * FRAME_BUBBLE_RADIUS / height, 8)
    store = _store(tmp_path)
    drawn = store.paths.profiles_dir / "hand_drawn.omrtemplate"
    drawn.write_text(json.dumps(wire), encoding="utf-8")
    loaded = store.load(drawn.name)
    assert isinstance(loaded, Ok)
    page = cv2.imdecode(np.fromfile(str(scans.first), np.uint8), cv2.IMREAD_GRAYSCALE)
    alignment = align_page(page, loaded.value)
    # It would pass the structure and coverage checks; only its precision rules it out.
    assert alignment is not None and alignment.inlier_ratio >= 0.75
    assert alignment.residual / alignment.bubble_radius > 0.1

    result = FormDetector(store).detect((str(scans.first),))

    assert isinstance(result, Ok)
    assert result.value.is_new
    assert result.value.generated_profile is not None


def test_a_saved_profile_is_reused_on_pages_with_feed_wobble(
    saved: SavedForm, scans: Scans, tmp_path: Path
) -> None:
    # A sideways wobble of about 0.5 mm no profile can follow: the saved one fits as well as
    # a profile built from the wobbly page itself, so it is reused instead of asking again.
    page = cv2.imdecode(np.fromfile(str(scans.first), np.uint8), cv2.IMREAD_GRAYSCALE)
    height, width = page.shape
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    wobbly = cv2.remap(
        page, xs + 6.0 * np.sin(2 * np.pi * ys / 700.0), ys, cv2.INTER_LINEAR, borderValue=245
    )
    path = write_png(tmp_path / "wobbly.png", wobbly)

    result = saved.detector.detect((str(path),))

    assert isinstance(result, Ok)
    assert result.value.profile_filename == saved.stored_name


def test_a_fresh_profile_its_own_pages_do_not_trust_is_never_offered(
    scans: Scans, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_align = form_detection.align_page

    def one_block_off(gray: np.ndarray, profile: object) -> object:
        fit = real_align(gray, profile)  # type: ignore[arg-type]
        return None if fit is None else replace(fit, shifted_regions=1)

    monkeypatch.setattr(form_detection, "align_page", one_block_off)

    result = FormDetector(_store(tmp_path)).detect((str(scans.first),))

    assert isinstance(result, Err)
    assert result.errors[0].code == "FORM_GEOMETRY_INVALID"


@pytest.mark.parametrize("empty_cells", (0, 1))
def test_label_rows_above_the_blocks_are_left_out_and_reported(
    tmp_path: Path, empty_cells: int
) -> None:
    # Three pages print "No." in the label row's number cell; one more page may print
    # the label row with an empty cell (which the one-page check already drops).
    folder = tmp_path / "scans"
    folder.mkdir()
    paths = []
    for page in range(3 + empty_cells):
        answers = {q: (q + page) % 5 + 1 for q in range(1, 101)}
        label = "No." if page < 3 else None
        image, _ = render_sheet_with_geometry(
            answers, "20261234", seed=page, header_rings=True, header_label=label
        )
        paths.append(str(write_png(folder / f"page{page}.png", to_gray(image))))

    result = FormDetector(_store(tmp_path)).detect(tuple(paths))

    assert isinstance(result, Ok)
    detection = result.value
    assert detection.question_count == 100
    assert detection.answer_blocks == ANSWER_BLOCKS
    assert detection.dropped_header_rows == 5
    assert detection.generated_profile is not None
    profile = parse_profile_bytes(detection.generated_profile)
    assert isinstance(profile, Ok)
    page = cv2.imdecode(np.fromfile(paths[1], np.uint8), cv2.IMREAD_GRAYSCALE)
    fit = align_page(page, profile.value)
    assert fit is not None and fit.trusted


def test_a_saved_profile_wins_over_a_first_question_nobody_answered(
    saved: SavedForm, tmp_path: Path
) -> None:
    # Every sampled student left question 1 blank (a voided question, say). Dropping that
    # row is only ever considered for a new form; the saved one of the printed structure stays.
    paths = []
    for page in range(3):
        answers = {q: (q + page) % 5 + 1 for q in range(2, 101)}
        image, _ = render_sheet_with_geometry(answers, "20261234", seed=page)
        paths.append(str(write_png(tmp_path / f"voided{page}.png", to_gray(image))))

    result = saved.detector.detect(tuple(paths))

    assert isinstance(result, Ok)
    assert result.value.profile_filename == saved.stored_name
    assert result.value.question_count == 100
    assert result.value.dropped_header_rows == 0


def test_a_form_of_another_structure_gets_its_own_profile(
    saved: SavedForm, scans: Scans, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    (store.paths.profiles_dir / saved.stored_name).write_bytes(
        (saved.store.paths.profiles_dir / saved.stored_name).read_bytes()
    )
    detector = FormDetector(store)

    short = detector.detect((str(scans.short_form),))
    assert isinstance(short, Ok)
    assert short.value.is_new  # the saved 100-question profile does not fit
    assert short.value.question_count == 50
    assert short.value.answer_blocks == ((1, 20), (21, 20), (41, 10))
    assert short.value.summary == "학번 8자리 · 객관식 50문항 (1~20, 21~40, 41~50)"
    assert short.value.generated_profile is not None
    stored = store.save_generated(short.value.generated_profile, short.value.suggested_filename)
    assert isinstance(stored, Ok)

    again = detector.detect((str(scans.short_form),))
    full = detector.detect((str(scans.first),))
    assert isinstance(again, Ok) and isinstance(full, Ok)
    assert again.value.profile_filename == stored.value.stored_name
    assert full.value.profile_filename == saved.stored_name
    assert stored.value.stored_name != saved.stored_name


def test_unreadable_selections_are_reported(saved: SavedForm, scans: Scans, tmp_path: Path) -> None:
    empty_folder = tmp_path / "empty"
    empty_folder.mkdir()

    for selection in ((), (str(tmp_path / "missing.png"),), (str(empty_folder),)):
        result = saved.detector.detect(selection)
        assert isinstance(result, Err)
        assert result.errors[0].code == "SCAN_SOURCE_EMPTY"

    blank = saved.detector.detect((str(scans.blank),))
    assert isinstance(blank, Err)
    assert blank.errors[0].code == "FORM_NOT_FOUND"
    assert blank.errors[0].message_key == "error.form_not_found"


def test_a_form_the_app_cannot_grade_is_reported_instead_of_saved(
    saved: SavedForm, tmp_path: Path
) -> None:
    narrow, _ = render_sheet_with_geometry({}, "", id_columns=5)

    result = saved.detector.detect((str(write_png(tmp_path / "narrow.png", to_gray(narrow))),))

    assert isinstance(result, Err)
    assert result.errors[0].code == "FORM_ID_UNSUPPORTED"


def test_a_detection_reads_as_a_summary_and_knows_whether_it_is_new() -> None:
    new = FormDetection(
        None, b"{}", "x.omrtemplate", 8, 50, ((1, 20), (21, 20), (41, 10)), 3, 3, b""
    )
    known = FormDetection("x.omrtemplate", None, "x.omrtemplate", 8, 5, ((1, 5),), 1, 1, b"")

    assert new.is_new and not known.is_new
    assert new.summary == "학번 8자리 · 객관식 50문항 (1~20, 21~40, 41~50)"
    assert known.summary == "학번 8자리 · 객관식 5문항 (1~5)"
