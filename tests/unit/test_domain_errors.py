from __future__ import annotations

import pytest

from omr_grader.domain.errors import Err, ErrorInfo, Ok


def error(*, warning: bool = False, **overrides: object) -> ErrorInfo:
    code = str(overrides.pop("code", "INVALID_INPUT"))
    return ErrorInfo(
        code=code,
        message_key=str(
            overrides.pop("message_key", f"{'warning' if warning else 'error'}.{code.lower()}")
        ),
        field_path=overrides.pop("field_path", "answers[0]"),
        context=overrides.pop("context", {"question": 1, "retry": False}),
        retryable=overrides.pop("retryable", False),
        cause_type=overrides.pop("cause_type", None),
        **overrides,
    )


def test_result_diagnostic_polarity_is_exact() -> None:
    warning = error(warning=True)
    failure = error()

    result = Ok(value={"accepted": True}, warnings=(warning,))
    assert result.value == {"accepted": True}
    assert result.warnings == (warning,)

    with pytest.raises(ValueError, match="warning"):
        Ok(value=None, warnings=(failure,))
    with pytest.raises(ValueError, match="nonempty"):
        Err(errors=())
    with pytest.raises(ValueError, match="error"):
        Err(errors=(warning,))
    with pytest.raises(TypeError):
        Err(errors=(failure,), warnings=(warning,))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("kwargs", "exception"),
    [
        ({"code": ""}, ValueError),
        ({"message_key": "other"}, ValueError),
        ({"code": "INVALID_INPUT", "message_key": "error.other"}, ValueError),
        ({"context": {"score": 0.5}}, ValueError),
        ({"context": {1: "not-a-string-key"}}, ValueError),
    ],
)
def test_error_info_rejects_invalid_authoritative_metadata(
    kwargs: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        error(**kwargs)


def test_error_info_direct_and_wire_construction_are_symmetric() -> None:
    expected = {
        "code": "INVALID_INPUT",
        "message_key": "error.invalid_input",
        "field_path": "answers[0]",
        "context": {"question": 1, "retry": False},
        "retryable": True,
        "cause_type": "ValueError",
    }

    direct = ErrorInfo(
        "INVALID_INPUT",
        "error.invalid_input",
        "answers[0]",
        {"question": 1, "retry": False},
        True,
        "ValueError",
    )
    from_wire = ErrorInfo.from_dict(expected)

    assert direct.to_dict() == expected
    assert from_wire == direct
    assert ErrorInfo.from_dict(direct.to_dict()) == direct

    malformed = expected | {"unexpected": "value"}
    with pytest.raises(ValueError, match="wire fields"):
        ErrorInfo.from_dict(malformed)


def test_err_requires_errors_and_ok_requires_warning_diagnostics() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        Err(errors=())
    with pytest.raises(ValueError, match="nonempty"):
        Err(errors=("not-an-error",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="warning"):
        Ok(value=None, warnings=("not-a-warning",))  # type: ignore[arg-type]
