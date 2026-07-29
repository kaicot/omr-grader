"""Offline, deterministic evaluator for the frozen ``omr-accuracy-v1`` contract."""

from .evaluator import (
    AccuracyError,
    build_receipt,
    evaluate,
    read_evidence_snapshot,
    receipt_digest,
    tree_manifest,
    validate_locked_labels,
    validate_predictions,
    validate_receipt,
    verify_approval,
    verify_locked_labels,
)

__all__ = [
    "AccuracyError",
    "build_receipt",
    "evaluate",
    "read_evidence_snapshot",
    "receipt_digest",
    "tree_manifest",
    "validate_locked_labels",
    "validate_predictions",
    "validate_receipt",
    "verify_approval",
    "verify_locked_labels",
]
