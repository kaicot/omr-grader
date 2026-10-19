"""Synthetic recognition stress suite: measure what the current engine reads wrongly.

usage: python tools/stress_suite.py [--seeds N] [--questions 60|100] [--sensitivity N]
           [--scenarios NAME ...] [--brief] [--json PATH] [--record PATH]

Every scenario renders synthetic answer sheets (printed digits inside the bubbles, an
8-digit student ID), pushes them through the product recognition path (``recognize_page``
with a profile learned from a clean page of the same form) and compares each question with
the known truth. A question is ``correct`` when the definite answer equals the truth (or it
is blank where the truth is blank), ``review`` when the engine asks for a person to look
(UNCERTAIN, including every question of a held or unreadable page) and ``silent_wrong``
when a definite answer, MULTIPLE or BLANK differs from the truth: the dangerous case,
because it is graded without anybody looking. The student ID is accepted (NORMAL), sent to
review (not NORMAL) or accepted wrongly (NORMAL but not the true ID, or any NORMAL ID where
the truth is a truncated one).

This tool only measures. Pages are deterministic for a given ``--seeds`` and nothing is
written to disk except the optional ``--json`` and ``--record`` files, which hold numbers
only. Exit status is always 0: the known weak scenarios are the point, not a failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Unpack

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _entry in (ROOT / "src", ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from omr_grader import __version__  # noqa: E402
from omr_grader.domain.enums import AnswerStatus, StudentIdStatus  # noqa: E402
from omr_grader.domain.profile import Profile  # noqa: E402
from omr_grader.recognition.pipeline import (  # noqa: E402
    PipelineInput,
    PipelineSuccess,
    recognize_page,
)
from omr_grader.recognition.thresholds import RecognitionThresholds  # noqa: E402
from tests.helpers.omr_engine import make_page_ref, make_thresholds, reference_sheet  # noqa: E402
from tests.helpers.synthetic_omr import (  # noqa: E402
    DISK_RADIUS,
    MARK_GRAY,
    Image,
    SheetGeometry,
    SheetOptions,
    encode_png,
    paint_mark,
    render_sheet_with_geometry,
    to_gray,
)

Truth = tuple[int, ...]
"""The choices that count for a question: ``()`` blank, one choice, or a double."""

CHOICES: Final = 5
LAYOUTS: Final = {100: (20, 20, 20, 20, 20), 60: (20, 20, 20)}
DEFAULT_SENSITIVITY: Final = 5
SWEEP_SENSITIVITIES: Final = (3, 5, 7, 10)
SWEEP_SCENARIOS: Final = ("faint_mark", "residue_lone", "residue_beside_mark")
CASE_STEP: Final = 3
"""Every third question is a stressed one; the rest are ordinary marks.

The unmarked level of a printed digit is a per-column median, so most of the page must stay
ordinary or the measurement would test that reference instead of the cell decision.
"""
PAPER: Final = 245
BLUR: Final = 0.7
NOISE: Final = 2.0
TAPE_GRAY: Final = 252
"""Correction tape scans slightly whiter than the paper."""
TAPE_EDGE_GRAY: Final = 215
"""The thin shadow along a tape edge."""
CUT_MARGIN: Final = 17.0
"""A bubble closer than this to the image border has part of its measured disk outside."""


@dataclass(frozen=True, slots=True)
class Level:
    """One severity step of a scenario: a label and the drawing parameters."""

    label: str
    gray: int = MARK_GRAY
    radius: float = DISK_RADIUS
    ghost: int | None = None
    """Correction tape only: gray of the old mark showing through the tape."""


FAINT_LEVELS: Final = (
    Level("gray 40", 40),
    Level("gray 80", 80),
    Level("gray 120", 120),
    Level("gray 160", 160),
    Level("gray 200", 200),
    Level("dot r10", MARK_GRAY, 10.0),
    Level("dot r8", MARK_GRAY, 8.0),
    Level("dot r6.5", MARK_GRAY, 6.5),
    Level("dot r5", MARK_GRAY, 5.0),
)
RESIDUE_LEVELS: Final = tuple(
    Level(f"gray {gray}", gray, 14.0) for gray in (210, 190, 170, 150, 130)
)
TAPE_LEVELS: Final = (
    Level("covers ring", 26),
    Level("ring visible", 20),
    Level("edge at mark", 16),
    Level("sliver 3px", 13),
    Level("sliver 6px", 10),
    Level("sliver 9px", 7),
    Level("sliver 12px", 4),
    Level("half covered", 0),
    Level("ghost 225", 26, ghost=225),
    Level("ghost 205", 26, ghost=205),
    Level("ghost 190", 26, ghost=190),
)
"""For tape the ``gray`` field is how far right of the cell center the patch reaches (px)."""
PAIR_LEVELS: Final = (
    Level("dark pair"),
    Level("medium pair", 100),
    Level("dot pair", MARK_GRAY, 10.0),
)
CUT_LEVELS: Final[tuple[tuple[str, SheetOptions], ...]] = (
    ("shift +130px", {"shift": (130.0, 0.0)}),
    ("shift +160px", {"shift": (160.0, 0.0)}),
    ("shift +220px", {"shift": (220.0, 0.0)}),
    ("shift down 260px", {"shift": (0.0, 260.0)}),
    ("shift down 300px", {"shift": (0.0, 300.0)}),
    ("scale 1.10", {"scale": 1.10}),
)
ID_VARIANTS: Final = (
    ("last column blank", lambda value: value[:7] + "_"),
    ("last two blank", lambda value: value[:6] + "__"),
    ("middle column blank", lambda value: value[:4] + "_" + value[5:]),
    ("first column blank", lambda value: "_" + value[1:]),
)


@dataclass(slots=True)
class Counts:
    """Question and ID outcomes. Question counts exclude ordinary background questions."""

    pages: int = 0
    failed_pages: int = 0
    held_pages: int = 0
    correct: int = 0
    review: int = 0
    silent_wrong: int = 0
    background_review: int = 0
    background_silent_wrong: int = 0
    id_normal: int = 0
    id_invalid: int = 0
    id_wrong_accepted: int = 0

    @property
    def total(self) -> int:
        return self.correct + self.review + self.silent_wrong

    def merge(self, other: Counts) -> None:
        for name in asdict(other):
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass(slots=True)
class Outcome:
    """One scenario at one sensitivity: totals plus the same counts for every level."""

    totals: Counts = field(default_factory=Counts)
    levels: dict[str, Counts] = field(default_factory=dict)

    def level(self, label: str) -> Counts:
        return self.levels.setdefault(label, Counts())


@dataclass(frozen=True, slots=True)
class PageSpec:
    """A rendered page and what is true about it."""

    image: Image
    truth: Mapping[int, Truth]
    cases: Mapping[int, str]
    """Stressed questions and the level each one belongs to; the others are background."""
    student_id: str | None
    """The true ID, or ``None`` when the ID is deliberately incomplete and must not be accepted."""
    label: str | None = None
    """Level that this page's page-level and ID counts belong to."""


@dataclass(frozen=True, slots=True)
class Context:
    questions: int
    layout: tuple[int, ...]
    profile: Profile


Builder = Callable[[Context, int], list[PageSpec]]


def _context(questions: int) -> Context:
    layout = LAYOUTS[questions]
    return Context(questions, layout, reference_sheet(layout).profile)


def _student_id(seed: int) -> str:
    return f"2026{(seed * 37 + 11) % 10000:04d}"


def _render(
    ctx: Context,
    seed: int,
    answers: Mapping[int, Truth],
    student_id: str,
    **options: Unpack[SheetOptions],
) -> tuple[Image, SheetGeometry]:
    """Render without scanner blur and noise so stress marks can be added before them."""
    return render_sheet_with_geometry(
        {question: (choice or None) for question, choice in answers.items()},
        student_id,
        **{**options, "layout": ctx.layout, "noise": 0.0, "blur": 0.0, "seed": seed},
    )


def _finish(image: Image, seed: int) -> Image:
    """The generator's scanner blur and noise, applied after the stress marks."""
    page = to_gray(image)
    page = np.asarray(cv2.GaussianBlur(page, (0, 0), BLUR), dtype=np.uint8)
    spread = max(1, round(NOISE * 1.7321))
    grain = np.random.default_rng([seed, 99]).integers(
        -spread, spread + 1, size=page.shape, dtype=np.int16
    )
    page = np.clip(page.astype(np.int16) + grain, 0, 255).astype(np.uint8)
    return np.asarray(cv2.cvtColor(page, cv2.COLOR_GRAY2BGR), dtype=np.uint8)


def _stamp(
    image: Image,
    geometry: SheetGeometry,
    question: int,
    choice: int,
    rng: np.random.Generator,
    gray: int | None = None,
    radius: float = DISK_RADIUS,
) -> None:
    """Paint a hand-made mark the way the generator does: jittered position, size and darkness."""
    x, y = geometry.answer_center(question, choice)
    offset = rng.uniform(-2.0, 2.0, size=2)
    size = radius + float(rng.uniform(-0.5, 0.5))
    value = MARK_GRAY + int(rng.integers(0, 20)) if gray is None else gray
    paint_mark(image, (x + float(offset[0]), y + float(offset[1])), size, value)


def _pick(rng: np.random.Generator, exclude: int | None = None) -> int:
    choices = [choice for choice in range(1, CHOICES + 1) if choice != exclude]
    return int(rng.choice(choices))


def _background(
    ctx: Context, rng: np.random.Generator, skip: Sequence[int] = ()
) -> dict[int, Truth]:
    """Ordinary answers: one clear mark, with every eleventh question left blank."""
    return {
        question: () if question % 11 == 0 else (_pick(rng),)
        for question in range(1, ctx.questions + 1)
        if question not in skip
    }


def _case_questions(ctx: Context) -> list[int]:
    return [question for question in range(1, ctx.questions + 1) if question % CASE_STEP == 0]


def _levelled(
    ctx: Context,
    seed: int,
    levels: Sequence[Level],
    draw: Callable[[Image, SheetGeometry, np.random.Generator, int, Level], Truth],
) -> list[PageSpec]:
    """One page whose stressed questions cycle through ``levels``; ``draw`` returns the truth."""
    rng = np.random.default_rng([seed, 1])
    cases = _case_questions(ctx)
    ordinary = _background(ctx, rng, cases)
    assigned = {
        question: levels[(index + seed) % len(levels)] for index, question in enumerate(cases)
    }
    truth: dict[int, Truth] = dict(ordinary)

    def paint(image: Image, geometry: SheetGeometry, painter: np.random.Generator) -> None:
        for question, level in assigned.items():
            truth[question] = draw(image, geometry, painter, question, level)

    student_id = _student_id(seed)
    image, geometry = _render(ctx, seed, ordinary, student_id)
    paint(image, geometry, np.random.default_rng([seed, 7]))
    cases = {question: level.label for question, level in assigned.items()}
    return [PageSpec(_finish(image, seed), truth, cases, student_id)]


def clean(ctx: Context, seed: int) -> list[PageSpec]:
    rng = np.random.default_rng([seed, 1])
    answers = _background(ctx, rng)
    student_id = _student_id(seed)
    image, _ = _render(ctx, seed, answers, student_id)
    cases = {question: "single mark" for question in answers}
    return [PageSpec(_finish(image, seed), answers, cases, student_id)]


def faint_mark(ctx: Context, seed: int) -> list[PageSpec]:
    def draw(
        image: Image, geometry: SheetGeometry, rng: np.random.Generator, question: int, level: Level
    ) -> Truth:
        choice = _pick(rng)
        _stamp(image, geometry, question, choice, rng, level.gray, level.radius)
        return (choice,)

    return _levelled(ctx, seed, FAINT_LEVELS, draw)


def residue_lone(ctx: Context, seed: int) -> list[PageSpec]:
    def draw(
        image: Image, geometry: SheetGeometry, rng: np.random.Generator, question: int, level: Level
    ) -> Truth:
        _smudge(image, geometry, question, _pick(rng), rng, level)
        return ()

    return _levelled(ctx, seed, RESIDUE_LEVELS, draw)


def residue_beside_mark(ctx: Context, seed: int) -> list[PageSpec]:
    def draw(
        image: Image, geometry: SheetGeometry, rng: np.random.Generator, question: int, level: Level
    ) -> Truth:
        mark = _pick(rng)
        _stamp(image, geometry, question, mark, rng)
        _smudge(image, geometry, question, _pick(rng, mark), rng, level)
        return (mark,)

    return _levelled(ctx, seed, RESIDUE_LEVELS, draw)


def _smudge(
    image: Image,
    geometry: SheetGeometry,
    question: int,
    choice: int,
    rng: np.random.Generator,
    level: Level,
) -> None:
    """What an eraser leaves behind: a light, uneven gray patch about the size of a mark."""
    x, y = geometry.answer_center(question, choice)
    offset = rng.uniform(-3.0, 3.0, size=2)
    axes = rng.uniform(level.radius - 2.0, level.radius + 2.0, size=2)
    cv2.ellipse(
        image,
        (round(x + float(offset[0])), round(y + float(offset[1]))),
        (round(float(axes[0])), round(float(axes[1]))),
        float(rng.uniform(0.0, 180.0)),
        0,
        360,
        (level.gray,) * 3,
        -1,
        cv2.LINE_AA,
    )


def correction_tape(ctx: Context, seed: int) -> list[PageSpec]:
    def draw(
        image: Image, geometry: SheetGeometry, rng: np.random.Generator, question: int, level: Level
    ) -> Truth:
        old = _pick(rng)
        new = _pick(rng, old)
        _stamp(image, geometry, question, old, rng)
        _tape(image, geometry.answer_center(question, old), level, rng)
        _stamp(image, geometry, question, new, rng)
        return (new,)

    return _levelled(ctx, seed, TAPE_LEVELS, draw)


def _tape(
    image: Image, center: tuple[float, float], level: Level, rng: np.random.Generator
) -> None:
    """A white strip over a filled cell: it hides the ring and the mark up to ``level.gray`` px.

    The strip is wide enough to cover the whole bubble on the left, with a faint shadow
    along its edge; what stays visible is the part of the old mark and ring on the right,
    and with ``level.ghost`` the old mark also shows through the tape as a light gray patch.
    """
    x, y = center
    right = round(x + level.gray + float(rng.uniform(-0.5, 0.5)))
    top = round(y - 26 + float(rng.uniform(-1.5, 1.5)))
    bottom = round(y + 26 + float(rng.uniform(-1.5, 1.5)))
    left = round(x - 32)
    cv2.rectangle(image, (left, top), (right, bottom), (TAPE_GRAY,) * 3, -1, cv2.LINE_AA)
    if level.ghost is not None:
        paint_mark(image, (x, y), DISK_RADIUS - 1.0, level.ghost)
    cv2.rectangle(image, (left, top), (right, bottom), (TAPE_EDGE_GRAY,) * 3, 2, cv2.LINE_AA)


def double_equal(ctx: Context, seed: int) -> list[PageSpec]:
    def draw(
        image: Image, geometry: SheetGeometry, rng: np.random.Generator, question: int, level: Level
    ) -> Truth:
        first = _pick(rng)
        second = _pick(rng, first)
        gray = None if level.gray == MARK_GRAY else level.gray
        for choice in (first, second):
            _stamp(image, geometry, question, choice, rng, gray, level.radius)
        return tuple(sorted((first, second)))

    return _levelled(ctx, seed, PAIR_LEVELS, draw)


def cut_column(ctx: Context, seed: int) -> list[PageSpec]:
    """Pages moved or enlarged so that some bubbles fall off the image edge."""
    specs: list[PageSpec] = []
    for label, options in CUT_LEVELS:
        rng = np.random.default_rng([seed, 1])
        answers = {question: (_pick(rng),) for question in range(1, ctx.questions + 1)}
        student_id = _student_id(seed)
        image, geometry = _render(ctx, seed, answers, student_id, **options)
        cases = {
            question: label
            for question in range(1, ctx.questions + 1)
            if _is_cut(geometry, question)
        }
        specs.append(PageSpec(_finish(image, seed), answers, cases, student_id, label))
    return specs


def _is_cut(geometry: SheetGeometry, question: int) -> bool:
    for choice in range(1, CHOICES + 1):
        x, y = geometry.answer_center(question, choice)
        if (
            x < CUT_MARGIN
            or y < CUT_MARGIN
            or x > geometry.width - 1 - CUT_MARGIN
            or y > geometry.height - 1 - CUT_MARGIN
        ):
            return True
    return False


def truncated_id(ctx: Context, seed: int) -> list[PageSpec]:
    specs: list[PageSpec] = []
    for label, cut in ID_VARIANTS:
        rng = np.random.default_rng([seed, 1])
        answers = _background(ctx, rng)
        image, _ = _render(ctx, seed, answers, cut(_student_id(seed)))
        specs.append(PageSpec(_finish(image, seed), answers, {}, None, label))
    return specs


SCENARIOS: Final[dict[str, Builder]] = {
    "clean": clean,
    "faint_mark": faint_mark,
    "residue_lone": residue_lone,
    "residue_beside_mark": residue_beside_mark,
    "correction_tape": correction_tape,
    "double_equal": double_equal,
    "cut_column": cut_column,
    "truncated_id": truncated_id,
}
SWEEP_NAME: Final = "sensitivity"


def classify(truth: Truth, status: AnswerStatus, choices: Sequence[int]) -> str | None:
    """``correct``, ``review``, ``silent_wrong``, or ``None`` for a question the form lacks."""
    if status is AnswerStatus.UNASKED:
        return None
    if status is AnswerStatus.UNCERTAIN:
        return "review"
    observed = () if status is AnswerStatus.BLANK else tuple(sorted(choices))
    return "correct" if observed == truth else "silent_wrong"


def _tally(counts: Counts, outcome: str, case: bool) -> None:
    if case:
        counts.correct += outcome == "correct"
        counts.review += outcome == "review"
        counts.silent_wrong += outcome == "silent_wrong"
    else:
        counts.background_review += outcome == "review"
        counts.background_silent_wrong += outcome == "silent_wrong"


def read_page(
    spec: PageSpec, profile: Profile, thresholds: RecognitionThresholds, encoded: bytes
) -> tuple[Counts, dict[str, Counts]]:
    """Recognize one page and score it: page-level counts plus counts per case level."""
    page_counts = Counts(pages=1)
    by_level: dict[str, Counts] = {}
    outcome = recognize_page(PipelineInput(make_page_ref(), encoded, profile, thresholds))
    if not isinstance(outcome, PipelineSuccess):
        page_counts.failed_pages = 1
        page_counts.id_invalid = 1
        for question in spec.truth:
            case = question in spec.cases
            level = by_level.setdefault(spec.cases[question] if case else "", Counts())
            _tally(page_counts, "review", case)
            _tally(level, "review", case)
        return page_counts, by_level
    page = outcome.page
    page_counts.held_pages = int(page.processing_status.value != "processed")
    identity = page.student_id
    if identity.status is StudentIdStatus.NORMAL:
        page_counts.id_normal = 1
        page_counts.id_wrong_accepted = int(identity.value != spec.student_id)
    else:
        page_counts.id_invalid = 1
    for answer in page.answers:
        truth = spec.truth.get(answer.question)
        if truth is None:
            continue
        result = classify(truth, answer.value.status, answer.value.choices)
        if result is None:
            continue
        case = answer.question in spec.cases
        level = by_level.setdefault(spec.cases[answer.question] if case else "", Counts())
        _tally(page_counts, result, case)
        _tally(level, result, case)
    return page_counts, by_level


def run_suite(
    names: Sequence[str],
    seeds: Sequence[int],
    sensitivities: Mapping[str, Sequence[int]] | Sequence[int] = (DEFAULT_SENSITIVITY,),
    questions: int = 100,
) -> dict[str, dict[int, Outcome]]:
    """Run ``names`` on ``seeds``; each page is rendered once and read at every sensitivity.

    ``sensitivities`` is one list for every scenario or a mapping from scenario name to its
    own list. Returns ``{scenario: {sensitivity: Outcome}}``.
    """
    ctx = _context(questions)
    results: dict[str, dict[int, Outcome]] = {}
    for name in names:
        wanted = sensitivities[name] if isinstance(sensitivities, Mapping) else sensitivities
        outcomes = {sensitivity: Outcome() for sensitivity in wanted}
        thresholds = {sensitivity: make_thresholds(sensitivity) for sensitivity in wanted}
        for seed in seeds:
            for spec in SCENARIOS[name](ctx, seed):
                encoded = encode_png(spec.image)
                for sensitivity, outcome in outcomes.items():
                    page_counts, by_level = read_page(
                        spec, ctx.profile, thresholds[sensitivity], encoded
                    )
                    outcome.totals.merge(page_counts)
                    for label, counts in by_level.items():
                        if label:
                            outcome.level(label).merge(counts)
                    if spec.label is not None:
                        outcome.level(spec.label).merge(_page_only(page_counts))
        results[name] = outcomes
    return results


def _page_only(counts: Counts) -> Counts:
    """The page-level and ID part of ``counts``, for a level that is a whole page."""
    return Counts(
        pages=counts.pages,
        failed_pages=counts.failed_pages,
        held_pages=counts.held_pages,
        id_normal=counts.id_normal,
        id_invalid=counts.id_invalid,
        id_wrong_accepted=counts.id_wrong_accepted,
    )


def _row(label: str, counts: Counts, indent: str = "") -> str:
    def page_field(value: int, width: int) -> str:
        """Page and ID counts stay blank on a level row that has no page of its own."""
        return f"{value if counts.pages else '':>{width}}"

    return (
        f"{indent + label:<30}{page_field(counts.pages, 6)}{counts.total:>7}{counts.correct:>9}"
        f"{counts.review:>8}{counts.silent_wrong:>13} |{counts.background_silent_wrong:>5}"
        f" |{page_field(counts.id_normal, 5)}{page_field(counts.id_invalid, 8)}"
        f"{page_field(counts.id_wrong_accepted, 7)}"
        f"{page_field(counts.failed_pages, 7)}{page_field(counts.held_pages, 6)}"
    )


HEADER: Final = (
    f"{'scenario / level':<30}{'pages':>6}{'cases':>7}{'correct':>9}{'review':>8}"
    f"{'silent_wrong':>13} |{'bg_sw':>5} |{'id_ok':>5}{'id_inv':>8}{'id_bad':>7}"
    f"{'failed':>7}{'held':>6}"
)


def render_table(
    results: Mapping[str, Mapping[int, Outcome]], sensitivity: int, levels: bool
) -> str:
    lines = [f"sensitivity {sensitivity}", HEADER, "-" * len(HEADER)]
    for name, outcomes in results.items():
        outcome = outcomes.get(sensitivity)
        if outcome is None:
            continue
        lines.append(_row(name, outcome.totals))
        if levels:
            lines.extend(_row(label, counts, "  ") for label, counts in outcome.levels.items())
    return "\n".join(lines)


def render_sweep(results: Mapping[str, Mapping[int, Outcome]]) -> str:
    head = f"{'sensitivity sweep':<30}{'sens':>5}{'cases':>7}{'correct':>9}{'review':>8}{'silent_wrong':>13}"
    lines = [head, "-" * len(head)]
    for name in SWEEP_SCENARIOS:
        for sensitivity, outcome in sorted(results.get(name, {}).items()):
            counts = outcome.totals
            lines.append(
                f"{name if sensitivity == min(results[name]) else '':<30}{sensitivity:>5}"
                f"{counts.total:>7}{counts.correct:>9}{counts.review:>8}{counts.silent_wrong:>13}"
            )
    return "\n".join(lines)


def _outcome_json(outcome: Outcome) -> dict[str, object]:
    return {
        **asdict(outcome.totals),
        "levels": {label: asdict(counts) for label, counts in outcome.levels.items()},
    }


def summary(
    results: Mapping[str, Mapping[int, Outcome]], seeds: int, questions: int, seconds: float
) -> dict[str, object]:
    return {
        "version": __version__,
        "commit": _git_commit(),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "questions": questions,
        "seeds": seeds,
        "seconds": round(seconds, 1),
        "scenarios": {
            name: {
                str(sensitivity): _outcome_json(outcome)
                for sensitivity, outcome in sorted(outcomes.items())
            }
            for name, outcomes in results.items()
        },
    }


def _record_line(data: Mapping[str, object]) -> dict[str, object]:
    """The summary without the per-level detail: numbers only, one line per run."""
    scenarios = data["scenarios"]
    assert isinstance(scenarios, dict)
    brief = {
        name: {
            sensitivity: {key: value for key, value in counts.items() if key != "levels"}
            for sensitivity, counts in by_sensitivity.items()
        }
        for name, by_sensitivity in scenarios.items()
    }
    return {**data, "scenarios": brief}


def _git_commit() -> str:
    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=30
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    return commit + ("+dirty" if git("status", "--porcelain") else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--seeds", type=int, default=3, help="pages per scenario level (default 3)")
    parser.add_argument("--questions", type=int, choices=sorted(LAYOUTS), default=100)
    parser.add_argument("--sensitivity", type=int, default=DEFAULT_SENSITIVITY)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=[*SCENARIOS, SWEEP_NAME],
        help=f"default: all; {SWEEP_NAME} reruns {', '.join(SWEEP_SCENARIOS)} at "
        f"{', '.join(map(str, SWEEP_SENSITIVITIES))}",
    )
    parser.add_argument("--brief", action="store_true", help="scenario rows without levels")
    parser.add_argument("--json", metavar="PATH", help="write the full summary as JSON")
    parser.add_argument("--record", metavar="PATH", help="append one numbers-only JSON line")
    args = parser.parse_args(argv)

    chosen = list(args.scenarios or [*SCENARIOS, SWEEP_NAME])
    sweep = SWEEP_NAME in chosen
    names = [name for name in chosen if name != SWEEP_NAME]
    if sweep:
        names += [name for name in SWEEP_SCENARIOS if name not in names]
    wanted = {
        name: sorted(
            {args.sensitivity, *(SWEEP_SENSITIVITIES if sweep and name in SWEEP_SCENARIOS else ())}
        )
        for name in names
    }
    started = time.perf_counter()
    results = run_suite(names, range(args.seeds), wanted, args.questions)
    seconds = time.perf_counter() - started

    print(render_table(results, args.sensitivity, not args.brief))
    if sweep:
        print()
        print(render_sweep(results))
    print(
        f"\n{sum(item.totals.pages for o in results.values() for item in o.values())} page reads in {seconds:.0f} s"
    )
    data = summary(results, args.seeds, args.questions, seconds)
    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=2), encoding="utf-8")
    if args.record:
        with Path(args.record).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_record_line(data), separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
