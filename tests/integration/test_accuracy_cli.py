from __future__ import annotations

import pytest

from omr_grader.accuracy import cli
from omr_grader.accuracy.cli import main


def test_cli_requires_all_release_evidence() -> None:
    # argparse is intentionally fail-closed: no offline release receipt can be
    # emitted without locked labels, prediction evidence, trust, and approval.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_cli_rejects_invalid_label_authority_before_reading_predictions(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_bytes(b"tampered")
    label_signature = tmp_path / "label-signature.json"
    label_signature.write_bytes(b"{}")
    label_trust = tmp_path / "label-trust.json"
    label_trust.write_bytes(b"{}")
    result = main(
        [
            "--labels",
            str(labels),
            "--predictions",
            str(tmp_path / "must-not-be-read.jsonl"),
            "--fixture-root",
            str(tmp_path / "fixture"),
            "--evaluator-root",
            str(tmp_path / "evaluator"),
            "--evaluator-exe",
            str(tmp_path / "evaluator.exe"),
            "--profile",
            str(tmp_path / "profile"),
            "--threshold",
            str(tmp_path / "threshold"),
            "--threshold-version",
            "v1",
            "--app-exe",
            str(tmp_path / "app.exe"),
            "--label-signature",
            str(label_signature),
            "--label-trust",
            str(label_trust),
            "--label-trust-sha256",
            "0" * 64,
            "--approval",
            str(tmp_path / "approval.json"),
            "--approval-trust",
            str(tmp_path / "approval-trust.json"),
            "--approval-trust-sha256",
            "0" * 64,
            "--runner",
            str(tmp_path / "runner.json"),
            "--output",
            str(tmp_path / "receipt.json"),
        ]
    )
    assert result == 2
    assert "locked-label signature or trust is invalid" in capsys.readouterr().err

def _arguments(tmp_path) -> list[str]:
    values = {
        "--labels": tmp_path / "labels.jsonl",
        "--predictions": tmp_path / "predictions.jsonl",
        "--fixture-root": tmp_path / "fixture",
        "--evaluator-root": tmp_path / "evaluator",
        "--evaluator-exe": tmp_path / "evaluator.exe",
        "--profile": tmp_path / "profile",
        "--threshold": tmp_path / "threshold",
        "--app-exe": tmp_path / "app.exe",
        "--label-signature": tmp_path / "label-signature.json",
        "--label-trust": tmp_path / "label-trust.json",
        "--approval": tmp_path / "approval.json",
        "--approval-trust": tmp_path / "approval-trust.json",
        "--runner": tmp_path / "runner.json",
        "--output": tmp_path / "output" / "receipt.json",
    }
    arguments = [item for option, path in values.items() for item in (option, str(path))]
    return [
        *arguments,
        "--threshold-version",
        "v1",
        "--label-trust-sha256",
        "0" * 64,
        "--approval-trust-sha256",
        "0" * 64,
    ]


def _successful_pipeline(monkeypatch: pytest.MonkeyPatch, reads: list[tuple[object, str]]) -> None:
    source = "a" * 64
    monkeypatch.setattr(cli, "_json", lambda _path: {})
    monkeypatch.setattr(
        cli,
        "read_evidence_snapshot",
        lambda path, name: reads.append((path, name)) or name.encode("ascii"),
    )
    monkeypatch.setattr(cli, "verify_locked_labels", lambda *_args: True)
    monkeypatch.setattr(cli, "validate_locked_labels", lambda _raw: [{"source_sha256": source}])
    monkeypatch.setattr(cli, "evaluate", lambda *_args: {"verdict": {"passed": True}})
    monkeypatch.setattr(
        cli,
        "tree_manifest",
        lambda _root: {"schema_version": 1, "root_name": "tree", "entries": [{"sha256": source}]},
    )
    monkeypatch.setattr(cli, "manifest_digest", lambda _manifest: "b" * 64)
    monkeypatch.setattr(cli, "_file_hash", lambda _path: "c" * 64)
    monkeypatch.setattr(cli, "build_receipt", lambda **_kwargs: {"verdict": {"passed": True}})
    monkeypatch.setattr(cli, "verify_approval", lambda *_args: True)
    monkeypatch.setattr(cli, "canonical_bytes", lambda _receipt: b"receipt")


def test_cli_success_writes_receipt_and_reads_evidence_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[tuple[object, str]] = []
    _successful_pipeline(monkeypatch, reads)

    assert main(_arguments(tmp_path)) == 0
    assert (tmp_path / "output" / "receipt.json").read_bytes() == b"receipt\n"
    assert [name for _path, name in reads] == ["locked labels", "predictions"]


def test_cli_normalizes_output_filesystem_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reads: list[tuple[object, str]] = []
    _successful_pipeline(monkeypatch, reads)

    def fail_write(_path, _data) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(cli.Path, "write_bytes", fail_write)
    assert main(_arguments(tmp_path)) == 2
    assert "unable to write receipt output" in capsys.readouterr().err
