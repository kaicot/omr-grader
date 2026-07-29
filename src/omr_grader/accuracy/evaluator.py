"""Strict offline implementation of the frozen ``omr-accuracy-v1`` release gate.

This module deliberately has no network, clock, or signing capability.  It only
validates supplied evidence and verifies an already-created Product Owner
approval.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

EVALUATOR_VERSION = "omr-accuracy-v1"
_LABEL_KEYS = frozenset(
    (
        "schema_version",
        "page_id",
        "source_sha256",
        "expected_processable",
        "expected_manual_review",
        "expected_id_digits",
        "expected_answers",
        "asked_questions",
    )
)
_PREDICTION_KEYS = frozenset(
    (
        "schema_version",
        "page_id",
        "source_sha256",
        "processing_status",
        "manual_review",
        "id_digits",
        "answers",
    )
)
_RECEIPT_KEYS = frozenset(
    (
        "schema_version",
        "evaluator_version",
        "evaluator_tree_sha256",
        "evaluator_exe_sha256",
        "labels_sha256",
        "fixture_manifest_sha256",
        "ordered_source_sha256",
        "profile_sha256",
        "threshold_version",
        "threshold_sha256",
        "app_exe_sha256",
        "predictions_sha256",
        "confusion_sha256",
        "runner",
        "verdict",
        "approval",
    )
)
_LABEL_SIGNATURE_KEYS = frozenset(("key_id", "labels_sha256", "algorithm", "signature_base64"))
_TRUST_ENTRY_KEYS = frozenset(("public_key_base64", "sha256_fingerprint"))
_L = 2**252 + 27742317777372353535851937790883648493


class AccuracyError(ValueError):
    """Evidence is malformed, inconsistent, or does not meet the locked gate."""


def canonical_bytes(value: Any) -> bytes:
    """Return the one permitted JSON representation (UTF-8, sorted, no spaces)."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AccuracyError("value cannot be canonically encoded") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_evidence_snapshot(raw: bytes | str | Path, name: str = "evidence") -> bytes:
    """Read evidence once into immutable bytes, converting filesystem errors to AccuracyError."""
    if isinstance(raw, bytes):
        return raw
    try:
        if isinstance(raw, Path):
            return raw.read_bytes()
        path = Path(raw)
        return path.read_bytes() if "\n" not in raw and path.is_file() else raw.encode("utf-8")
    except OSError as exc:
        raise AccuracyError(f"unreadable {name}") from exc


def _require_sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise AccuracyError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _exact_object(value: Any, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AccuracyError(f"{name} has invalid keys")
    return value


def _answer(value: Any, *, allow_unasked: bool) -> dict[str, Any]:
    item = _exact_object(value, frozenset(("category", "choices")), "answer")
    category, choices = item["category"], item["choices"]
    if category not in {"single", "multiple", "blank"} | ({"unasked"} if allow_unasked else set()):
        raise AccuracyError("invalid answer category")
    if not isinstance(choices, list) or any(
        type(choice) is not int or not 1 <= choice <= 5 for choice in choices
    ):
        raise AccuracyError("answer choices must be integer choices 1..5")
    if choices != sorted(set(choices)):
        raise AccuracyError("answer choices must be sorted and unique")
    required = {"single": 1, "multiple": None, "blank": 0, "unasked": 0}[category]
    if required is not None and len(choices) != required:
        raise AccuracyError("answer category has invalid choice count")
    if category == "multiple" and not 2 <= len(choices) <= 5:
        raise AccuracyError("multiple answer must have 2..5 choices")
    return {"category": category, "choices": choices}


def _label(value: Any) -> dict[str, Any]:
    item = _exact_object(value, _LABEL_KEYS, "locked label")
    if item["schema_version"] != 1 or not isinstance(item["page_id"], str) or not item["page_id"]:
        raise AccuracyError("invalid locked-label schema_version or page_id")
    _require_sha256(item["source_sha256"], "source_sha256")
    if (
        type(item["expected_processable"]) is not bool
        or type(item["expected_manual_review"]) is not bool
    ):
        raise AccuracyError("processability and manual-review labels must be booleans")
    digits, answers, asked = (
        item["expected_id_digits"],
        item["expected_answers"],
        item["asked_questions"],
    )
    if (
        not isinstance(digits, list)
        or len(digits) != 8
        or not isinstance(answers, list)
        or len(answers) != 100
        or not isinstance(asked, list)
    ):
        raise AccuracyError("locked label has invalid field lengths")
    if not item["expected_processable"]:
        if (
            item["expected_manual_review"] is not True
            or digits != [None] * 8
            or answers != [None] * 100
            or asked != []
        ):
            raise AccuracyError("unprocessable label is inconsistent")
        return item
    if any(
        type(digit) is not str or len(digit) != 1 or digit not in "0123456789" for digit in digits
    ):
        raise AccuracyError("processable label must have eight ASCII digits")
    normalized_answers = [_answer(answer, allow_unasked=True) for answer in answers]
    if any(
        type(question) is not int or not 1 <= question <= 100 for question in asked
    ) or asked != sorted(set(asked)):
        raise AccuracyError("asked_questions must be sorted unique question numbers")
    actual_asked = [
        index + 1
        for index, answer in enumerate(normalized_answers)
        if answer["category"] != "unasked"
    ]
    if asked != actual_asked or not asked:
        raise AccuracyError("asked_questions must exactly match non-unasked answers")
    return item


def validate_locked_labels(raw: bytes | str | Path) -> list[dict[str, Any]]:
    """Validate and return strict canonical locked labels, in UTF-8 page-ID order."""
    data = read_evidence_snapshot(raw)
    if not data or not data.endswith(b"\n") or b"\r" in data:
        raise AccuracyError("locked labels must be nonempty canonical JSONL with final LF")
    records: list[dict[str, Any]] = []
    for line in data[:-1].split(b"\n"):
        if not line:
            raise AccuracyError("locked labels cannot contain blank lines")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccuracyError("locked labels contain malformed JSON") from exc
        record = _label(value)
        if canonical_bytes(record) != line:
            raise AccuracyError("locked labels are not canonical JSONL")
        records.append(record)
    if not records or [record["page_id"].encode("utf-8") for record in records] != sorted(
        record["page_id"].encode("utf-8") for record in records
    ):
        raise AccuracyError("locked labels must be strictly sorted by UTF-8 page_id")
    if len({record["page_id"] for record in records}) != len(records) or len(
        {record["source_sha256"] for record in records}
    ) != len(records):
        raise AccuracyError("locked labels have duplicate page or source bindings")
    return records


def _verify_signature(
    *,
    artifact_digest: str,
    artifact_name: str,
    signature_envelope: Mapping[str, Any],
    trust: Mapping[str, Any],
    expected_trust_sha256: str,
    domain: bytes,
) -> bool:
    """Verify a signed artifact against a separately pinned trust-store digest."""
    try:
        _require_sha256(expected_trust_sha256, "expected trust-store SHA-256")
        if sha256_bytes(canonical_bytes(dict(trust))) != expected_trust_sha256:
            return False
        envelope = _exact_object(
            dict(signature_envelope), _LABEL_SIGNATURE_KEYS, f"{artifact_name} signature"
        )
    except (AccuracyError, TypeError):
        return False
    if (
        envelope["algorithm"] != "Ed25519"
        or envelope["labels_sha256"] != artifact_digest
        or not isinstance(envelope["key_id"], str)
        or not envelope["key_id"]
        or not isinstance(envelope["signature_base64"], str)
    ):
        return False
    key = trust.get(envelope["key_id"])
    if not isinstance(key, dict) or set(key) != _TRUST_ENTRY_KEYS:
        return False
    try:
        public_key = base64.b64decode(key["public_key_base64"], validate=True)
        signature = base64.b64decode(envelope["signature_base64"], validate=True)
    except (TypeError, ValueError):
        return False
    if (
        len(public_key) != 32
        or len(signature) != 64
        or not isinstance(key["sha256_fingerprint"], str)
        or sha256_bytes(public_key) != key["sha256_fingerprint"]
    ):
        return False
    return _ed25519_verify(
        public_key,
        signature,
        domain + b"\0" + bytes.fromhex(artifact_digest),
    )


def verify_locked_labels(
    raw: bytes | str | Path,
    signature_envelope: Mapping[str, Any],
    trust: Mapping[str, Any],
    expected_trust_sha256: str,
) -> bool:
    """Independently authenticate locked-label bytes before they are evaluated."""
    try:
        data = read_evidence_snapshot(raw)
    except (AccuracyError, OSError):
        return False
    return _verify_signature(
        artifact_digest=sha256_bytes(data),
        artifact_name="locked-label",
        signature_envelope=signature_envelope,
        trust=trust,
        expected_trust_sha256=expected_trust_sha256,
        domain=b"omr-accuracy-v1/locked-labels",
    )


def _prediction(value: Any) -> dict[str, Any]:
    item = _exact_object(value, _PREDICTION_KEYS, "prediction")
    if item["schema_version"] != 1 or not isinstance(item["page_id"], str) or not item["page_id"]:
        raise AccuracyError("invalid prediction schema_version or page_id")
    _require_sha256(item["source_sha256"], "prediction source_sha256")
    if (
        item["processing_status"] not in {"processed", "failed"}
        or type(item["manual_review"]) is not bool
    ):
        raise AccuracyError("invalid prediction processing status")
    digits, answers = item["id_digits"], item["answers"]
    if (
        not isinstance(digits, list)
        or len(digits) != 8
        or not isinstance(answers, list)
        or len(answers) != 100
    ):
        raise AccuracyError("prediction has invalid field lengths")
    if item["processing_status"] == "failed":
        if digits != [None] * 8 or answers != [None] * 100:
            raise AccuracyError("failed prediction must not contain field values")
        return item
    for digit in digits:
        if (
            not isinstance(digit, dict)
            or set(digit) != {"status", "value"}
            or digit["status"] not in {"normal", "uncertain"}
        ):
            raise AccuracyError("invalid predicted ID field")
        if digit["status"] == "normal" and (
            type(digit["value"]) is not str
            or len(digit["value"]) != 1
            or digit["value"] not in "0123456789"
        ):
            raise AccuracyError("normal predicted ID must be an ASCII digit")
        if digit["status"] == "uncertain" and digit["value"] is not None:
            raise AccuracyError("uncertain predicted ID must have null value")
    for answer in answers:
        if (
            not isinstance(answer, dict)
            or set(answer) != {"status", "value"}
            or answer["status"] not in {"normal", "blank", "multiple", "uncertain"}
        ):
            raise AccuracyError("invalid predicted answer field")
        status, field_value = answer["status"], answer["value"]
        if status == "uncertain":
            if field_value is not None:
                raise AccuracyError("uncertain predicted answer must have null value")
        elif status == "normal":
            _answer(field_value, allow_unasked=False)
            if field_value["category"] != "single":
                raise AccuracyError("normal predicted answer must be single")
        elif status == "blank":
            if _answer(field_value, allow_unasked=False)["category"] != "blank":
                raise AccuracyError("blank prediction must be blank")
        elif _answer(field_value, allow_unasked=False)["category"] != "multiple":
            raise AccuracyError("multiple prediction must be multiple")
    return item


def validate_predictions(raw: bytes | str | Path) -> list[dict[str, Any]]:
    data = read_evidence_snapshot(raw)
    if not data or not data.endswith(b"\n") or b"\r" in data:
        raise AccuracyError("predictions must be nonempty canonical JSONL with final LF")
    records: list[dict[str, Any]] = []
    for line in data[:-1].split(b"\n"):
        if not line:
            raise AccuracyError("predictions cannot contain blank lines")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccuracyError("predictions contain malformed JSON") from exc
        record = _prediction(value)
        if canonical_bytes(record) != line:
            raise AccuracyError("predictions are not canonical JSONL")
        records.append(record)
    if [record["page_id"].encode("utf-8") for record in records] != sorted(
        record["page_id"].encode("utf-8") for record in records
    ):
        raise AccuracyError("predictions must be sorted by UTF-8 page_id")
    if len({record["page_id"] for record in records}) != len(records):
        raise AccuracyError("predictions have duplicate page IDs")
    return records


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        raise AccuracyError("empty denominator")
    return str(
        (Decimal(numerator) * Decimal(100) / Decimal(denominator)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    )


def evaluate(labels: bytes | str | Path, predictions: bytes | str | Path) -> dict[str, Any]:
    """Score canonical evidence and return the canonical confusion/verdict object."""
    label_records = validate_locked_labels(labels)
    if (
        len(label_records) < 100
        or sum(record["expected_processable"] for record in label_records) < 100
    ):
        raise AccuracyError(
            "locked release set requires at least 100 total and 100 processable pages"
        )
    prediction_records = validate_predictions(predictions)
    label_by_id = {record["page_id"]: record for record in label_records}
    prediction_by_id = {record["page_id"]: record for record in prediction_records}
    if set(label_by_id) != set(prediction_by_id):
        raise AccuracyError("predictions and locked labels must bind exactly the same pages")
    table = {
        kind: {
            "true_confirm": 0,
            "false_confirm": 0,
            "false_reject": 0,
            "uncertain": 0,
            "manual_hold": 0,
            "unscored_due_page_failure": 0,
        }
        for kind in ("id_digit", "answer_single", "answer_multiple", "answer_blank")
    }
    processability = {
        "correct_unprocessable": 0,
        "false_processable": 0,
        "processable": 0,
        "unexpected_page_failure": 0,
        "expected_processable_page_failure": 0,
    }
    held_pages = 0
    eligible_pages: list[tuple[int, int]] = []
    processable_pages = 0
    for label in label_records:
        prediction = prediction_by_id[label["page_id"]]
        if prediction["source_sha256"] != label["source_sha256"]:
            raise AccuracyError("prediction source hash does not match locked label")
        held = prediction["manual_review"] or prediction["processing_status"] == "failed"
        held_pages += held
        if not label["expected_processable"]:
            if held:
                processability["correct_unprocessable"] += 1
            else:
                processability["false_processable"] += 1
            continue
        processable_pages += 1
        expected_fields = [
            *(
                ("id_digit", digit, index)
                for index, digit in enumerate(label["expected_id_digits"])
            ),
            *(
                ("answer_" + answer["category"], answer, question)
                for question, answer in enumerate(label["expected_answers"])
                if answer["category"] != "unasked"
            ),
        ]
        if prediction["processing_status"] == "failed":
            processability["unexpected_page_failure"] += 1
            processability["expected_processable_page_failure"] += 1
            for kind, _expected, _index in expected_fields:
                table[kind]["unscored_due_page_failure"] += 1
            continue
        processability["processable"] += 1
        field_false_reject = 0
        for kind, expected, index in expected_fields:
            predicted = (
                prediction["id_digits"][index]
                if kind == "id_digit"
                else prediction["answers"][index]
            )
            auto = not held and predicted["status"] != "uncertain"
            if label["expected_manual_review"]:
                outcome = "false_confirm" if auto else "false_reject"
            elif not auto:
                outcome = "false_reject"
            else:
                outcome = "true_confirm" if predicted["value"] == expected else "false_confirm"
            table[kind][outcome] += 1
            if predicted["status"] == "uncertain":
                table[kind]["uncertain"] += 1
            if held:
                table[kind]["manual_hold"] += 1
            field_false_reject += outcome == "false_reject"
        if expected_fields:
            eligible_pages.append((field_false_reject, len(expected_fields)))
    denominator = sum(
        sum(values[key] for key in ("true_confirm", "false_confirm", "false_reject"))
        for values in table.values()
    )
    false_confirm = sum(values["false_confirm"] for values in table.values())
    false_reject = sum(values["false_reject"] for values in table.values())
    if denominator == 0 or not eligible_pages or processable_pages == 0:
        raise AccuracyError("empty required evaluator denominator")
    macro = sum(
        (Decimal(rejected) / Decimal(total) for rejected, total in eligible_pages), Decimal(0)
    ) / Decimal(len(eligible_pages))
    manual_rate = _percent(held_pages, len(label_records))
    failure_rate = _percent(processability["unexpected_page_failure"], processable_pages)
    automatic_only = {
        "denominator": denominator,
        "false_confirm": false_confirm,
        "false_reject": false_reject,
        "false_reject_percent": _percent(false_reject, denominator),
    }
    verdict = {
        "passed": false_confirm == 0
        and processability["false_processable"] == 0
        and false_reject * 1000 <= denominator * 5
        and held_pages * 100 <= len(label_records) * 5
        and processability["unexpected_page_failure"] * 100 <= processable_pages
        and processability["unexpected_page_failure"] <= 1,
        "field_micro": automatic_only,
        "field_macro_false_reject_percent": str(
            (macro * Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        ),
        "manual_review": {
            "numerator": held_pages,
            "denominator": len(label_records),
            "percent": manual_rate,
        },
        "unexpected_page_failure": {
            "numerator": processability["unexpected_page_failure"],
            "denominator": processable_pages,
            "percent": failure_rate,
        },
        "processability": processability,
    }
    return {"schema_version": 1, "categories": table, "verdict": verdict}


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mode == right.st_mode
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _hash_regular_file(descriptor: int, expected: os.stat_result) -> str:
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or _is_reparse(opened)
        or opened.st_nlink != 1
        or not _same_identity(expected, opened)
    ):
        raise AccuracyError("fixture/evaluator tree contains link, alias, or changed file")
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if not _same_identity(opened, os.fstat(descriptor)):
        raise AccuracyError("fixture/evaluator file changed while hashing")
    return digest.hexdigest()


def _open_tree_directory(name: str | Path, parent_fd: int | None = None) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        if parent_fd is None:
            return os.open(name, flags)
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise AccuracyError(f"unreadable fixture/evaluator directory: {name}") from exc


def tree_manifest(root: str | Path, root_name: str | None = None) -> dict[str, Any]:
    """Hash a tree through stable no-follow directory handles, or fail closed."""
    if (
        os.open not in os.supports_dir_fd
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise AccuracyError("secure handle-relative tree traversal is unsupported on this OS")
    root_path = Path(root)
    try:
        root_info = root_path.lstat()
    except OSError as exc:
        raise AccuracyError(f"unreadable fixture/evaluator root: {root_path}") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or _is_reparse(root_info)
    ):
        raise AccuracyError("fixture/evaluator root must be a real directory")

    root_fd = _open_tree_directory(root_path)
    directories: list[tuple[int, tuple[str, ...], os.stat_result]] = [(root_fd, (), root_info)]
    children: list[tuple[int, str, os.stat_result]] = []
    entries: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    excluded = {".git", ".hg", ".svn", "__pycache__"}
    try:
        index = 0
        while index < len(directories):
            directory_fd, relative, expected_directory = directories[index]
            index += 1
            actual_directory = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(actual_directory.st_mode)
                or _is_reparse(actual_directory)
                or not _same_identity(expected_directory, actual_directory)
            ):
                raise AccuracyError("fixture/evaluator tree contains changed directory")
            try:
                with os.scandir(os.dup(directory_fd)) as scan:
                    directory_children = list(scan)
            except OSError as exc:
                raise AccuracyError("unreadable fixture/evaluator directory") from exc
            for child in directory_children:
                if (
                    child.name in excluded
                    or child.name.endswith(".tmp")
                    or child.name.startswith("~")
                ):
                    continue
                try:
                    info = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise AccuracyError("unreadable fixture/evaluator entry") from exc
                if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                    raise AccuracyError("fixture/evaluator tree contains link or reparse alias")
                if stat.S_ISDIR(info.st_mode):
                    identity = (info.st_dev, info.st_ino)
                    if identity in seen:
                        raise AccuracyError("fixture/evaluator tree contains directory alias")
                    seen.add(identity)
                    directories.append(
                        (
                            _open_tree_directory(child.name, directory_fd),
                            relative + (child.name,),
                            info,
                        )
                    )
                    children.append((directory_fd, child.name, info))
                    continue
                if child.name.endswith(".pyc"):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise AccuracyError("fixture/evaluator tree contains link or alias")
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    raise AccuracyError("fixture/evaluator tree contains file alias")
                seen.add(identity)
                try:
                    descriptor = os.open(
                        child.name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        digest = _hash_regular_file(descriptor, info)
                    finally:
                        os.close(descriptor)
                except OSError as exc:
                    raise AccuracyError("unreadable fixture/evaluator file") from exc
                entries.append(
                    {
                        "path": "/".join(relative + (child.name,)),
                        "size": info.st_size,
                        "sha256": digest,
                    }
                )
                children.append((directory_fd, child.name, info))
        for directory_fd, _relative, expected_directory in directories:
            if not _same_identity(expected_directory, os.fstat(directory_fd)):
                raise AccuracyError("fixture/evaluator tree changed while hashing")
        for parent_fd, name, expected in children:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise AccuracyError("unreadable fixture/evaluator entry") from exc
            if not _same_identity(expected, current):
                raise AccuracyError("fixture/evaluator tree changed while hashing")
        try:
            if not _same_identity(root_info, root_path.lstat()):
                raise AccuracyError("fixture/evaluator tree changed while hashing")
        except OSError as exc:
            raise AccuracyError(f"unreadable fixture/evaluator root: {root_path}") from exc
    finally:
        for directory_fd, _relative, _expected in directories:
            os.close(directory_fd)
    entries.sort(key=lambda entry: entry["path"].encode("utf-8"))
    return {"schema_version": 1, "root_name": root_name or root_path.name, "entries": entries}


_APPROVAL_KEYS = frozenset(
    ("key_id", "signer_name", "signed_at", "receipt_digest", "algorithm", "signature_base64")
)
_CATEGORY_KEYS = frozenset(
    (
        "true_confirm",
        "false_confirm",
        "false_reject",
        "uncertain",
        "manual_hold",
        "unscored_due_page_failure",
    )
)
_PROCESSABILITY_KEYS = frozenset(
    (
        "correct_unprocessable",
        "false_processable",
        "processable",
        "unexpected_page_failure",
        "expected_processable_page_failure",
    )
)


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Return the approval preimage digest with both approval digest and signature blanked."""
    value = _exact_object(dict(receipt), _RECEIPT_KEYS, "receipt")
    blanked = dict(value)
    blanked["approval"] = dict(value["approval"])
    blanked["approval"]["receipt_digest"] = ""
    blanked["approval"]["signature_base64"] = ""
    return sha256_bytes(canonical_bytes(blanked))


def _validate_confusion(value: Any) -> dict[str, Any]:
    confusion = _exact_object(
        value, frozenset(("schema_version", "categories", "verdict")), "confusion"
    )
    if confusion["schema_version"] != 1 or not isinstance(confusion["categories"], dict):
        raise AccuracyError("invalid confusion")
    if set(confusion["categories"]) != {
        "id_digit",
        "answer_single",
        "answer_multiple",
        "answer_blank",
    }:
        raise AccuracyError("invalid confusion categories")
    for category in confusion["categories"].values():
        fields = _exact_object(category, _CATEGORY_KEYS, "confusion category")
        if any(type(count) is not int or count < 0 for count in fields.values()):
            raise AccuracyError("invalid confusion count")
    return confusion


def _canonical_percent(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 6
        or value.count(".") != 1
        or len(value.rsplit(".", 1)[1]) != 4
    ):
        raise AccuracyError(f"{name} must be a canonical percentage")
    whole, fraction = value.split(".", 1)
    if (
        not whole.isascii()
        or not whole.isdigit()
        or (len(whole) > 1 and whole.startswith("0"))
        or not fraction.isascii()
        or not fraction.isdigit()
        or Decimal(value) > Decimal(100)
    ):
        raise AccuracyError(f"{name} must be a canonical percentage")
    return value


def _validate_percent(value: Any, numerator: int, denominator: int, name: str) -> None:
    if (
        denominator <= 0
        or numerator > denominator
        or _canonical_percent(value, name) != _percent(numerator, denominator)
    ):
        raise AccuracyError(f"{name} does not match its counters")


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete signed-receipt contract before digest or signature verification."""
    value = _exact_object(dict(receipt), _RECEIPT_KEYS, "receipt")
    if value["schema_version"] != 1 or value["evaluator_version"] != EVALUATOR_VERSION:
        raise AccuracyError("unsupported receipt schema or evaluator")
    for name in (
        "evaluator_tree_sha256",
        "evaluator_exe_sha256",
        "labels_sha256",
        "fixture_manifest_sha256",
        "profile_sha256",
        "threshold_sha256",
        "app_exe_sha256",
        "predictions_sha256",
        "confusion_sha256",
    ):
        _require_sha256(value[name], name)
    if (
        not isinstance(value["threshold_version"], str)
        or not value["threshold_version"]
        or not isinstance(value["ordered_source_sha256"], list)
        or not value["ordered_source_sha256"]
    ):
        raise AccuracyError("invalid receipt evidence fields")
    for digest in value["ordered_source_sha256"]:
        _require_sha256(digest, "ordered_source_sha256")
    if len(set(value["ordered_source_sha256"])) != len(value["ordered_source_sha256"]):
        raise AccuracyError("ordered source hashes must be unique")
    # The receipt binds the full confusion by digest; its embedded verdict has its own exact shape.
    verdict = value["verdict"]
    if not isinstance(verdict, dict) or set(verdict) != {
        "passed",
        "field_micro",
        "field_macro_false_reject_percent",
        "manual_review",
        "unexpected_page_failure",
        "processability",
    }:
        raise AccuracyError("invalid receipt verdict")
    if type(verdict["passed"]) is not bool:
        raise AccuracyError("invalid receipt verdict values")
    _canonical_percent(verdict["field_macro_false_reject_percent"], "field macro percentage")
    counter_keys = {
        "field_micro": frozenset(
            ("denominator", "false_confirm", "false_reject", "false_reject_percent")
        ),
        "manual_review": frozenset(("numerator", "denominator", "percent")),
        "unexpected_page_failure": frozenset(("numerator", "denominator", "percent")),
    }
    counters: dict[str, dict[str, Any]] = {}
    for name, keys in counter_keys.items():
        field = _exact_object(verdict[name], keys, f"receipt {name}")
        if any(
            type(number) is not int or number < 0
            for key, number in field.items()
            if "percent" not in key
        ):
            raise AccuracyError("invalid receipt verdict counter")
        counters[name] = field
    field_micro = counters["field_micro"]
    if (
        field_micro["denominator"] <= 0
        or field_micro["false_confirm"] + field_micro["false_reject"] > field_micro["denominator"]
    ):
        raise AccuracyError("invalid field-micro counters")
    _validate_percent(
        field_micro["false_reject_percent"],
        field_micro["false_reject"],
        field_micro["denominator"],
        "field-micro false-reject percentage",
    )
    for name in ("manual_review", "unexpected_page_failure"):
        _validate_percent(
            counters[name]["percent"],
            counters[name]["numerator"],
            counters[name]["denominator"],
            f"{name} percentage",
        )
    processability = _exact_object(
        verdict["processability"], _PROCESSABILITY_KEYS, "processability"
    )
    if any(type(count) is not int or count < 0 for count in processability.values()):
        raise AccuracyError("invalid processability")
    if (
        processability["unexpected_page_failure"]
        != processability["expected_processable_page_failure"]
        or counters["unexpected_page_failure"]["numerator"]
        != processability["unexpected_page_failure"]
        or counters["unexpected_page_failure"]["denominator"]
        != processability["processable"] + processability["unexpected_page_failure"]
        or counters["manual_review"]["denominator"]
        != counters["unexpected_page_failure"]["denominator"]
        + processability["correct_unprocessable"]
        + processability["false_processable"]
    ):
        raise AccuracyError("receipt counters do not describe the processability totals")
    total_pages = counters["manual_review"]["denominator"]
    processable_pages = processability["processable"] + processability["unexpected_page_failure"]
    if (
        total_pages < 100
        or processable_pages < 100
        or len(value["ordered_source_sha256"]) != total_pages
        or counters["manual_review"]["numerator"]
        < processability["unexpected_page_failure"] + processability["correct_unprocessable"]
        or counters["manual_review"]["numerator"] > total_pages
        or processability["false_processable"] > total_pages - processable_pages
        or field_micro["false_confirm"] > field_micro["denominator"]
    ):
        raise AccuracyError("receipt counters violate release-set bounds")
    expected_passed = (
        field_micro["false_confirm"] == 0
        and processability["false_processable"] == 0
        and field_micro["false_reject"] * 1000 <= field_micro["denominator"] * 5
        and counters["manual_review"]["numerator"] * 100 <= total_pages * 5
        and processability["unexpected_page_failure"] * 100 <= processable_pages
        and processability["unexpected_page_failure"] <= 1
    )
    if verdict["passed"] is not expected_passed:
        raise AccuracyError("receipt passed verdict does not match release thresholds")
    runner = _exact_object(
        value["runner"], frozenset(("os", "cpu", "ram_bytes", "run_at")), "runner"
    )
    if (
        not all(isinstance(runner[key], str) and runner[key] for key in ("os", "cpu", "run_at"))
        or type(runner["ram_bytes"]) is not int
        or runner["ram_bytes"] < 0
    ):
        raise AccuracyError("invalid runner metadata")
    approval = _exact_object(value["approval"], _APPROVAL_KEYS, "approval")
    if (
        approval["signer_name"] != "OMR Grader Product Owner"
        or approval["algorithm"] != "Ed25519"
        or not all(isinstance(approval[key], str) and approval[key] for key in _APPROVAL_KEYS)
    ):
        raise AccuracyError("invalid Product Owner approval")
    _require_sha256(approval["receipt_digest"], "approval receipt digest")
    try:
        if len(base64.b64decode(approval["signature_base64"], validate=True)) != 64:
            raise AccuracyError("invalid approval signature")
    except (TypeError, ValueError) as exc:
        raise AccuracyError("invalid approval signature") from exc
    return value


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    value = _exact_object(
        dict(manifest), frozenset(("schema_version", "root_name", "entries")), "tree manifest"
    )
    if (
        value["schema_version"] != 1
        or not isinstance(value["root_name"], str)
        or not value["root_name"]
    ):
        raise AccuracyError("invalid tree manifest")
    if not isinstance(value["entries"], list):
        raise AccuracyError("invalid tree manifest entries")
    paths: list[bytes] = []
    for entry in value["entries"]:
        item = _exact_object(entry, frozenset(("path", "size", "sha256")), "tree manifest entry")
        if (
            not isinstance(item["path"], str)
            or not item["path"]
            or item["path"].startswith("/")
            or ".." in item["path"].split("/")
            or type(item["size"]) is not int
            or item["size"] < 0
        ):
            raise AccuracyError("invalid tree manifest entry")
        _require_sha256(item["sha256"], "tree manifest entry sha256")
        paths.append(item["path"].encode("utf-8"))
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise AccuracyError("tree manifest entries must be unique and UTF-8 sorted")
    return sha256_bytes(canonical_bytes(value))


def build_receipt(
    *,
    evaluator_tree_sha256: str,
    evaluator_exe_sha256: str,
    labels_sha256: str,
    fixture_manifest_sha256: str,
    ordered_source_sha256: Sequence[str],
    profile_sha256: str,
    threshold_version: str,
    threshold_sha256: str,
    app_exe_sha256: str,
    predictions_sha256: str,
    confusion: Mapping[str, Any],
    runner: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind supplied evidence; this function never creates a signature or approval."""
    _validate_confusion(dict(confusion))
    for name, value in {
        "evaluator_tree_sha256": evaluator_tree_sha256,
        "evaluator_exe_sha256": evaluator_exe_sha256,
        "labels_sha256": labels_sha256,
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "profile_sha256": profile_sha256,
        "threshold_sha256": threshold_sha256,
        "app_exe_sha256": app_exe_sha256,
        "predictions_sha256": predictions_sha256,
    }.items():
        _require_sha256(value, name)
    if (
        not isinstance(threshold_version, str)
        or not threshold_version
        or not isinstance(ordered_source_sha256, Sequence)
        or isinstance(ordered_source_sha256, str)
    ):
        raise AccuracyError("invalid threshold or ordered source hashes")
    source_hashes = list(ordered_source_sha256)
    if any(_require_sha256(value, "ordered_source_sha256") != value for value in source_hashes):
        raise AccuracyError("invalid ordered source hashes")
    if (
        not isinstance(runner, dict)
        or set(runner) != {"os", "cpu", "ram_bytes", "run_at"}
        or not all(isinstance(runner[key], str) and runner[key] for key in ("os", "cpu", "run_at"))
        or type(runner["ram_bytes"]) is not int
        or runner["ram_bytes"] < 0
    ):
        raise AccuracyError("invalid runner metadata")
    approval_value = _exact_object(dict(approval), _APPROVAL_KEYS, "approval")
    if (
        approval_value["signer_name"] != "OMR Grader Product Owner"
        or approval_value["algorithm"] != "Ed25519"
        or not all(
            isinstance(approval_value[key], str) and approval_value[key] for key in _APPROVAL_KEYS
        )
    ):
        raise AccuracyError("invalid Product Owner approval")
    receipt = {
        "schema_version": 1,
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_tree_sha256": evaluator_tree_sha256,
        "evaluator_exe_sha256": evaluator_exe_sha256,
        "labels_sha256": labels_sha256,
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "ordered_source_sha256": source_hashes,
        "profile_sha256": profile_sha256,
        "threshold_version": threshold_version,
        "threshold_sha256": threshold_sha256,
        "app_exe_sha256": app_exe_sha256,
        "predictions_sha256": predictions_sha256,
        "confusion_sha256": sha256_bytes(canonical_bytes(confusion)),
        "runner": dict(runner),
        "verdict": dict(confusion)["verdict"],
        "approval": dict(approval_value),
    }
    digest = receipt_digest(receipt)
    if receipt["approval"]["receipt_digest"] != digest:
        raise AccuracyError("approval receipt digest does not bind this receipt")
    return validate_receipt(receipt)


def verify_approval(
    receipt: Mapping[str, Any],
    trust: Mapping[str, Any],
    expected_trust_sha256: str,
) -> bool:
    """Verify a complete Product Owner receipt approval against a pinned trust store."""
    try:
        value = validate_receipt(receipt)
        approval = value["approval"]
        digest = receipt_digest(value)
        if approval["receipt_digest"] != digest:
            return False
        envelope = {
            "key_id": approval["key_id"],
            "labels_sha256": digest,
            "algorithm": approval["algorithm"],
            "signature_base64": approval["signature_base64"],
        }
        return _verify_signature(
            artifact_digest=digest,
            artifact_name="receipt",
            signature_envelope=envelope,
            trust=trust,
            expected_trust_sha256=expected_trust_sha256,
            domain=EVALUATOR_VERSION.encode("ascii"),
        )
    except (AccuracyError, TypeError):
        return False


# RFC 8032 verification, kept local to avoid adding a release dependency.
_Q = 2**255 - 19
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if x * x % _Q != xx:
        x = x * _I % _Q
    return x


_B_Y = 4 * pow(5, _Q - 2, _Q) % _Q
_B = (_Q - _xrecover(_B_Y), _B_Y)


def _decodepoint(data: bytes) -> tuple[int, int] | None:
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    y, sign = value & ((1 << 255) - 1), value >> 255
    if y >= _Q:
        return None
    x = _xrecover(y)
    if x & 1 != sign:
        x = _Q - x
    point = (x, y)
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _Q or _encodepoint(point) != data:
        return None
    return point


def _add(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = left
    x2, y2 = right
    denominator = pow(1 + _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    x3 = (x1 * y2 + x2 * y1) * denominator % _Q
    y3 = (y1 * y2 + x1 * x2) * pow(1 - _D * x1 * x2 * y1 * y2, _Q - 2, _Q) % _Q
    return x3, y3


def _scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encodepoint(point: tuple[int, int]) -> bytes:
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed25519_verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    point = _decodepoint(public_key)
    encoded_r, encoded_s = signature[:32], signature[32:]
    r = _decodepoint(encoded_r)
    scalar = int.from_bytes(encoded_s, "little")
    identity = (0, 1)
    if (
        point is None
        or r is None
        or scalar >= _L
        or _scalarmult(point, 8) == identity
        or _scalarmult(r, 8) == identity
    ):
        return False
    challenge = int.from_bytes(hashlib.sha512(encoded_r + public_key + message).digest(), "little")
    return _encodepoint(_scalarmult(_B, scalar)) == _encodepoint(
        _add(r, _scalarmult(point, challenge))
    )
