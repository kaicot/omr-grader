from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from omr_grader.accuracy import evaluator
from omr_grader.accuracy.evaluator import (
    AccuracyError,
    canonical_bytes,
    evaluate,
    manifest_digest,
    sha256_bytes,
    tree_manifest,
    validate_locked_labels,
    validate_receipt,
    verify_approval,
    verify_locked_labels,
)


def _labels_and_predictions(page_count: int = 100) -> tuple[bytes, bytes]:
    labels = []
    predictions = []
    for number in range(page_count):
        source = hashlib.sha256(f"source-{number}".encode()).hexdigest()
        answers = [{"category": "single", "choices": [1]}] + [
            {"category": "unasked", "choices": []}
        ] * 99
        labels.append(
            {
                "schema_version": 1,
                "page_id": f"p{number:03d}",
                "source_sha256": source,
                "expected_processable": True,
                "expected_manual_review": False,
                "expected_id_digits": list("01234567"),
                "expected_answers": answers,
                "asked_questions": [1],
            }
        )
        predictions.append(
            {
                "schema_version": 1,
                "page_id": f"p{number:03d}",
                "source_sha256": source,
                "processing_status": "processed",
                "manual_review": False,
                "id_digits": [{"status": "normal", "value": digit} for digit in "01234567"],
                "answers": [{"status": "normal", "value": {"category": "single", "choices": [1]}}]
                + [{"status": "blank", "value": {"category": "blank", "choices": []}}] * 99,
            }
        )
    return b"".join(canonical_bytes(value) + b"\n" for value in labels), b"".join(
        canonical_bytes(value) + b"\n" for value in predictions
    )


def _signed_labels(labels: bytes) -> tuple[dict[str, str], dict[str, dict[str, str]], str]:
    assert sha256_bytes(labels) == (
        "37fff7fd639e157daed7744414af8e24031814a3536e983286f2b26424157c40"
    )
    trust = {
        "label-signer": {
            "public_key_base64": "KrC/RZ6+lZl6UuGHrOR2eg1Sw7s5PVa2zOMdizzOS5A=",
            "sha256_fingerprint": (
                "f43e7c6e66d1c3a778c727b17f19dc33386abae2f9aec3cfdd33b30b6855793b"
            ),
        }
    }
    return (
        {
            "key_id": "label-signer",
            "labels_sha256": ("37fff7fd639e157daed7744414af8e24031814a3536e983286f2b26424157c40"),
            "algorithm": "Ed25519",
            "signature_base64": (
                "lfMU8HYkirHT5jzjLtUFbDX62l1XZgrDJ2Lw2a8oRq3zOQQehtzJKLnMmkB/oL6l"
                "NbXib9k00tv3jBn+yrQ4BQ=="
            ),
        },
        trust,
        "3c9250f24207de80c3962fe1bd2d6e31a210dd5febb667ec942f6e02e26e04e7",
    )


def _prediction_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(record) + b"\n" for record in records)


def _evaluate_with_prediction_mutations(
    *,
    false_rejects: int = 0,
    manual_reviews: int = 0,
    page_failures: int = 0,
    page_count: int = 100,
) -> dict[str, Any]:
    labels, predictions = _labels_and_predictions(page_count)
    records = [json.loads(line) for line in predictions.splitlines()]
    for index in range(false_rejects):
        records[index]["id_digits"][0] = {"status": "uncertain", "value": None}
    for index in range(manual_reviews):
        records[index]["manual_review"] = True
    for index in range(page_failures):
        records[index].update(
            processing_status="failed",
            id_digits=[None] * 8,
            answers=[None] * 100,
        )
    return evaluate(labels, _prediction_bytes(records))


def test_evaluator_release_threshold_boundaries() -> None:
    false_reject_at_boundary = _evaluate_with_prediction_mutations(false_rejects=4)
    assert false_reject_at_boundary["verdict"]["passed"] is True
    assert false_reject_at_boundary["verdict"]["field_micro"] == {
        "denominator": 900,
        "false_confirm": 0,
        "false_reject": 4,
        "false_reject_percent": "0.4444",
    }

    false_reject_over_boundary = _evaluate_with_prediction_mutations(false_rejects=5)
    assert false_reject_over_boundary["verdict"]["passed"] is False
    assert false_reject_over_boundary["verdict"]["field_micro"]["denominator"] == 900
    assert false_reject_over_boundary["verdict"]["field_micro"]["false_reject"] == 5

    manual_review_at_boundary = _evaluate_with_prediction_mutations(manual_reviews=5)
    assert manual_review_at_boundary["verdict"]["passed"] is False
    assert manual_review_at_boundary["verdict"]["manual_review"] == {
        "numerator": 5,
        "denominator": 100,
        "percent": "5.0000",
    }
    assert manual_review_at_boundary["verdict"]["field_micro"]["false_reject"] == 45

    manual_review_over_boundary = _evaluate_with_prediction_mutations(manual_reviews=6)
    assert manual_review_over_boundary["verdict"]["passed"] is False
    assert manual_review_over_boundary["verdict"]["manual_review"] == {
        "numerator": 6,
        "denominator": 100,
        "percent": "6.0000",
    }
    assert manual_review_over_boundary["verdict"]["field_micro"]["false_reject"] == 54

    page_failure_at_boundary = _evaluate_with_prediction_mutations(page_failures=1)
    assert page_failure_at_boundary["verdict"]["passed"] is True
    assert page_failure_at_boundary["verdict"]["unexpected_page_failure"] == {
        "numerator": 1,
        "denominator": 100,
        "percent": "1.0000",
    }
    assert page_failure_at_boundary["verdict"]["field_micro"]["denominator"] == 891

    page_failure_over_boundary = _evaluate_with_prediction_mutations(page_failures=2)
    assert page_failure_over_boundary["verdict"]["passed"] is False
    assert page_failure_over_boundary["verdict"]["unexpected_page_failure"] == {
        "numerator": 2,
        "denominator": 100,
        "percent": "2.0000",
    }
    assert page_failure_over_boundary["verdict"]["field_micro"]["denominator"] == 882
    page_failure_over_cap = _evaluate_with_prediction_mutations(page_failures=2, page_count=200)
    assert page_failure_over_cap["verdict"]["passed"] is False
    assert page_failure_over_cap["verdict"]["unexpected_page_failure"] == {
        "numerator": 2,
        "denominator": 200,
        "percent": "1.0000",
    }
    assert page_failure_over_cap["verdict"]["field_micro"]["denominator"] == 1782


def test_locked_labels_reject_noncanonical_or_false_processable_shape() -> None:
    labels, _ = _labels_and_predictions()
    with pytest.raises(AccuracyError):
        validate_locked_labels(labels.replace(b'"schema_version":1', b'"schema_version": 1', 1))
    with pytest.raises(AccuracyError):
        validate_locked_labels(
            labels.replace(b'"expected_processable":true', b'"expected_processable":false', 1)
        )


def test_false_processable_is_a_release_failure() -> None:
    labels, predictions = _labels_and_predictions()
    label_records = [json.loads(line) for line in labels.splitlines()]
    prediction_records = [json.loads(line) for line in predictions.splitlines()]
    source = hashlib.sha256(b"unprocessable").hexdigest()
    label_records.append(
        {
            "schema_version": 1,
            "page_id": "p100",
            "source_sha256": source,
            "expected_processable": False,
            "expected_manual_review": True,
            "expected_id_digits": [None] * 8,
            "expected_answers": [None] * 100,
            "asked_questions": [],
        }
    )
    prediction_records.append(
        {
            "schema_version": 1,
            "page_id": "p100",
            "source_sha256": source,
            "processing_status": "processed",
            "manual_review": False,
            "id_digits": [{"status": "normal", "value": "0"}] * 8,
            "answers": [{"status": "blank", "value": {"category": "blank", "choices": []}}] * 100,
        }
    )
    result = evaluate(
        b"".join(canonical_bytes(value) + b"\n" for value in label_records),
        b"".join(canonical_bytes(value) + b"\n" for value in prediction_records),
    )
    assert result["verdict"]["processability"]["false_processable"] == 1
    assert result["verdict"]["passed"] is False


def test_locked_labels_require_pinned_signature_and_reject_tampering() -> None:
    labels, _ = _labels_and_predictions()
    signature, trust, trust_digest = _signed_labels(labels)
    assert verify_locked_labels(labels, signature, trust, trust_digest)
    assert not verify_locked_labels(labels + b" ", signature, trust, trust_digest)

    altered_signature = dict(signature)
    altered_signature["signature_base64"] = "A" + signature["signature_base64"][1:]
    assert not verify_locked_labels(labels, altered_signature, trust, trust_digest)

    altered_key_id = dict(signature)
    altered_key_id["key_id"] = "other-signer"
    assert not verify_locked_labels(labels, altered_key_id, trust, trust_digest)

    altered_fingerprint = {
        "label-signer": {
            **trust["label-signer"],
            "sha256_fingerprint": "0" * 64,
        }
    }
    assert not verify_locked_labels(labels, signature, altered_fingerprint, trust_digest)
    assert not verify_locked_labels(labels, signature, trust, "0" * 64)


def test_page_failures_are_explicit_and_excluded_from_successful_field_denominator() -> None:
    labels, predictions = _labels_and_predictions()
    records = [json.loads(line) for line in predictions.splitlines()]
    records[0]["processing_status"] = "failed"
    records[0]["id_digits"] = [None] * 8
    records[0]["answers"] = [None] * 100
    result = evaluate(labels, b"".join(canonical_bytes(record) + b"\n" for record in records))
    assert result["verdict"]["field_micro"]["denominator"] == 891
    assert result["verdict"]["field_micro"]["false_reject"] == 0
    assert result["categories"]["id_digit"]["unscored_due_page_failure"] == 8
    assert result["categories"]["answer_single"]["unscored_due_page_failure"] == 1
    assert result["verdict"]["unexpected_page_failure"]["numerator"] == 1


def test_ed25519_rfc8032_vector_and_forgery_rejections() -> None:
    public_key = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert evaluator._ed25519_verify(public_key, signature, b"")
    assert not evaluator._ed25519_verify(b"\0" * 32, b"\0" * 64, b"")
    assert not evaluator._ed25519_verify(public_key, b"\0" * 64, b"")
    assert not evaluator._ed25519_verify(public_key, signature[:-1], b"")

_TEST_Q = 2**255 - 19
_TEST_D = -121665 * pow(121666, _TEST_Q - 2, _TEST_Q) % _TEST_Q
_TEST_I = pow(2, (_TEST_Q - 1) // 4, _TEST_Q)
_TEST_L = 2**252 + 27742317777372353535851937790883648493


def _test_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_TEST_D * y * y + 1, _TEST_Q - 2, _TEST_Q) % _TEST_Q
    x = pow(xx, (_TEST_Q + 3) // 8, _TEST_Q)
    return x if x * x % _TEST_Q == xx else x * _TEST_I % _TEST_Q


_TEST_BASE = (
    _TEST_Q - _test_xrecover(4 * pow(5, _TEST_Q - 2, _TEST_Q) % _TEST_Q),
    4 * pow(5, _TEST_Q - 2, _TEST_Q) % _TEST_Q,
)


def _test_add(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = left
    x2, y2 = right
    return (
        (x1 * y2 + x2 * y1) * pow(1 + _TEST_D * x1 * x2 * y1 * y2, _TEST_Q - 2, _TEST_Q)
        % _TEST_Q,
        (y1 * y2 + x1 * x2) * pow(1 - _TEST_D * x1 * x2 * y1 * y2, _TEST_Q - 2, _TEST_Q)
        % _TEST_Q,
    )


def _test_scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    while scalar:
        if scalar & 1:
            result = _test_add(result, point)
        point = _test_add(point, point)
        scalar >>= 1
    return result


def _test_encode(point: tuple[int, int]) -> bytes:
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _test_ed25519_sign(seed: bytes, message: bytes) -> tuple[bytes, bytes]:
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    public_key = _test_encode(_test_scalarmult(_TEST_BASE, scalar))
    nonce = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), "little") % _TEST_L
    encoded_r = _test_encode(_test_scalarmult(_TEST_BASE, nonce))
    challenge = int.from_bytes(hashlib.sha512(encoded_r + public_key + message).digest(), "little")
    signature = encoded_r + ((nonce + challenge * scalar) % _TEST_L).to_bytes(32, "little")
    return public_key, signature


def _reference_canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _reference_receipt_digest(receipt: dict[str, Any]) -> str:
    preimage = dict(receipt)
    preimage["approval"] = dict(receipt["approval"])
    preimage["approval"]["receipt_digest"] = ""
    preimage["approval"]["signature_base64"] = ""
    return hashlib.sha256(_reference_canonical_bytes(preimage)).hexdigest()


def _receipt() -> tuple[dict[str, Any], dict[str, dict[str, str]], str]:
    receipt = {
        "schema_version": 1,
        "evaluator_version": "omr-accuracy-v1",
        "evaluator_tree_sha256": "1" * 64,
        "evaluator_exe_sha256": "2" * 64,
        "labels_sha256": "3" * 64,
        "fixture_manifest_sha256": "4" * 64,
        "ordered_source_sha256": [f"{number:064x}" for number in range(100)],
        "profile_sha256": "6" * 64,
        "threshold_version": "v1",
        "threshold_sha256": "7" * 64,
        "app_exe_sha256": "8" * 64,
        "predictions_sha256": "9" * 64,
        "confusion_sha256": "a" * 64,
        "runner": {"os": "test", "cpu": "test", "ram_bytes": 1, "run_at": "2026-01-01"},
        "verdict": {
            "passed": True,
            "field_micro": {
                "denominator": 900,
                "false_confirm": 0,
                "false_reject": 0,
                "false_reject_percent": "0.0000",
            },
            "field_macro_false_reject_percent": "0.0000",
            "manual_review": {"numerator": 0, "denominator": 100, "percent": "0.0000"},
            "unexpected_page_failure": {
                "numerator": 0,
                "denominator": 100,
                "percent": "0.0000",
            },
            "processability": {
                "correct_unprocessable": 0,
                "false_processable": 0,
                "processable": 100,
                "unexpected_page_failure": 0,
                "expected_processable_page_failure": 0,
            },
        },
        "approval": {
            "key_id": "owner",
            "signer_name": "OMR Grader Product Owner",
            "signed_at": "2026-01-01",
            "receipt_digest": "0" * 64,
            "algorithm": "Ed25519",
            "signature_base64": "",
        },
    }
    receipt_digest = _reference_receipt_digest(receipt)
    public_key, signature = _test_ed25519_sign(
        bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"),
        b"omr-accuracy-v1\0" + bytes.fromhex(receipt_digest),
    )
    trust = {
        "owner": {
            "public_key_base64": base64.b64encode(public_key).decode("ascii"),
            "sha256_fingerprint": hashlib.sha256(public_key).hexdigest(),
        }
    }
    receipt["approval"]["receipt_digest"] = receipt_digest
    receipt["approval"]["signature_base64"] = base64.b64encode(signature).decode("ascii")
    return receipt, trust, hashlib.sha256(_reference_canonical_bytes(trust)).hexdigest()


def test_fixed_manifest_and_receipt_preimage_are_canonical_and_sensitive() -> None:
    manifest = {
        "schema_version": 1,
        "root_name": "fixed",
        "entries": [{"path": "a.txt", "size": 3, "sha256": "b" * 64}],
    }
    manifest_bytes = (
        b'{"entries":[{"path":"a.txt","sha256":"'
        + b"b" * 64
        + b'","size":3}],"root_name":"fixed","schema_version":1}'
    )
    assert manifest_digest(manifest) == hashlib.sha256(manifest_bytes).hexdigest()

    receipt, trust, trust_digest = _receipt()
    preimage = dict(receipt)
    preimage["approval"] = dict(receipt["approval"])
    preimage["approval"]["receipt_digest"] = ""
    preimage["approval"]["signature_base64"] = ""
    preimage_bytes = _reference_canonical_bytes(preimage)
    assert hashlib.sha256(preimage_bytes).hexdigest() == receipt["approval"]["receipt_digest"]
    assert validate_receipt(receipt) == receipt
    assert verify_approval(receipt, trust, trust_digest)
    receipt["runner"]["os"] = "other"
    assert not verify_approval(receipt, trust, trust_digest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt, trust, trust_digest: receipt["approval"].update(receipt_digest="0" * 64),
        lambda receipt, trust, trust_digest: receipt["approval"].update(
            signature_base64=base64.b64encode(b"\0" * 64).decode("ascii")
        ),
        lambda receipt, trust, trust_digest: trust["owner"].update(
            sha256_fingerprint="0" * 64
        ),
        lambda receipt, trust, trust_digest: trust.update(
            other=trust.pop("owner")
        ),
        lambda receipt, trust, trust_digest: trust_digest[:-1]
        + ("0" if trust_digest[-1] != "0" else "1"),
    ],
)
def test_approval_trust_vector_rejects_mutations(mutate: Any) -> None:
    receipt, trust, trust_digest = _receipt()
    mutated_trust_digest = mutate(receipt, trust, trust_digest) or trust_digest
    assert not verify_approval(receipt, trust, mutated_trust_digest)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda receipt: receipt["verdict"]["field_micro"].update(
                false_reject_percent="0.0"
            ),
            "canonical percentage",
        ),
        (
            lambda receipt: receipt["verdict"]["manual_review"].update(percent="1.0000"),
            "does not match its counters",
        ),
        (
            lambda receipt: receipt["verdict"]["unexpected_page_failure"].update(
                denominator=99
            ),
            "processability totals",
        ),
        (
            lambda receipt: receipt["verdict"]["processability"].update(
                false_processable=1
            ),
            "processability totals",
        ),
    ],
)
def test_receipt_rejects_noncanonical_percentages_and_inconsistent_counters(
    mutate: Any, message: str
) -> None:
    receipt = _receipt()[0]
    mutate(receipt)
    with pytest.raises(AccuracyError, match=message):
        validate_receipt(receipt)
    receipt = _receipt()[0]
    receipt["verdict"]["passed"] = False
    with pytest.raises(AccuracyError, match="release thresholds"):
        validate_receipt(receipt)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.update(
            ordered_source_sha256=receipt["ordered_source_sha256"][:-1]
        ),
        lambda receipt: receipt.update(
            ordered_source_sha256=[
                receipt["ordered_source_sha256"][0]
            ]
                * len(receipt["ordered_source_sha256"])
        ),
        lambda receipt: receipt["verdict"]["processability"].update(processable=99),
        lambda receipt: receipt["verdict"]["manual_review"].update(numerator=101),
    ],
)
def test_receipt_rejects_derived_release_set_inconsistencies(mutate: Any) -> None:
    receipt = _receipt()[0]
    mutate(receipt)
    with pytest.raises(AccuracyError):
        validate_receipt(receipt)
_SECURE_DIR_FD_AVAILABLE = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


def test_tree_manifest_fails_closed_when_handle_relative_traversal_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evaluator.os, "supports_dir_fd", frozenset())
    with pytest.raises(AccuracyError, match="handle-relative"):
        tree_manifest(tmp_path)


@pytest.mark.skipif(
    not _SECURE_DIR_FD_AVAILABLE, reason="secure dir_fd traversal is unavailable"
)
def test_tree_manifest_rejects_same_size_change_after_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    original_hash = evaluator._hash_regular_file

    def mutate_after_hash(descriptor: int, expected: Any) -> str:
        digest = original_hash(descriptor, expected)
        source.write_bytes(b"changed!")
        return digest

    monkeypatch.setattr(evaluator, "_hash_regular_file", mutate_after_hash)
    with pytest.raises(AccuracyError, match="changed"):
        tree_manifest(tmp_path)


@pytest.mark.skipif(
    not _SECURE_DIR_FD_AVAILABLE, reason="secure dir_fd traversal is unavailable"
)
def test_tree_manifest_rejects_path_swap_after_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(b"original")
    original_hash = evaluator._hash_regular_file

    def swap_after_hash(descriptor: int, expected: Any) -> str:
        digest = original_hash(descriptor, expected)
        replacement.replace(source)
        return digest

    monkeypatch.setattr(evaluator, "_hash_regular_file", swap_after_hash)
    with pytest.raises(AccuracyError, match="changed while hashing"):
        tree_manifest(tmp_path)
@pytest.mark.skipif(
    not _SECURE_DIR_FD_AVAILABLE, reason="secure dir_fd traversal is unavailable"
)
def test_tree_manifest_rejects_swap_between_scan_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(b"changed!")
    original_open = evaluator.os.open

    def swap_before_open(name: object, *args: Any, **kwargs: Any) -> int:
        if name == "source.bin" and kwargs.get("dir_fd") is not None:
            replacement.replace(source)
        return original_open(name, *args, **kwargs)

    monkeypatch.setattr(evaluator.os, "open", swap_before_open)
    with pytest.raises(AccuracyError, match="changed"):
        tree_manifest(tmp_path)
@pytest.mark.skipif(
    not _SECURE_DIR_FD_AVAILABLE, reason="secure dir_fd traversal is unavailable"
)
def test_tree_manifest_rejects_nested_directory_swap_before_child_handle_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "evidence.bin").write_bytes(b"original")
    replacement = tmp_path.parent / f"{tmp_path.name}-replacement"
    displaced = tmp_path.parent / f"{tmp_path.name}-displaced"
    replacement.mkdir()
    (replacement / "evidence.bin").write_bytes(b"replacement")
    original_open = evaluator.os.open

    def swap_before_child_open(name: object, *args: Any, **kwargs: Any) -> int:
        if name == "nested" and kwargs.get("dir_fd") is not None:
            nested.replace(displaced)
            replacement.replace(nested)
        return original_open(name, *args, **kwargs)

    monkeypatch.setattr(evaluator.os, "open", swap_before_child_open)
    with pytest.raises(AccuracyError, match="changed directory"):
        tree_manifest(tmp_path)


@pytest.mark.skipif(
    not _SECURE_DIR_FD_AVAILABLE, reason="secure dir_fd traversal is unavailable"
)
def test_tree_manifest_rejects_nested_directory_swap_after_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "evidence.bin").write_bytes(b"original")
    replacement = tmp_path.parent / f"{tmp_path.name}-replacement"
    displaced = tmp_path.parent / f"{tmp_path.name}-displaced"
    replacement.mkdir()
    (replacement / "evidence.bin").write_bytes(b"replacement")
    original_stat = evaluator.os.stat

    def swap_after_traversal(name: object, *args: Any, **kwargs: Any) -> os.stat_result:
        if name == "nested" and kwargs.get("dir_fd") is not None:
            nested.replace(displaced)
            replacement.replace(nested)
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(evaluator.os, "stat", swap_after_traversal)
    with pytest.raises(AccuracyError, match="changed while hashing"):
        tree_manifest(tmp_path)
