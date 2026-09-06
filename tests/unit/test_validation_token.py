"""Response validation tokens retain a live, verified source file."""

from __future__ import annotations

import os
import pickle
from hashlib import sha256
from pathlib import Path

from omr_grader.application.validation_token import ResponseValidationToken, SourceFileIdentity
from omr_grader.domain.errors import Err, Ok


def _opened_token(path: Path) -> ResponseValidationToken:
    opened = ResponseValidationToken.open(str(path))
    assert isinstance(opened, Ok)
    return opened.value


def _error_code(result: Err) -> str:
    return result.errors[0].code


def test_response_token_opens_file_and_records_digest_and_identity(tmp_path: Path) -> None:
    source = tmp_path / "응답.xlsx"
    contents = b"validated response workbook"
    source.write_bytes(contents)

    token = _opened_token(source)
    try:
        source_stat = source.stat()
        assert token.canonical_path == str(source.resolve())
        assert token.source_sha256 == sha256(contents).hexdigest()
        assert token.source_identity == SourceFileIdentity(
            device=source_stat.st_dev,
            inode=source_stat.st_ino,
            size=len(contents),
            modified_ns=source_stat.st_mtime_ns,
        )
        assert isinstance(token.revalidate(), Ok)
    finally:
        token.close()


def test_response_token_context_manager_closes_and_rejects_later_use(tmp_path: Path) -> None:
    source = tmp_path / "responses.xlsx"
    source.write_bytes(b"initial")

    with _opened_token(source) as token:
        assert not token.closed
        assert isinstance(token.revalidate(), Ok)

    assert token.closed
    result = token.revalidate()
    assert isinstance(result, Err)
    assert _error_code(result) == "XLSX_VALIDATION_TOKEN_CLOSED"


def test_response_token_fails_closed_for_missing_serialized_or_replayed_sources(
    tmp_path: Path,
) -> None:
    missing = ResponseValidationToken.open(str(tmp_path / "missing.xlsx"))
    assert isinstance(missing, Err)
    assert _error_code(missing) == "XLSX_SOURCE_CHANGED"

    source = tmp_path / "responses.xlsx"
    source.write_bytes(b"verified")
    token = _opened_token(source)
    try:
        first = token.consume_for_import()
        assert isinstance(first, Ok)
        assert first.value.read() == b"verified"

        replay = token.consume_for_import()
        assert isinstance(replay, Err)
        assert _error_code(replay) == "XLSX_SOURCE_CHANGED"

        try:
            pickle.dumps(token)
        except TypeError:
            pass
        else:
            raise AssertionError("live validation token must not serialize")
    finally:
        token.close()


def test_response_token_detects_in_place_source_mutation(tmp_path: Path) -> None:
    source = tmp_path / "responses.xlsx"
    source.write_bytes(b"original")
    token = _opened_token(source)
    try:
        with source.open("r+b") as source_handle:
            source_handle.write(b"modified")
            source_handle.flush()
            os.fsync(source_handle.fileno())

        result = token.revalidate()
        assert isinstance(result, Err)
        assert _error_code(result) == "XLSX_SOURCE_CHANGED"
    finally:
        token.close()


def test_response_token_detects_source_replacement(tmp_path: Path) -> None:
    source = tmp_path / "responses.xlsx"
    replacement = tmp_path / "replacement.xlsx"
    source.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    token = _opened_token(source)
    try:
        try:
            os.replace(replacement, source)
        except PermissionError:
            # Windows may refuse replacement while a shared-delete handle is live.
            # That is also a valid fail-closed outcome: the validated source did not change.
            result = token.revalidate()
            assert isinstance(result, Ok)
            assert source.read_bytes() == b"original"
        else:
            result = token.revalidate()
            assert isinstance(result, Err)
            assert _error_code(result) == "XLSX_SOURCE_CHANGED"
    finally:
        token.close()
