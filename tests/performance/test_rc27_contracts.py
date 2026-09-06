from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[2] / "tools" / "performance_workload.py"
spec = importlib.util.spec_from_file_location("performance_workload", MODULE)
assert spec and spec.loader
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def workload() -> dict:
    return {
        "schema_version": "rc27-workload-v1",
        "workload_id": "rc27-test",
        "executable": {"path": "app.exe", "sha256": "a" * 64},
        "argv": [],
        "inputs": [{"path": "fixture", "sha256": "b" * 64, "source_sha256": "c" * 64, "bytes": 1}],
        "fixed_workload": {
            "pages": 50,
            "dpi": 300,
            "max_in_flight": 4,
            "index_sessions": 2000,
            "detail_students": 500,
        },
    }


def evidence(**changes: object) -> dict:
    value = {
        "heartbeat_gaps_ms": [250],
        "cancel_submit_ms": 250,
        "cancel_settle_ms": 10_000,
        "shutdown_ms": 15_000,
        "orphan_pids": [],
        "workload_elapsed_ms": 0,
        "index_warm_ms": 100,
        "index_cold_ms": 500,
        "index_bytes": 5 * 1024**2,
        "detail_ms": 100,
        "transform_rms_px": 1,
        "transform_max_px": 2,
        "io": {"read_bytes": 0, "write_bytes": 0},
        "samples": [
            {
                "elapsed_ms": 0,
                "pids": {"1": 42, "2": 58},
                "aggregate_rss_bytes": 100,
                "in_flight": 4,
            }
        ],
        "descendant_pids": [1, 2],
        "runner": {
            "cpu": "i5",
            "ram_bytes": 1,
            "os": "Windows",
            "power": "ac",
            "defender": "true",
        },
    }
    value.update(changes)
    return value


def test_canonical_schema_and_receipt_hash_are_exact(tmp_path: Path) -> None:
    receipt = harness.make_receipt(workload(), evidence())
    output = tmp_path / "receipt.json"
    harness.write_receipt(output, receipt)
    assert output.read_bytes() == harness.canonical_json(receipt)
    assert output.read_bytes().endswith(b"\n")
    with pytest.raises(ValueError):
        harness.validate_workload({**workload(), "extra": True})


def _samples_for_elapsed(elapsed_ms: int) -> list[dict[str, object]]:
    points = list(range(0, elapsed_ms + 1, 100))
    if points[-1] != elapsed_ms:
        points.append(elapsed_ms)
    return [
        {
            "elapsed_ms": point,
            "pids": {"1": 42, "2": 58},
            "aggregate_rss_bytes": 100,
            "in_flight": 4,
        }
        for point in points
    ]


@pytest.mark.parametrize("key,limit", list(harness.LIMITS.items()))
def test_rc27_boundary_and_next_value(key: str, limit: int) -> None:
    if key == "orphan_count":
        good = evidence(orphan_pids=[])
        bad = evidence(orphan_pids=[99])
    elif key == "aggregate_rss_bytes":
        good = evidence(
            samples=[
                {
                    "elapsed_ms": 0,
                    "pids": {"1": harness.GIB, "2": harness.GIB},
                    "aggregate_rss_bytes": 2 * harness.GIB,
                    "in_flight": 4,
                }
            ]
        )
        bad = evidence(
            samples=[
                {
                    "elapsed_ms": 0,
                    "pids": {"1": harness.GIB, "2": harness.GIB + 1},
                    "aggregate_rss_bytes": 2 * harness.GIB + 1,
                    "in_flight": 4,
                }
            ]
        )
    elif key == "in_flight":
        good = evidence(
            samples=[
                {
                    "elapsed_ms": 0,
                    "pids": {"1": 42, "2": 58},
                    "aggregate_rss_bytes": 100,
                    "in_flight": 4,
                }
            ]
        )
        bad = evidence(
            samples=[
                {
                    "elapsed_ms": 0,
                    "pids": {"1": 42, "2": 58},
                    "aggregate_rss_bytes": 100,
                    "in_flight": 5,
                }
            ]
        )
    elif key == "workload_elapsed_ms":
        good = evidence(
            workload_elapsed_ms=limit,
            samples=_samples_for_elapsed(limit),
        )
        bad = evidence(
            workload_elapsed_ms=limit + 1,
            samples=_samples_for_elapsed(limit + 1),
        )
    else:
        field = {"heartbeat_gap_ms": "heartbeat_gaps_ms"}.get(key, key)
        good = evidence(**{field: [limit] if field == "heartbeat_gaps_ms" else limit})
        bad = evidence(**{field: [limit + 1] if field == "heartbeat_gaps_ms" else limit + 1})
    assert harness.evaluate_evidence(good)[key]
    assert not harness.evaluate_evidence(bad)[key]


def test_elapsed_sample_coverage_accepts_exact_tolerance_and_rejects_tolerance_plus_one() -> None:
    tolerance_ms = int(harness.SAMPLE_INTERVAL_SECONDS * 2000)
    exact = evidence(
        workload_elapsed_ms=tolerance_ms,
        samples=[
            {
                "elapsed_ms": 0,
                "pids": {"1": 42, "2": 58},
                "aggregate_rss_bytes": 100,
                "in_flight": 4,
            }
        ],
    )
    too_old = evidence(
        workload_elapsed_ms=tolerance_ms + 1,
        samples=[
            {
                "elapsed_ms": 0,
                "pids": {"1": 42, "2": 58},
                "aggregate_rss_bytes": 100,
                "in_flight": 4,
            }
        ],
    )
    assert harness.evaluate_evidence(exact)["workload_elapsed_ms"]
    with pytest.raises(ValueError, match="cover"):
        harness.evaluate_evidence(too_old)


def test_aggregate_is_per_pid_sum_and_missing_channel_fails() -> None:
    assert harness.evaluate_evidence(evidence())["aggregate_rss_bytes"]
    with pytest.raises(ValueError, match="aggregate RSS"):
        harness.evaluate_evidence(
            evidence(
                samples=[
                    {"elapsed_ms": 0, "pids": {"1": 2}, "aggregate_rss_bytes": 3, "in_flight": 0}
                ]
            )
        )
    incomplete = evidence()
    del incomplete["runner"]
    with pytest.raises(ValueError):
        harness.evaluate_evidence(incomplete)


def test_orphans_fail_closed() -> None:
    assert not harness.evaluate_evidence(evidence(orphan_pids=[99]))["orphan_count"]
def test_samples_cannot_extend_past_reported_elapsed_time() -> None:
    with pytest.raises(ValueError, match="beyond"):
        harness.evaluate_evidence(
            evidence(
                workload_elapsed_ms=0,
                samples=[
                    {
                        "elapsed_ms": 0,
                        "pids": {"1": 42, "2": 58},
                        "aggregate_rss_bytes": 100,
                        "in_flight": 4,
                    },
                    {
                        "elapsed_ms": 1,
                        "pids": {"1": 42, "2": 58},
                        "aggregate_rss_bytes": 100,
                        "in_flight": 4,
                    },
                ],
            )
        )
