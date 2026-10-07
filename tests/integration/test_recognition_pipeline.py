from __future__ import annotations

import json
import struct
from dataclasses import replace
from decimal import Decimal
from functools import cache

import cv2
import numpy as np
import pytest

from omr_grader.domain.enums import AnswerStatus, ProcessingStatus, StudentIdStatus
from omr_grader.domain.errors import ErrorInfo, Ok
from omr_grader.domain.profile import Profile, parse_profile_bytes
from omr_grader.recognition.bubbles import find_bubbles
from omr_grader.recognition.form_layout import (
    detect_layout,
    drop_unmarked_header_rows,
    layout_from_bubbles,
)
from omr_grader.recognition.form_profile import build_profile
from omr_grader.recognition.pipeline import (
    PipelineFailure,
    PipelineInput,
    PipelineResult,
    PipelineSuccess,
    RecognitionArtifacts,
    recognize_page,
)
from tests.helpers.omr_engine import (
    ReferenceSheet,
    make_page_ref,
    make_thresholds,
    profile_from_page,
    reference_sheet,
)
from tests.helpers.synthetic_omr import (
    SPECK_RADIUS,
    choices_of,
    encode_jpeg,
    encode_png,
    paint_mark,
    render_sheet_with_geometry,
    sample_answers,
)

NORMAL, BLANK, MULTIPLE = AnswerStatus.NORMAL, AnswerStatus.BLANK, AnswerStatus.MULTIPLE
UNCERTAIN, UNASKED = AnswerStatus.UNCERTAIN, AnswerStatus.UNASKED
TURNS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _classic_tiff_chain(frame_count: int) -> bytes:
    ifd_size = 30
    offsets = [8 + index * ifd_size for index in range(frame_count)]
    payload = bytearray(b"II*\0" + offsets[0].to_bytes(4, "little"))
    for index, offset in enumerate(offsets):
        assert len(payload) == offset
        payload.extend((2).to_bytes(2, "little"))
        for tag, value in ((256, 5), (257, 3)):
            payload.extend(tag.to_bytes(2, "little"))
            payload.extend((4).to_bytes(2, "little"))
            payload.extend((1).to_bytes(4, "little"))
            payload.extend(value.to_bytes(4, "little"))
        payload.extend((offsets[index + 1] if index + 1 < frame_count else 0).to_bytes(4, "little"))
    return bytes(payload)


def _big_tiff(width: int, height: int) -> bytes:
    payload = bytearray(b"II+\0\x08\0\0\0" + (16).to_bytes(8, "little"))
    payload.extend((2).to_bytes(8, "little"))
    for tag, value in ((256, width), (257, height)):
        payload.extend(tag.to_bytes(2, "little"))
        payload.extend((4).to_bytes(2, "little"))
        payload.extend((1).to_bytes(8, "little"))
        payload.extend(value.to_bytes(4, "little") + b"\0" * 4)
    payload.extend(b"\0" * 8)
    return bytes(payload)


def _png_header(width: int, height: int) -> bytes:
    """A PNG signature and IHDR that declare a size; there is no image data behind it."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\0\0\0"
    )


@cache
def _png(layout: tuple[int, ...] = (20, 20, 20, 20, 20)) -> bytes:
    return encode_png(reference_sheet(layout).image)


def _task(encoded: bytes, profile: Profile, **options: object) -> PipelineInput:
    thresholds = make_thresholds(**options)  # type: ignore[arg-type]
    return PipelineInput(make_page_ref(), encoded, profile, thresholds)


def _success(result: PipelineResult) -> PipelineSuccess:
    assert isinstance(result, PipelineSuccess), getattr(result, "failure", result)
    return result


@cache
def _upright() -> PipelineSuccess:
    return _success(recognize_page(_task(_png(), reference_sheet().profile)))


def _expected(sheet: ReferenceSheet) -> list[tuple[AnswerStatus, tuple[int, ...]]]:
    """What reading the sheet must give for each of the 100 question slots."""
    rows: list[tuple[AnswerStatus, tuple[int, ...]]] = []
    for question in range(1, 101):
        if question > sheet.geometry.question_count:
            rows.append((UNASKED, ()))
            continue
        choices = choices_of(sheet.answers.get(question))
        rows.append((NORMAL if len(choices) == 1 else MULTIPLE if choices else BLANK, choices))
    return rows


def _seen(result: PipelineSuccess) -> list[tuple[AnswerStatus, tuple[int, ...]]]:
    return [(answer.value.status, answer.value.choices) for answer in result.page.answers]


def test_an_upright_page_reads_with_the_exact_answers_and_student_id() -> None:
    sheet = reference_sheet()

    result = _upright()

    page = result.page
    assert page.processing_status is ProcessingStatus.PROCESSED
    assert page.rotation_degrees == 0
    assert page.student_id.status is StudentIdStatus.NORMAL
    assert page.student_id.value == sheet.student_id
    assert _seen(result) == _expected(sheet)
    assert len(page.answers) == 100
    assert len(page.evidence) == 580
    assert page.errors == ()
    assert page.page_ref == make_page_ref()


def test_the_page_reports_its_geometry_and_how_well_it_fit() -> None:
    page = _upright().page
    profile = reference_sheet().profile
    assert profile.page is not None

    assert page.normalized_size == (profile.page.source_width, profile.page.source_height)
    assert page.homography_forward is not None and len(page.homography_forward) == 9
    assert page.homography_inverse is not None and len(page.homography_inverse) == 9
    assert page.normalization_confidence == "1"  # every printed bubble sat on the profile
    assert page.orientation_confidence is not None
    assert Decimal("0.5") < Decimal(page.orientation_confidence) <= 1


@pytest.mark.parametrize("turn", (90, 180, 270))
def test_a_page_turned_by_a_right_angle_reads_the_same_with_the_turn_that_uprights_it(
    turn: int,
) -> None:
    sheet = reference_sheet()
    turned = np.asarray(cv2.rotate(sheet.image, TURNS[turn]), dtype=np.uint8)

    result = _success(recognize_page(_task(encode_png(turned), sheet.profile)))

    assert result.page.processing_status is ProcessingStatus.PROCESSED
    assert result.page.rotation_degrees == (360 - turn) % 360
    assert _seen(result) == _expected(sheet) == _seen(_upright())
    assert result.page.student_id.value == sheet.student_id
    normalized = cv2.imdecode(
        np.frombuffer(result.artifacts.normalized_jpeg, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    assert normalized.shape[1] > normalized.shape[0]  # landscape again


def test_a_tilted_shifted_jpeg_scan_in_dim_light_still_reads_exactly() -> None:
    sheet = reference_sheet()
    image, _ = render_sheet_with_geometry(
        sheet.answers, sheet.student_id, rotation=1.5, scale=0.98, shift=(30.0, -20.0), seed=3
    )
    dim = np.asarray(cv2.convertScaleAbs(image, alpha=0.8), dtype=np.uint8)

    result = _success(recognize_page(_task(encode_jpeg(dim, 60), sheet.profile)))

    assert result.page.processing_status is ProcessingStatus.PROCESSED
    assert result.page.rotation_degrees == 0
    assert _seen(result) == _expected(sheet)
    assert result.page.student_id.value == sheet.student_id


def test_small_dots_read_like_pen_marks_and_a_speck_goes_to_review() -> None:
    sheet = reference_sheet()
    image, geometry = render_sheet_with_geometry(
        sheet.answers, sheet.student_id, mark_style="dot", seed=5
    )
    paint_mark(image, geometry.answer_center(11, 4), SPECK_RADIUS)  # question 11 is left blank

    result = _success(recognize_page(_task(encode_png(image), sheet.profile)))

    seen, expected = _seen(result), _expected(sheet)
    assert result.page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW
    assert result.page.student_id.value == sheet.student_id
    assert seen[10] == (UNCERTAIN, (4,))
    assert seen[:10] + seen[11:] == expected[:10] + expected[11:]


def test_a_fifty_question_profile_leaves_the_rest_of_the_slots_unasked() -> None:
    sheet = reference_sheet((20, 20, 10))

    result = _success(recognize_page(_task(_png((20, 20, 10)), sheet.profile)))

    page = result.page
    assert page.processing_status is ProcessingStatus.PROCESSED
    assert page.student_id.value == sheet.student_id
    assert _seen(result) == _expected(sheet)
    assert [a.value.status for a in page.answers[:50]].count(UNASKED) == 0
    unasked = page.answers[50:]
    assert [answer.question for answer in unasked] == list(range(51, 101))
    assert {answer.value.status for answer in unasked} == {UNASKED}
    assert all(
        cell.pixel_rect is None and cell.ratio_rect is None for a in unasked for cell in a.cells
    )
    assert all(cell.fill_score is None for answer in unasked for cell in answer.cells)
    assert [cell.index for cell in page.evidence] == list(range(580))
    assert json.loads(result.artifacts.coordinates_json) == page.to_dict()


def test_a_page_of_a_smaller_form_fits_only_partly_and_is_sent_to_review() -> None:
    page_of_fifty = reference_sheet((20, 20, 10))
    profile_of_hundred = reference_sheet().profile

    result = _success(recognize_page(_task(_png((20, 20, 10)), profile_of_hundred)))

    page = result.page
    assert page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW
    assert Decimal("0.5") < Decimal(page.normalization_confidence or "0") < Decimal("0.65")
    # Nothing is confirmed from a page that only partly fits; what was seen stays as evidence.
    assert {answer.value.status for answer in page.answers} == {UNCERTAIN}
    assert page.student_id.status is StudentIdStatus.INVALID
    assert page.student_id.value is None
    assert page.answers[0].value.choices == choices_of(page_of_fifty.answers[1])


def test_a_page_printed_with_more_bubbles_than_the_profile_describes_is_sent_to_review() -> None:
    """Engine gap reproducer (plan 3.2: a page of another form mixed into the batch goes to review).

    The 100-question page is aligned with the 50-question profile on every cell the profile
    has, so nothing marks it as a stranger: it is read as PROCESSED while questions 51-100
    silently become unasked. The reverse mix (a short page against the long profile) is
    already sent to review.
    """
    sheet = reference_sheet()
    profile_of_fifty = reference_sheet((20, 20, 10)).profile

    result = _success(recognize_page(_task(_png(), profile_of_fifty)))

    page = result.page
    assert page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW, (
        f"processed with {sheet.geometry.question_count - 50} printed questions ignored"
    )
    # Grading ignores the page status: every printed question must be withheld itself.
    assert {answer.value.status for answer in page.answers[:50]} == {UNCERTAIN}
    assert {answer.value.status for answer in page.answers[50:]} == {UNASKED}
    assert page.student_id.status is StudentIdStatus.INVALID
    assert page.student_id.value is None


@pytest.mark.parametrize(("axis", "steps"), (("x", 1), ("x", -1), ("y", 1), ("y", -1)))
def test_a_profile_block_drawn_one_bubble_off_withholds_every_value(axis: str, steps: int) -> None:
    # Every other block still fits, so coverage, residual and rotation checks all pass.
    sheet = reference_sheet()
    wire = json.loads(sheet.payload)
    block = next(region for region in wire["regions"] if region.get("question_start") == 81)
    box, grid = block["bbox_ratio"], block["grid"]
    pitch = box["w"] / grid["cols"] if axis == "x" else box["h"] / grid["rows"]
    box[axis] = round(box[axis] + steps * pitch, 8)
    parsed = parse_profile_bytes(json.dumps(wire).encode())
    assert isinstance(parsed, Ok)

    result = _success(recognize_page(_task(_png(), parsed.value)))

    page = result.page
    assert page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW
    assert {answer.value.status for answer in page.answers} == {UNCERTAIN}
    assert page.student_id.status is StudentIdStatus.INVALID


@pytest.mark.parametrize("turn", (0, 180))
def test_a_form_with_column_label_rings_reads_every_question_under_its_own_number(
    turn: int,
) -> None:
    # The label rings stay on every page; the profile leaves them out and pages still fit.
    answers = sample_answers()
    image, _ = render_sheet_with_geometry(answers, "20261234", header_rings=True)
    profile, _, layout = profile_from_page(image, "labelled")
    assert [block.rows for block in layout.answer_blocks] == [20] * 5
    page = image if turn == 0 else cv2.rotate(image, TURNS[turn])

    result = _success(recognize_page(_task(encode_png(page), profile)))

    assert result.page.processing_status is ProcessingStatus.PROCESSED
    assert result.page.student_id.value == "20261234"
    seen = {a.question: a.value.choices for a in result.page.answers if a.value.status is NORMAL}
    expected = {q: choices_of(c) for q, c in answers.items() if len(choices_of(c)) == 1}
    assert seen == expected


def test_a_mark_in_a_printed_row_the_profile_left_out_withholds_the_page() -> None:
    # Three sampled sheets all left question 1 blank, so the new profile took its row for a
    # header. A sheet that does mark question 1 shows the profile is missing a question.
    samples = []
    for page in range(3):
        answers = {q: (q + page) % 5 + 1 for q in range(2, 101)}
        image, _ = render_sheet_with_geometry(answers, "20261234", seed=page)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        layout = detect_layout(gray)
        assert layout is not None
        samples.append((layout, gray))
    layouts, dropped = drop_unmarked_header_rows(samples)
    assert dropped == 1
    size = (samples[0][1].shape[1], samples[0][1].shape[0])
    built = build_profile([(layout, size) for layout in layouts], "first row dropped")
    assert isinstance(built, Ok)
    marked, _ = render_sheet_with_geometry(sample_answers(), "20261234", seed=9)

    result = _success(recognize_page(_task(encode_png(marked), built.value[0])))

    page = result.page
    assert page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW
    assert {a.value.status for a in page.answers if a.value.status is not UNASKED} == {UNCERTAIN}
    assert page.student_id.status is StudentIdStatus.INVALID


@pytest.mark.parametrize("gap", (2.0, 3.0))
def test_stacked_blocks_with_empty_rows_between_them_read_under_their_numbers(
    gap: float,
) -> None:
    answers = {question: (question * 3) % 5 + 1 for question in range(1, 33)}
    image, _ = render_sheet_with_geometry(
        answers, "20261234", layout=((8, 8), (8, 8)), stack_gap_rows=gap
    )
    profile, _, layout = profile_from_page(image, "stacked")
    assert layout.question_count == 32

    result = _success(recognize_page(_task(encode_png(image), profile)))

    assert result.page.processing_status is ProcessingStatus.PROCESSED
    seen = {a.question: a.value.choices for a in result.page.answers if a.value.status is NORMAL}
    assert seen == {question: (choice,) for question, choice in answers.items()}


def test_label_rings_printed_dark_are_not_taken_for_a_student_mark() -> None:
    # Filled column labels make the whole row dark; a student marks one bubble of a row.
    answers = sample_answers()
    image, geometry = render_sheet_with_geometry(answers, "20261234", header_rings=True)
    profile, _, _ = profile_from_page(image, "labelled")
    dark = image.copy()
    for nodes in geometry.block_nodes:
        for column in range(5):
            above = nodes[0, column] - (nodes[1, column] - nodes[0, column])
            paint_mark(dark, (float(above[0]), float(above[1])))

    result = _success(recognize_page(_task(encode_png(dark), profile)))

    assert result.page.processing_status is ProcessingStatus.PROCESSED
    assert result.page.student_id.value == "20261234"


def test_a_profile_that_is_upside_down_withholds_every_value() -> None:
    sheet = reference_sheet()
    upside_down = cv2.rotate(sheet.gray, cv2.ROTATE_180)
    bubbles = find_bubbles(upside_down)
    assert bubbles is not None
    # Measured without turning the page upright, as an unoriented detector would have done.
    layout = layout_from_bubbles(bubbles, upside_down)
    assert layout is not None and layout.rotation == 0
    built = build_profile([(layout, (upside_down.shape[1], upside_down.shape[0]))], "mirrored")
    assert isinstance(built, Ok)
    mirrored = built.value[0]

    result = _success(recognize_page(_task(_png(), mirrored)))

    page = result.page
    assert page.rotation_degrees == 180  # the page fits the mirrored profile when turned over
    assert page.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW
    assert {answer.value.status for answer in page.answers} == {UNCERTAIN}
    assert page.student_id.status is StudentIdStatus.INVALID
    assert page.student_id.value is None


@pytest.mark.parametrize("kind", ("blank", "text", "circles"))
def test_an_image_that_is_not_the_form_is_not_found(kind: str) -> None:
    sheet = reference_sheet()
    image = np.full((2480, 3508, 3), 245, dtype=np.uint8)
    rng = np.random.default_rng(1)
    if kind == "text":
        for row in range(120, 2400, 75):
            cv2.putText(image, "lorem ipsum dolor sit amet", (100, row), 0, 1.6, (30, 30, 30), 2)
    if kind == "circles":
        for _ in range(500):
            center = (int(rng.integers(60, 3400)), int(rng.integers(60, 2400)))
            cv2.circle(image, center, 22, (110, 110, 110), 3, cv2.LINE_AA)
    task = _task(encode_png(image), sheet.profile)

    result = recognize_page(task)

    assert isinstance(result, PipelineFailure)
    assert result.page.processing_status is ProcessingStatus.UNPROCESSABLE
    assert result.failure.errors[0].code == "FORM_NOT_FOUND"
    assert result.failure.errors[0].message_key == "error.form_not_found"
    assert result.failure.errors[0].context["manual_review"] is True
    assert result.failure.page_ref == result.page.page_ref == task.page_ref
    assert result.page.answers == () and result.page.evidence == ()
    assert result.page.rotation_degrees is None


def test_artifacts_are_unpublished_bytes_that_describe_the_page() -> None:
    result = _upright()
    artifacts = result.artifacts
    profile = reference_sheet().profile
    assert profile.page is not None

    normalized = cv2.imdecode(np.frombuffer(artifacts.normalized_jpeg, np.uint8), cv2.IMREAD_COLOR)
    overlay = cv2.imdecode(np.frombuffer(artifacts.overlay_jpeg, np.uint8), cv2.IMREAD_COLOR)

    assert normalized.shape[:2] == (profile.page.source_height, profile.page.source_width)
    assert overlay.shape == normalized.shape
    # Stored as JPEG, so the 01원본스캔 page is a fraction of an uncompressed PNG.
    assert artifacts.normalized_jpeg.startswith(b"\xff\xd8\xff")
    assert artifacts.overlay_jpeg.startswith(b"\xff\xd8\xff")
    assert not np.array_equal(normalized, overlay)  # the overlay draws the evidence boxes
    assert json.loads(artifacts.coordinates_json) == result.page.to_dict()
    assert artifacts.coordinates_json.endswith(b"\n")
    assert result.page.evidence == tuple(
        cell for item in result.page.student_id.cells for cell in item.candidates
    ) + tuple(cell for answer in result.page.answers for cell in answer.cells)


def test_the_pipeline_is_deterministic() -> None:
    sheet = reference_sheet()

    again = _success(recognize_page(_task(_png(), sheet.profile)))

    first = _upright()
    assert again.page == first.page
    assert again.artifacts == first.artifacts


def test_pipeline_rejects_tampered_frozen_profiles_before_decoding() -> None:
    profile = reference_sheet().profile
    white = encode_png(np.full((40, 60, 3), 255, dtype=np.uint8))
    empty = replace(profile, regions=())
    invalid_region = replace(
        profile.regions[0], bbox_ratio=replace(profile.regions[0].bbox_ratio, w=Decimal("0"))
    )
    invalid = replace(profile, regions=(invalid_region, *profile.regions[1:]))
    no_frame = replace(profile, page=None)

    for tampered in (empty, invalid, no_frame):
        result = recognize_page(_task(white, tampered))
        assert isinstance(result, PipelineFailure)
        assert result.failure.errors[0].code == "INVALID_FROZEN_PROFILE"


def test_a_corrupt_image_body_fails_to_decode_and_a_tiny_one_is_not_the_form() -> None:
    profile = reference_sheet().profile

    corrupt = recognize_page(_task(_png_header(60, 40) + b"not image data", profile))
    tiny = recognize_page(_task(encode_png(np.full((40, 60, 3), 255, dtype=np.uint8)), profile))

    assert isinstance(corrupt, PipelineFailure)
    assert corrupt.failure.errors[0].code == "IMAGE_DECODE_FAILED"
    assert isinstance(tiny, PipelineFailure)
    assert tiny.failure.errors[0].code == "FORM_NOT_FOUND"


def test_the_pipeline_input_checks_headers_sizes_and_types() -> None:
    profile = reference_sheet().profile
    thresholds = make_thresholds()
    page_ref = make_page_ref()

    for payload in (b"not an image", b"\x89PNG\r\n\x1a\n", b"BM", b"\xff\xd8\xff"):
        with pytest.raises(ValueError, match="header is invalid"):
            PipelineInput(page_ref, payload, profile, thresholds)
    for width, height in ((40_000, 10), (10, 40_000), (0, 10), (20_000, 20_000)):
        with pytest.raises(ValueError, match="header is invalid or exceeds"):
            PipelineInput(page_ref, _png_header(width, height), profile, thresholds)
    with pytest.raises(ValueError, match="incomplete"):
        PipelineInput(page_ref, b"", profile, thresholds)
    with pytest.raises(TypeError):
        PipelineInput(page_ref, "text", profile, thresholds)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PipelineInput("page", b"x", profile, thresholds)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="incomplete"):
        PipelineInput(page_ref, b"x", "profile", thresholds)  # type: ignore[arg-type]


def test_pipeline_tiff_worker_preflight_accepts_bigtiff_and_rejects_excess_frames() -> None:
    profile = reference_sheet().profile
    task = _task(_big_tiff(17, 19), profile)
    assert task.encoded_raster.startswith(b"II+\0")

    with pytest.raises(ValueError, match="TIFF_FRAME_QUOTA"):
        _task(_classic_tiff_chain(257), profile)


def test_a_tiff_that_passes_preflight_but_cannot_be_decoded_is_a_page_failure() -> None:
    result = recognize_page(_task(_big_tiff(17, 19), reference_sheet().profile))

    assert isinstance(result, PipelineFailure)
    assert result.failure.errors[0].code == "IMAGE_DECODE_FAILED"


def test_results_and_artifacts_refuse_inconsistent_contents() -> None:
    success = _upright()
    failure = recognize_page(_task(_big_tiff(17, 19), reference_sheet().profile))
    assert isinstance(failure, PipelineFailure)

    with pytest.raises(ValueError, match="recognized page"):
        PipelineSuccess(failure.page, success.artifacts)
    with pytest.raises(ValueError, match="failed page"):
        PipelineFailure(success.page, failure.failure)
    other = replace(failure.failure, errors=(ErrorInfo("OTHER", "error.other"),))
    with pytest.raises(ValueError, match="diagnostics"):
        PipelineFailure(failure.page, other)
    for artifacts in ((b"", b"x", b"x"), (b"x", b"x", ""), (b"x", None, b"x")):
        with pytest.raises(ValueError, match="nonempty bytes"):
            RecognitionArtifacts(*artifacts)  # type: ignore[arg-type]
