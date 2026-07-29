"""Command line entry point for offline ``omr-accuracy-v1`` evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .evaluator import (
    AccuracyError,
    build_receipt,
    canonical_bytes,
    evaluate,
    manifest_digest,
    read_evidence_snapshot,
    sha256_bytes,
    tree_manifest,
    validate_locked_labels,
    verify_approval,
    verify_locked_labels,
)


def _json(path: Path) -> dict[str, Any]:
    try:
        raw = read_evidence_snapshot(path, "JSON")
        value = json.loads(raw)
    except (AccuracyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccuracyError(f"malformed JSON: {path}") from exc
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        raise AccuracyError(f"non-canonical JSON: {path}")
    return value


def _file_hash(path: Path) -> str:
    return sha256_bytes(read_evidence_snapshot(path, "file"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr-accuracy-v1")
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--evaluator-root", type=Path, required=True)
    parser.add_argument("--evaluator-exe", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--threshold", type=Path, required=True)
    parser.add_argument("--threshold-version", required=True)
    parser.add_argument("--app-exe", type=Path, required=True)
    parser.add_argument("--label-signature", type=Path, required=True)
    parser.add_argument("--label-trust", type=Path, required=True)
    parser.add_argument("--label-trust-sha256", required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--approval-trust", type=Path, required=True)
    parser.add_argument("--approval-trust-sha256", required=True)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        label_signature = _json(arguments.label_signature)
        label_trust = _json(arguments.label_trust)
        labels_snapshot = read_evidence_snapshot(arguments.labels, "locked labels")
        if not verify_locked_labels(
            labels_snapshot,
            label_signature,
            label_trust,
            arguments.label_trust_sha256,
        ):
            raise AccuracyError("locked-label signature or trust is invalid")
        labels = validate_locked_labels(labels_snapshot)
        # Prediction evidence is read only after locked labels authenticate, then never reopened.
        predictions_snapshot = read_evidence_snapshot(arguments.predictions, "predictions")
        confusion = evaluate(labels_snapshot, predictions_snapshot)
        fixture = tree_manifest(arguments.fixture_root)
        fixture_hashes = {entry["sha256"] for entry in fixture["entries"]}
        if any(label["source_sha256"] not in fixture_hashes for label in labels):
            raise AccuracyError("fixture tree does not contain every locked source hash")
        if not arguments.threshold_version:
            raise AccuracyError("threshold version is required")
        runner = _json(arguments.runner)
        approval = _json(arguments.approval)
        trust = _json(arguments.approval_trust)
        receipt = build_receipt(
            evaluator_tree_sha256=manifest_digest(tree_manifest(arguments.evaluator_root)),
            evaluator_exe_sha256=_file_hash(arguments.evaluator_exe),
            labels_sha256=sha256_bytes(labels_snapshot),
            fixture_manifest_sha256=manifest_digest(fixture),
            ordered_source_sha256=[record["source_sha256"] for record in labels],
            profile_sha256=_file_hash(arguments.profile),
            threshold_version=arguments.threshold_version,
            threshold_sha256=_file_hash(arguments.threshold),
            app_exe_sha256=_file_hash(arguments.app_exe),
            predictions_sha256=sha256_bytes(predictions_snapshot),
            confusion=confusion,
            runner=runner,
            approval=approval,
        )
        if not verify_approval(receipt, trust, arguments.approval_trust_sha256):
            raise AccuracyError("Product Owner approval signature is invalid")
        if not receipt["verdict"]["passed"]:
            raise AccuracyError("accuracy release gate failed")
        rendered = canonical_bytes(receipt) + b"\n"
        try:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_bytes(rendered)
        except OSError as exc:
            raise AccuracyError(f"unable to write receipt output: {arguments.output}") from exc
        return 0
    except AccuracyError as exc:
        print(f"omr-accuracy-v1: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
