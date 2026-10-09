"""Run the v4 recognizer on the local exam baseline and compare it with the verified truth.

usage: python tools/v4_baseline.py [--baseline DIR] [--sensitivity N] [--part-profile]

The baseline folder (default: ``$OMR_V4_BASELINE_DIR``) holds ``baseline.json`` and
``truth.json``. It contains real student data and never belongs in the repository.
For every part the form profile is detected from that part's own scans (as the app
does) and, with ``--part-profile``, also from the other part, so a profile learned on
one exam is checked against another. Exit status 1 when any answer or ID disagrees.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import fitz
import numpy as np

SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from omr_grader.domain.enums import AnswerStatus, ProcessingStatus, SourceKind  # noqa: E402
from omr_grader.domain.errors import Err  # noqa: E402
from omr_grader.domain.models import PageRef  # noqa: E402
from omr_grader.domain.profile import Profile  # noqa: E402
from omr_grader.recognition.form_layout import (  # noqa: E402
    detect_layout,
    drop_unmarked_header_rows,
)
from omr_grader.recognition.form_profile import build_profile  # noqa: E402
from omr_grader.recognition.pipeline import (  # noqa: E402
    PipelineInput,
    PipelineSuccess,
    recognize_page,
)
from omr_grader.recognition.thresholds import (  # noqa: E402
    CALIBRATION_PROVENANCE,
    thresholds_for_sensitivity,
)

RENDER_DPI = 300


@dataclass
class PartResult:
    part: str
    profile_from: str
    pages: int = 0
    processed: int = 0
    review_pages: int = 0
    failed: int = 0
    wrong_answers: list[str] = field(default_factory=list)
    review_cells: list[str] = field(default_factory=list)
    id_mismatches: list[str] = field(default_factory=list)
    id_review: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.wrong_answers and not self.id_mismatches and not self.failed


def render(pdf: Path) -> list[bytes]:
    pages: list[bytes] = []
    with fitz.open(str(pdf)) as document:
        for page in document:
            scale = RENDER_DPI / 72
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False
            )
            rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, 3
            )
            ok, encoded = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            if not ok:
                raise RuntimeError(f"cannot encode page of {pdf.name}")
            pages.append(encoded.tobytes())
    return pages


PERTURBATIONS = (
    "rotate+1.5",
    "rotate-1.5",
    "scale0.97",
    "scale1.03",
    "shift+10mm",
    "shift-10mm",
    "jpeg60",
    "bright+20%",
    "dark-20%",
    "turn180",
    "turn90",
    "combined",
)


def perturb(encoded: bytes, kind: str) -> bytes:
    """Return a re-encoded page with one scan-like disturbance (G2 robustness gate)."""
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    height, width = image.shape[:2]
    center = (width / 2, height / 2)
    millimetre = RENDER_DPI / 25.4

    def affine(matrix: np.ndarray) -> np.ndarray:
        return cv2.warpAffine(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255),
        )

    quality = None
    if kind.startswith("rotate"):
        image = affine(cv2.getRotationMatrix2D(center, float(kind[6:]), 1.0))
    elif kind.startswith("scale"):
        image = affine(cv2.getRotationMatrix2D(center, 0.0, float(kind[5:])))
    elif kind.startswith("shift"):
        step = (1 if kind[5] == "+" else -1) * 10 * millimetre
        image = affine(np.float32([[1, 0, step], [0, 1, step]]))
    elif kind == "jpeg60":
        quality = 60
    elif kind == "bright+20%":
        image = np.clip(image.astype(np.float32) * 1.2, 0, 255).astype(np.uint8)
    elif kind == "dark-20%":
        image = np.clip(image.astype(np.float32) * 0.8, 0, 255).astype(np.uint8)
    elif kind == "turn180":
        image = cv2.rotate(image, cv2.ROTATE_180)
    elif kind == "turn90":
        image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    elif kind == "combined":
        image = affine(cv2.getRotationMatrix2D(center, 1.0, 0.98))
        image = np.clip(image.astype(np.float32) * 0.85, 0, 255).astype(np.uint8)
        quality = 70
    else:
        raise ValueError(kind)
    if quality is not None:
        ok, data = cv2.imencode(".jpg", image, (cv2.IMWRITE_JPEG_QUALITY, quality))
    else:
        ok, data = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("cannot re-encode perturbed page")
    return data.tobytes()


def make_variant(encoded: bytes, profile: Profile, keep: int) -> bytes:
    """G3: erase every answer row after ``keep``; whole erased blocks become a short-answer box."""
    from omr_grader.recognition.form_alignment import align_page

    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    alignment = align_page(gray, profile)
    if alignment is None or alignment.rotation != 0:
        raise RuntimeError("variant source page must align upright")
    page = profile.page
    assert page is not None
    width, height = page.source_width, page.source_height
    inverse = alignment.inverse

    def to_page(points: list[tuple[float, float]]) -> np.ndarray:
        homogeneous = np.c_[np.asarray(points), np.ones(len(points))] @ inverse.T
        return np.rint(homogeneous[:, :2] / homogeneous[:, 2:3]).astype(np.int32)

    erased: list[np.ndarray] = []
    for region in profile.answer_regions:
        start = int(region.question_start or 1)
        first_dropped = max(0, keep + 1 - start)
        if first_dropped >= region.grid.rows:
            continue
        box = region.bbox_ratio
        x0, y0 = float(box.x) * width, float(box.y) * height
        w, h = float(box.w) * width, float(box.h) * height
        pitch = w / region.grid.cols
        row_h = h / region.grid.rows
        left, right = x0 - 1.0 * pitch, x0 + w + 0.15 * pitch
        top, bottom = y0 + first_dropped * row_h + 2, y0 + h - 2
        polygon = to_page([(left, top), (right, top), (right, bottom), (left, bottom)])
        cv2.fillPoly(image, [polygon], (255, 255, 255))
        if first_dropped == 0:
            erased.append(polygon)
    if erased:
        corners = np.concatenate(erased)
        x_min, y_min = corners.min(axis=0) + 20
        x_max, y_max = corners.max(axis=0) - 20
        cv2.rectangle(image, (int(x_min), int(y_min)), (int(x_max), int(y_max)), (40, 40, 40), 4)
        rng = np.random.default_rng(7)
        line = int(y_min) + 90
        number = keep + 1
        while line < y_max - 40:
            cv2.line(image, (int(x_min) + 30, line), (int(x_max) - 30, line), (120, 120, 120), 2)
            cv2.putText(
                image,
                f"{number}.",
                (int(x_min) + 40, line - 15),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.4,
                (30, 30, 30),
                3,
            )
            word = "".join(rng.choice(list("0O8ADLMT ")) for _ in range(6))
            cv2.putText(
                image,
                word,
                (int(x_min) + 200, line - 12),
                cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,
                2.0,
                (20, 20, 60),
                4,
            )
            line += 110
            number += 1
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("cannot encode variant page")
    return data.tobytes()


def run_variant(baseline: Path, sensitivity: int, keep: int = 50) -> list[PartResult]:
    config = json.loads((baseline / "baseline.json").read_text(encoding="utf-8"))
    truth = json.loads((baseline / config["truth"]).read_text(encoding="utf-8"))
    results: list[PartResult] = []
    for part, spec in config["parts"].items():
        pages = render(Path(spec["pdf"]))
        original = learn_profile(pages, f"baseline-part{part}")
        variants = [make_variant(encoded, original, keep) for encoded in pages]
        profile = learn_profile(variants, f"variant-part{part}")
        questions = sum(region.grid.rows for region in profile.answer_regions)
        if questions != keep:
            raise RuntimeError(f"variant form detected with {questions} questions, expected {keep}")
        expected = [
            {
                "student_id": page["student_id"],
                "answers": {
                    str(q): (page["answers"][str(q)] if q <= keep else None) for q in range(1, 101)
                },
            }
            for page in truth["parts"][part]
        ]
        results.append(
            evaluate(part, variants, expected, profile, f"{keep}-question variant", sensitivity)
        )
    return results


def learn_profile(pages: list[bytes], name: str) -> Profile:
    """Build a profile from pages the way form detection does (header rows included)."""
    found = []
    for encoded in pages:
        gray = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        layout = detect_layout(gray)
        if layout is not None:
            found.append((layout, gray))
    layouts, _ = drop_unmarked_header_rows(found)
    samples = [
        (layout, (gray.shape[1], gray.shape[0]))
        for layout, (_, gray) in zip(layouts, found, strict=True)
    ]
    built = build_profile(samples, name)
    if isinstance(built, Err):
        raise RuntimeError(f"profile could not be built: {built.errors[0].code}")
    return built.value[0]


def evaluate(
    part: str,
    pages: list[bytes],
    truth_pages: list[dict],
    profile: Profile,
    profile_from: str,
    sensitivity: int,
) -> PartResult:
    thresholds = thresholds_for_sensitivity(
        sensitivity, calibrated=True, calibration_provenance=CALIBRATION_PROVENANCE
    ).value
    result = PartResult(part, profile_from)
    for index, (encoded, truth) in enumerate(zip(pages, truth_pages, strict=True), start=1):
        result.pages += 1
        reference = PageRef(
            1,
            "scan-" + "0" * 32,
            f"wi_b{index:02d}",
            SourceKind.PDF,
            "0" * 64,
            f"part{part}.pdf",
            f"p{index}",
            index,
            None,
            index - 1,
            0,
            f"b{part}{index:02d}",
        )
        outcome = recognize_page(PipelineInput(reference, encoded, profile, thresholds))
        if not isinstance(outcome, PipelineSuccess):
            result.failed += 1
            continue
        page = outcome.page
        if page.processing_status is ProcessingStatus.PROCESSED:
            result.processed += 1
        else:
            result.review_pages += 1
        tag = f"P{part} p{index}"
        expected_id = truth["student_id"]
        if page.student_id.value is None:
            result.id_review.append(tag)
        elif page.student_id.value != expected_id:
            result.id_mismatches.append(tag)
        for answer in page.answers:
            expected = truth["answers"][str(answer.question)]
            status = answer.value.status
            if status is AnswerStatus.NORMAL:
                if answer.value.choices[0] != expected:
                    result.wrong_answers.append(f"{tag} Q{answer.question}")
            elif status in (AnswerStatus.BLANK, AnswerStatus.UNASKED):
                if expected is not None:
                    result.wrong_answers.append(f"{tag} Q{answer.question} read blank")
            else:
                result.review_cells.append(f"{tag} Q{answer.question} {status.value}")
    return result


def run_perturbed(baseline: Path, sensitivity: int) -> list[PartResult]:
    config = json.loads((baseline / "baseline.json").read_text(encoding="utf-8"))
    truth = json.loads((baseline / config["truth"]).read_text(encoding="utf-8"))
    results: list[PartResult] = []
    for part, spec in config["parts"].items():
        pages = render(Path(spec["pdf"]))
        profile = learn_profile(pages, f"baseline-part{part}")
        for kind in PERTURBATIONS:
            disturbed = [perturb(encoded, kind) for encoded in pages]
            results.append(
                evaluate(part, disturbed, truth["parts"][part], profile, kind, sensitivity)
            )
    return results


def run(baseline: Path, sensitivity: int, cross: bool) -> list[PartResult]:
    config = json.loads((baseline / "baseline.json").read_text(encoding="utf-8"))
    truth = json.loads((baseline / config["truth"]).read_text(encoding="utf-8"))
    rendered = {part: render(Path(spec["pdf"])) for part, spec in config["parts"].items()}
    profiles = {
        part: learn_profile(pages, f"baseline-part{part}") for part, pages in rendered.items()
    }
    results: list[PartResult] = []
    for part, pages in rendered.items():
        sources = list(profiles) if cross else [part]
        results.extend(
            evaluate(
                part,
                pages,
                truth["parts"][part],
                profiles[source],
                f"profile from part {source}",
                sensitivity,
            )
            for source in sources
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", default=os.environ.get("OMR_V4_BASELINE_DIR"))
    parser.add_argument("--sensitivity", type=int, default=5)
    parser.add_argument("--part-profile", action="store_true", help="also cross-apply profiles")
    parser.add_argument("--perturb", action="store_true", help="G2: disturbed copies of each page")
    parser.add_argument(
        "--variant", action="store_true", help="G3: 50-question form with short answers"
    )
    args = parser.parse_args(argv)
    if not args.baseline:
        parser.error("set --baseline or OMR_V4_BASELINE_DIR")
    if args.variant:
        results = run_variant(Path(args.baseline), args.sensitivity)
    elif args.perturb:
        results = run_perturbed(Path(args.baseline), args.sensitivity)
    else:
        results = run(Path(args.baseline), args.sensitivity, args.part_profile)
    for item in results:
        kinds = Counter(cell.rsplit(" ", 1)[-1] for cell in item.review_cells)
        print(
            f"part {item.part} ({item.profile_from}): pages {item.pages}, "
            f"automatic {item.processed}, review {item.review_pages}, failed {item.failed} | "
            f"wrong answers {len(item.wrong_answers)}, review cells {len(item.review_cells)} {dict(kinds)} | "
            f"ID mismatches {len(item.id_mismatches)}, ID to review {len(item.id_review)}"
        )
        for line in item.wrong_answers + item.id_mismatches + item.review_cells + item.id_review:
            print(f"   {line}")
    return 0 if all(item.passed for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
