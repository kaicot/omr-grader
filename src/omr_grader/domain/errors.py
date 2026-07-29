"""Typed result and diagnostic contracts; neither result type is persisted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

type ErrorContextValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: str
    message_key: str
    field_path: str | None = None
    context: dict[str, ErrorContextValue] = field(default_factory=dict)
    retryable: bool = False
    cause_type: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or not self.code
            or type(self.message_key) is not str
            or self.message_key
            not in {f"error.{self.code.lower()}", f"warning.{self.code.lower()}"}
            or (self.field_path is not None and type(self.field_path) is not str)
            or type(self.context) is not dict
            or type(self.retryable) is not bool
            or (self.cause_type is not None and type(self.cause_type) is not str)
        ):
            raise ValueError("invalid error fields")
        if any(
            type(key) is not str or type(value) not in (str, int, bool, type(None))
            for key, value in self.context.items()
        ):
            raise ValueError("error context contains an unsupported value")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "field_path": self.field_path,
            "context": dict(self.context),
            "retryable": self.retryable,
            "cause_type": self.cause_type,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ErrorInfo:
        if type(value) is not dict or set(value) != {
            "code",
            "message_key",
            "field_path",
            "context",
            "retryable",
            "cause_type",
        }:
            raise ValueError("invalid error wire fields")
        code = value["code"]
        key = value["message_key"]
        path = value["field_path"]
        context = value["context"]
        retryable = value["retryable"]
        cause = value["cause_type"]
        if (
            type(code) is not str
            or type(key) is not str
            or (path is not None and type(path) is not str)
            or type(context) is not dict
            or type(retryable) is not bool
            or (cause is not None and type(cause) is not str)
        ):
            raise ValueError("invalid error fields")
        typed_context: dict[str, ErrorContextValue] = {}
        for name, item in context.items():
            if type(name) is not str or type(item) not in (str, int, bool, type(None)):
                raise ValueError("error context contains an unsupported value")
            typed_context[name] = item
        return cls(code, key, path, typed_context, retryable, cause)


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    value: T
    warnings: tuple[ErrorInfo, ...] = ()

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, ErrorInfo) and item.message_key.startswith("warning.")
            for item in self.warnings
        ):
            raise ValueError("Ok diagnostics must be warning.* ErrorInfo values")


@dataclass(frozen=True, slots=True)
class Err:
    errors: tuple[ErrorInfo, ...]

    def __post_init__(self) -> None:
        if not self.errors or not all(
            isinstance(item, ErrorInfo) and item.message_key.startswith("error.")
            for item in self.errors
        ):
            raise ValueError("Err diagnostics must be nonempty error.* ErrorInfo values")


type Result[T] = Ok[T] | Err
