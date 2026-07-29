"""Pure session identity and immutable generation-lineage mechanics."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping

from .enums import SourceKind
from .errors import Err, ErrorInfo, Ok, Result
from .models import (
    SCHEMA_VERSION,
    CurrentPointer,
    PageRef,
    SessionManifest,
    validate_portable_component,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INVALID_STEM = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _error(code: str, reason: str, field_path: str | None = None) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path, {"reason": reason}),))


def _stable_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"omr-grader/work-item/v1\x00")
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def stable_work_item_id(
    session_id: str,
    source_kind: SourceKind,
    source_sha256: str,
    page_number: int | None,
    frame_number: int | None,
    input_ordinal: int,
    duplicate_ordinal: int,
) -> Result[str]:
    """Return the deterministic work-item identity for one source unit."""
    if not isinstance(session_id, str) or not session_id:
        return _error("INVALID_SESSION_ID", "session_id must be a nonempty string", "session_id")
    if not isinstance(source_kind, SourceKind):
        return _error("INVALID_SOURCE_KIND", "source_kind must be a SourceKind", "source_kind")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        return _error(
            "INVALID_SOURCE_SHA256",
            "source_sha256 must be a lowercase SHA-256 digest",
            "source_sha256",
        )
    if type(input_ordinal) is not int or input_ordinal < 0:
        return _error(
            "INVALID_INPUT_ORDINAL", "input_ordinal must be a nonnegative integer", "input_ordinal"
        )
    if type(duplicate_ordinal) is not int or duplicate_ordinal < 0:
        return _error(
            "INVALID_DUPLICATE_ORDINAL",
            "duplicate_ordinal must be a nonnegative integer",
            "duplicate_ordinal",
        )
    for name, number in (("page_number", page_number), ("frame_number", frame_number)):
        if number is not None and (type(number) is not int or number < 1):
            return _error("INVALID_SOURCE_UNIT", f"{name} must be a positive integer or null", name)
    token = _stable_digest(
        session_id,
        source_kind.value,
        source_sha256,
        "" if page_number is None else str(page_number),
        "" if frame_number is None else str(frame_number),
        str(input_ordinal),
        str(duplicate_ordinal),
    )
    return Ok(f"wi_{token[:24]}")


def source_label(source_display_name: str, duplicate_ordinal: int) -> Result[str]:
    """Return a stable display label, numbering duplicate source names from two."""
    if not isinstance(source_display_name, str) or not source_display_name:
        return _error(
            "INVALID_SOURCE_NAME",
            "source_display_name must be a nonempty string",
            "source_display_name",
        )
    if type(duplicate_ordinal) is not int or duplicate_ordinal < 0:
        return _error(
            "INVALID_DUPLICATE_ORDINAL",
            "duplicate_ordinal must be a nonnegative integer",
            "duplicate_ordinal",
        )
    name = unicodedata.normalize("NFKC", source_display_name)
    return Ok(name if duplicate_ordinal == 0 else f"{name} ({duplicate_ordinal + 1})")


def artifact_stem(
    source_display_name: str, source_kind: SourceKind, work_item_id: str
) -> Result[str]:
    """Return a deterministic portable stem with a bounded human-readable base."""
    if not isinstance(source_display_name, str) or not source_display_name:
        return _error(
            "INVALID_SOURCE_NAME",
            "source_display_name must be a nonempty string",
            "source_display_name",
        )
    if not isinstance(source_kind, SourceKind):
        return _error("INVALID_SOURCE_KIND", "source_kind must be a SourceKind", "source_kind")
    if not isinstance(work_item_id, str) or re.fullmatch(r"wi_[0-9a-f]{24}", work_item_id) is None:
        return _error(
            "INVALID_WORK_ITEM_ID", "work_item_id must be a stable work-item ID", "work_item_id"
        )
    base = unicodedata.normalize("NFKC", source_display_name).rsplit(".", 1)[0]
    base = _INVALID_STEM.sub("_", base).strip(". ")
    base = re.sub(r"\s+", " ", base)
    if not base:
        base = "source"
    if base.split(".", 1)[0].upper().translate(str.maketrans("¹²³", "123")) in _RESERVED:
        base = f"source_{base}"
    base = base[:48].rstrip(". ") or "source"
    value = f"{base}_{source_kind.value}_{work_item_id}"
    try:
        validate_portable_component(value)
    except ValueError:
        return _error(
            "INVALID_ARTIFACT_STEM", "artifact stem is not Windows-safe", "source_display_name"
        )
    return Ok(value)


def build_page_ref(
    *,
    session_id: str,
    source_kind: SourceKind,
    source_sha256: str,
    source_display_name: str,
    page_number: int | None,
    frame_number: int | None,
    input_ordinal: int,
    duplicate_ordinal: int,
) -> Result[PageRef]:
    """Build the complete deterministic identity for an input page or frame."""
    work_item = stable_work_item_id(
        session_id,
        source_kind,
        source_sha256,
        page_number,
        frame_number,
        input_ordinal,
        duplicate_ordinal,
    )
    if isinstance(work_item, Err):
        return work_item
    label = source_label(source_display_name, duplicate_ordinal)
    if isinstance(label, Err):
        return label
    stem = artifact_stem(source_display_name, source_kind, work_item.value)
    if isinstance(stem, Err):
        return stem
    try:
        return Ok(
            PageRef(
                SCHEMA_VERSION,
                session_id,
                work_item.value,
                source_kind,
                source_sha256,
                source_display_name,
                label.value,
                page_number,
                frame_number,
                input_ordinal,
                duplicate_ordinal,
                stem.value,
            )
        )
    except ValueError as error:
        return _error("INVALID_PAGE_REF", str(error))


def validate_lineage(manifests: Iterable[SessionManifest], current: CurrentPointer) -> Result[None]:
    """Validate a complete immutable generation chain and its CURRENT authority pointer."""
    records = tuple(manifests)
    if not isinstance(current, CurrentPointer):
        return _error("INVALID_CURRENT_POINTER", "current must be a CurrentPointer", "current")
    if not records:
        return _error("LINEAGE_EMPTY", "a current pointer requires a generation", "manifests")
    if not all(isinstance(record, SessionManifest) for record in records):
        return _error(
            "INVALID_LINEAGE", "manifests must contain SessionManifest values", "manifests"
        )
    expected_session = current.session_id
    ordered = tuple(sorted(records, key=lambda record: record.revision))
    if records != ordered:
        return _error("LINEAGE_ORDER", "manifests must be revision ordered", "manifests")
    seen_generations: set[str] = set()
    previous: SessionManifest | None = None
    for manifest in records:
        if manifest.session_id != expected_session or manifest.generation_id in seen_generations:
            return _error(
                "LINEAGE_MISMATCH", "generation session or identity is inconsistent", "manifests"
            )
        seen_generations.add(manifest.generation_id)
        if previous is None:
            if manifest.revision != 1:
                return _error(
                    "LINEAGE_MISMATCH", "complete lineage must begin at revision one", "manifests"
                )
        elif (
            manifest.revision != previous.revision + 1
            or manifest.parent_revision != previous.revision
            or manifest.parent_generation_id != previous.generation_id
            or manifest.parent_manifest_sha256 != _manifest_digest(previous)
        ):
            return _error(
                "LINEAGE_MISMATCH", "generation parent does not match its predecessor", "manifests"
            )
        previous = manifest
    tail = records[-1]
    if (
        current.revision != tail.revision
        or current.generation_id != tail.generation_id
        or current.manifest_sha256 != _manifest_digest(tail)
    ):
        return _error(
            "CURRENT_POINTER_MISMATCH",
            "CURRENT does not identify the committed generation",
            "current",
        )
    if current.generation_relpath != f"generations/g{tail.revision:08d}_{tail.generation_id}":
        return _error(
            "CURRENT_POINTER_MISMATCH",
            "CURRENT generation path is not canonical",
            "current.generation_relpath",
        )
    return Ok(None)


def _manifest_digest(manifest: SessionManifest) -> str:
    """Canonical digest boundary for callers that construct CURRENT pointers."""
    import json

    encoded = (
        json.dumps(
            _normalize_json(manifest.to_dict()),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_json(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized[unicodedata.normalize("NFC", key)] = _normalize_json(item)
        return normalized
    return value


__all__ = [
    "artifact_stem",
    "build_page_ref",
    "source_label",
    "stable_work_item_id",
    "validate_lineage",
]
