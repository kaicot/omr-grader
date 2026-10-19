"""Smoke run of the synthetic stress suite (``tools/stress_suite.py``): one seed, sensitivity 5.

The suite measures weaknesses of the engine, so only the two scenarios that hold today are
asserted: ordinary marks never read wrongly and equal double marks stay MULTIPLE. For the
others the test checks that the harness produced numbers, not that the engine is right;
their results are informational and will change as the engine improves.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).parents[2] / "tools" / "stress_suite.py"


def _load_tool(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load ``tools/stress_suite.py`` by path; its dataclasses need the module in sys.modules."""
    spec = importlib.util.spec_from_file_location("stress_suite", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_stress_suite_reads_clean_pages_right_and_keeps_equal_doubles_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _load_tool(monkeypatch)

    results = suite.run_suite(list(suite.SCENARIOS), [0], (suite.DEFAULT_SENSITIVITY,))

    assert set(results) == set(suite.SCENARIOS)
    totals = {
        name: outcomes[suite.DEFAULT_SENSITIVITY].totals for name, outcomes in results.items()
    }
    clean = totals["clean"]
    assert clean.total == 100 and clean.silent_wrong == 0 and clean.review == 0
    assert clean.background_silent_wrong == 0 and clean.id_wrong_accepted == 0
    assert clean.id_normal == 1
    pairs = totals["double_equal"]
    assert pairs.total > 0 and pairs.correct == pairs.total
    # The other scenarios are measurements: they must have run and counted their cases.
    for name, counts in totals.items():
        assert counts.pages > 0, name
        assert counts.id_normal + counts.id_invalid == counts.pages, name
        if name != "truncated_id":
            assert counts.total > 0, name
