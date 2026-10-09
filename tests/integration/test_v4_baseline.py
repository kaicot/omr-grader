"""Regression gate on the real exam baseline, plus a smoke run of its harness on fake data.

The folder named by ``OMR_V4_BASELINE_DIR`` holds real student scans and the verified
truth. It never belongs in the repository, and nothing here prints or asserts on student
IDs, names or page contents: only counts and part labels reach an assertion message.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.helpers.pdf_writer import write_pdf
from tests.helpers.synthetic_omr import encode_png, render_sheet

HARNESS = Path(__file__).parents[2] / "tools" / "v4_baseline.py"
BASELINE_DIR = os.environ.get("OMR_V4_BASELINE_DIR")


def _load_harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load ``tools/v4_baseline.py`` by path; its dataclasses need the module in sys.modules."""
    spec = importlib.util.spec_from_file_location("v4_baseline", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not BASELINE_DIR, reason="set OMR_V4_BASELINE_DIR to run the real-exam baseline"
)
def test_the_real_exam_baseline_reads_with_no_wrong_answer_and_no_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_harness(monkeypatch)

    results = harness.run(Path(str(BASELINE_DIR)), 5, True)

    assert results, "the baseline produced no result"
    wrong = sum(len(item.wrong_answers) for item in results)
    mismatches = sum(len(item.id_mismatches) for item in results)
    failed = sum(item.failed for item in results)
    summary = f"{wrong} wrong answers, {mismatches} ID mismatches, {failed} failed pages"
    assert wrong == 0 and mismatches == 0 and failed == 0, summary
    assert all(item.pages > 0 for item in results)
    assert all(item.passed for item in results), [
        f"part {item.part} ({item.profile_from})" for item in results if not item.passed
    ]


def test_the_harness_runs_on_a_fake_one_page_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {question: (question % 5) + 1 if question % 9 else None for question in range(1, 101)}
    page = render_sheet(answers, "20250001", seed=2)
    pdf = tmp_path / "fake.pdf"
    write_pdf(pdf, [encode_png(page)])
    truth = {
        "parts": {
            "1": [{"student_id": "20250001", "answers": {str(q): a for q, a in answers.items()}}]
        }
    }
    (tmp_path / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    config = {"truth": "truth.json", "parts": {"1": {"pdf": str(pdf)}}}
    (tmp_path / "baseline.json").write_text(json.dumps(config), encoding="utf-8")
    harness = _load_harness(monkeypatch)

    results = harness.run(tmp_path, 5, True)

    assert len(results) == 1
    result = results[0]
    assert (result.part, result.profile_from) == ("1", "profile from part 1")
    assert result.pages == 1 and result.failed == 0
    assert result.wrong_answers == [] and result.id_mismatches == []
    assert result.processed == 1 and result.review_pages == 0
    assert result.passed
