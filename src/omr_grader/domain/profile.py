"""Validated, canonical OMR template domain model and bounded JSON decoder."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Final, cast

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

MAX_PROFILE_BYTES: Final = 2 * 1024 * 1024
MAX_PROFILE_CODEPOINTS: Final = 2 * 1024 * 1024
MAX_PROFILE_DEPTH: Final = 32
MAX_PROFILE_NODES: Final = 10_000
MAX_PROFILE_KEYS: Final = 128
MAX_PROFILE_STRING_LENGTH: Final = 4_096
MAX_PROFILE_REGIONS: Final = 64
_SCHEMA_VERSION: Final = 1
_MAX_DECIMAL_PRECISION: Final = 28
_MAX_DECIMAL_ADJUSTED_EXPONENT: Final = 12
_MAX_DECIMAL_MAGNITUDE: Final = Decimal("1e12")
_MAX_DECIMAL_FIXED_LENGTH: Final = 32
_MAX_INTEGER_TOKEN_DIGITS: Final = 32
MAX_PAGE_DIMENSION: Final = 10_000


def _issue(code: str, reason: str, field_path: str | None = None) -> ErrorInfo:
    return ErrorInfo(code, f"error.{code.lower()}", field_path, {"reason": reason})


def _warning(code: str, reason: str, field_path: str | None = None) -> ErrorInfo:
    return ErrorInfo(code, f"warning.{code.lower()}", field_path, {"reason": reason})


@dataclass(frozen=True, slots=True)
class Page:
    orientation: str
    aspect_ratio: Decimal
    source_width: int
    source_height: int


@dataclass(frozen=True, slots=True)
class BoundingBoxRatio:
    x: Decimal
    y: Decimal
    w: Decimal
    h: Decimal


@dataclass(frozen=True, slots=True)
class Grid:
    cols: int
    rows: int


@dataclass(frozen=True, slots=True)
class ProfileRegion:
    name: str
    kind: str
    bbox_ratio: BoundingBoxRatio
    grid: Grid
    question_start: int | None = None


@dataclass(frozen=True, slots=True)
class Profile:
    """A semantic `.omrtemplate`; ``page is None`` denotes a legacy template."""

    schema_version: int
    profile_name: str
    page: Page | None
    regions: tuple[ProfileRegion, ...]
    sha256: str

    @property
    def id_region(self) -> ProfileRegion:
        return next(region for region in self.regions if region.kind == "id")

    @property
    def answer_regions(self) -> tuple[ProfileRegion, ...]:
        return tuple(region for region in self.regions if region.kind == "answer")


def _duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        normalized = unicodedata.normalize("NFC", key)
        if normalized in result:
            raise ValueError("duplicate object key")
        result[normalized] = value
    return result


def _validate_decimal(value: Decimal, path: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{path} must be a finite number")
    digits = len(value.as_tuple().digits)
    if digits > _MAX_DECIMAL_PRECISION:
        raise ValueError(f"{path} exceeds decimal precision limit")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{path} must be a finite number")
    fixed_length = (
        digits + exponent
        if exponent >= 0
        else digits + 1
        if digits + exponent > 0
        else 2 - exponent
    )
    if fixed_length > _MAX_DECIMAL_FIXED_LENGTH:
        raise ValueError(f"{path} exceeds fixed-point rendering limit")
    if value == 0:
        return value
    if (
        abs(value.adjusted()) > _MAX_DECIMAL_ADJUSTED_EXPONENT
        or value.copy_abs() > _MAX_DECIMAL_MAGNITUDE
    ):
        raise ValueError(f"{path} exceeds decimal magnitude limit")
    return value


def _canonical_decimal(value: Decimal) -> str:
    _validate_decimal(value, "decimal")
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    if len(rendered) > _MAX_DECIMAL_FIXED_LENGTH:
        raise ValueError("decimal exceeds fixed-point rendering limit")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _parse_decimal_token(token: str) -> Decimal:
    if len(token) > _MAX_DECIMAL_PRECISION + 16:
        raise ValueError("number is outside decimal limits")
    try:
        return _validate_decimal(Decimal(token), "number")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("number is outside decimal limits") from exc


def _canonical_json(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if type(value) is str:
        return json.dumps(unicodedata.normalize("NFC", value), ensure_ascii=False)
    if type(value) is list:
        return "[" + ",".join(_canonical_json(item) for item in cast(list[object], value)) + "]"
    if type(value) is dict:
        object_value = cast(dict[str, object], value)
        return (
            "{"
            + ",".join(
                _canonical_json(key) + ":" + _canonical_json(object_value[key])
                for key in sorted(object_value)
            )
            + "}"
        )
    raise ValueError("unsupported canonical JSON value")


def _bounded(value: object, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    counter = nodes if nodes is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_PROFILE_NODES:
        raise ValueError("node quota exceeded")
    if depth > MAX_PROFILE_DEPTH:
        raise ValueError("depth quota exceeded")
    if type(value) is str:
        if len(value) > MAX_PROFILE_STRING_LENGTH:
            raise ValueError("string quota exceeded")
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _bounded(item, depth=depth + 1, nodes=counter)
        return
    if type(value) is dict:
        object_value = cast(dict[str, object], value)
        if len(object_value) > MAX_PROFILE_KEYS:
            raise ValueError("key quota exceeded")
        for key, item in object_value.items():
            if len(key) > MAX_PROFILE_STRING_LENGTH:
                raise ValueError("string quota exceeded")
            _bounded(item, depth=depth + 1, nodes=counter)


def _object(value: object, path: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{path} must be an object")
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str], path: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{path} has unsupported field {sorted(unknown)[0]}")
    if missing:
        raise ValueError(f"{path} is missing field {sorted(missing)[0]}")


def _text(value: object, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{path} must be nonempty text")
    return unicodedata.normalize("NFC", value)


def _integer(
    value: object,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    exact: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{path} must be an integer")
    if exact is not None and value != exact:
        raise ValueError(f"{path} must be {exact}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{path} must be at most {maximum}")
    return value


def _parse_integer_token(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > _MAX_INTEGER_TOKEN_DIGITS:
        raise ValueError("integer is outside profile limits")
    return int(token)


def _ratio(value: object, path: str, *, positive: bool = False) -> Decimal:
    if type(value) not in (int, float, Decimal) or type(value) is bool:
        raise ValueError(f"{path} must be a finite number")
    try:
        decimal = _validate_decimal(Decimal(str(value)), path)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if positive and decimal <= 0:
        raise ValueError(f"{path} must be a finite positive number")
    return decimal


def _parse_region(value: object, index: int) -> ProfileRegion:
    path = f"regions[{index}]"
    raw = _object(value, path)
    allowed = {"name", "type", "bbox_ratio", "grid", "question_start"}
    unknown = set(raw) - allowed
    required = {"name", "type", "bbox_ratio", "grid"}
    if unknown or required - set(raw):
        detail = sorted(unknown or (required - set(raw)))[0]
        raise ValueError(f"{path} has invalid field {detail}")
    kind = _text(raw["type"], f"{path}.type")
    if kind not in {"id", "answer"}:
        raise ValueError(f"{path}.type is unsupported")
    bbox = _object(raw["bbox_ratio"], f"{path}.bbox_ratio")
    _exact_keys(bbox, {"x", "y", "w", "h"}, f"{path}.bbox_ratio")
    parsed_bbox = BoundingBoxRatio(
        _ratio(bbox["x"], f"{path}.bbox_ratio.x"),
        _ratio(bbox["y"], f"{path}.bbox_ratio.y"),
        _ratio(bbox["w"], f"{path}.bbox_ratio.w", positive=True),
        _ratio(bbox["h"], f"{path}.bbox_ratio.h", positive=True),
    )
    if (
        parsed_bbox.x < 0
        or parsed_bbox.y < 0
        or parsed_bbox.x + parsed_bbox.w > 1
        or parsed_bbox.y + parsed_bbox.h > 1
    ):
        raise ValueError(f"{path}.bbox_ratio exceeds page bounds")
    grid = _object(raw["grid"], f"{path}.grid")
    _exact_keys(grid, {"cols", "rows"}, f"{path}.grid")
    start: int | None = None
    if "question_start" in raw:
        if kind != "answer":
            raise ValueError(f"{path}.question_start is only supported for answer regions")
        start = _integer(raw["question_start"], f"{path}.question_start", minimum=1, maximum=100)
    return ProfileRegion(
        _text(raw["name"], f"{path}.name"),
        kind,
        parsed_bbox,
        Grid(
            _integer(grid["cols"], f"{path}.grid.cols", exact=8 if kind == "id" else 5),
            _integer(
                grid["rows"],
                f"{path}.grid.rows",
                exact=10 if kind == "id" else None,
                minimum=1 if kind == "answer" else None,
                maximum=100 if kind == "answer" else None,
            ),
        ),
        start,
    )


def _semantic_wire(
    profile_name: str,
    page: Page | None,
    regions: tuple[ProfileRegion, ...],
    intervals: list[range],
) -> dict[str, object]:
    wire_regions: list[dict[str, object]] = []
    answer_index = 0
    for region in regions:
        wire_region: dict[str, object] = {
            "name": region.name,
            "type": region.kind,
            "bbox_ratio": {
                "x": region.bbox_ratio.x,
                "y": region.bbox_ratio.y,
                "w": region.bbox_ratio.w,
                "h": region.bbox_ratio.h,
            },
            "grid": {"cols": region.grid.cols, "rows": region.grid.rows},
        }
        if region.kind == "answer":
            wire_region["question_start"] = intervals[answer_index].start
            answer_index += 1
        wire_regions.append(wire_region)
    wire: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "profile_name": profile_name,
        "regions": wire_regions,
    }
    if page is not None:
        wire["page"] = {
            "orientation": page.orientation,
            "aspect_ratio": page.aspect_ratio,
            "source_width": page.source_width,
            "source_height": page.source_height,
        }
    return wire


def parse_profile_bytes(payload: bytes) -> Result[Profile]:
    """Decode, validate and canonically hash a template without raising publicly."""
    try:
        if type(payload) is not bytes or len(payload) > MAX_PROFILE_BYTES:
            raise ValueError("profile byte quota exceeded")
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("profile must be UTF-8") from exc
        if len(source) > MAX_PROFILE_CODEPOINTS:
            raise ValueError("profile codepoint quota exceeded")
        raw = json.loads(
            source,
            parse_int=_parse_integer_token,
            parse_float=_parse_decimal_token,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")),
            object_pairs_hook=_duplicate_object,
        )
        _bounded(raw)
        root = _object(raw, "$")
        allowed = {"schema_version", "profile_name", "page", "regions"}
        legacy_allowed = {"schema_version", "profile_name", "regions"}
        unversioned_allowed = {"profile_name", "page", "regions"}
        unversioned_legacy_allowed = {"profile_name", "regions"}
        if set(root) not in (
            allowed,
            legacy_allowed,
            unversioned_allowed,
            unversioned_legacy_allowed,
        ):
            raise ValueError("profile has missing or unsupported fields")
        if "schema_version" in root:
            _integer(root["schema_version"], "schema_version", exact=_SCHEMA_VERSION)
        page: Page | None = None
        if "page" in root:
            raw_page = _object(root["page"], "page")
            _exact_keys(
                raw_page, {"orientation", "aspect_ratio", "source_width", "source_height"}, "page"
            )
            orientation = _text(raw_page["orientation"], "page.orientation")
            if orientation not in {"landscape", "portrait", "square"}:
                raise ValueError("page.orientation is unsupported")
            aspect_ratio = _ratio(raw_page["aspect_ratio"], "page.aspect_ratio", positive=True)
            if (
                (orientation == "landscape" and aspect_ratio <= 1)
                or (orientation == "portrait" and aspect_ratio >= 1)
                or (orientation == "square" and aspect_ratio != 1)
            ):
                raise ValueError("page.orientation and page.aspect_ratio disagree")
            page = Page(
                orientation,
                aspect_ratio,
                _integer(
                    raw_page["source_width"],
                    "page.source_width",
                    minimum=1,
                    maximum=MAX_PAGE_DIMENSION,
                ),
                _integer(
                    raw_page["source_height"],
                    "page.source_height",
                    minimum=1,
                    maximum=MAX_PAGE_DIMENSION,
                ),
            )
        raw_regions = root["regions"]
        if (
            type(raw_regions) is not list
            or not raw_regions
            or len(raw_regions) > MAX_PROFILE_REGIONS
        ):
            raise ValueError("regions must be a nonempty bounded array")
        regions = tuple(
            _parse_region(item, index) for index, item in enumerate(cast(list[object], raw_regions))
        )
        if len({region.name for region in regions}) != len(regions):
            raise ValueError("region names must be unique")
        ids = tuple(region for region in regions if region.kind == "id")
        answers = tuple(region for region in regions if region.kind == "answer")
        if len(ids) != 1:
            raise ValueError("exactly one id region is required")
        if len(answers) != 5:
            raise ValueError("exactly five answer regions are required")
        starts = tuple(region.question_start for region in answers)
        if any(start is None for start in starts) and any(start is not None for start in starts):
            raise ValueError("question_start must be all absent or all present")
        intervals: list[range] = []
        if all(start is None for start in starts):
            cursor = 1
            for region in answers:
                intervals.append(range(cursor, cursor + region.grid.rows))
                cursor += region.grid.rows
        else:
            intervals = [
                range(
                    cast(int, region.question_start),
                    cast(int, region.question_start) + region.grid.rows,
                )
                for region in answers
            ]
        if sum(region.grid.rows for region in answers) != 100:
            raise ValueError("answer rows must total 100")
        cursor = 1
        for interval in sorted(intervals, key=lambda item: item.start):
            if interval.start != cursor:
                raise ValueError(
                    "answer question ranges must cover Q1 through Q100 without gaps or overlaps"
                )
            cursor = interval.stop
        if cursor != 101:
            raise ValueError(
                "answer question ranges must cover Q1 through Q100 without gaps or overlaps"
            )
        profile_name = _text(root["profile_name"], "profile_name")
        canonical = _canonical_json(_semantic_wire(profile_name, page, regions, intervals)).encode(
            "utf-8"
        )
        profile = Profile(
            _SCHEMA_VERSION,
            profile_name,
            page,
            regions,
            hashlib.sha256(canonical).hexdigest(),
        )
        warnings = (
            (_warning("LEGACY_PAGE_METADATA", "용지 메타데이터가 없는 구형 프로필입니다.", "page"),)
            if page is None
            else ()
        )
        return Ok(profile, warnings)
    except (DecimalException, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        return Err((_issue("INVALID_PROFILE", str(exc)),))


parse_profile = parse_profile_bytes

__all__ = [
    "BoundingBoxRatio",
    "Grid",
    "MAX_PROFILE_BYTES",
    "MAX_PROFILE_CODEPOINTS",
    "MAX_PROFILE_DEPTH",
    "MAX_PROFILE_KEYS",
    "MAX_PROFILE_NODES",
    "MAX_PROFILE_REGIONS",
    "MAX_PROFILE_STRING_LENGTH",
    "Page",
    "Profile",
    "MAX_PAGE_DIMENSION",
    "ProfileRegion",
    "parse_profile",
    "parse_profile_bytes",
]
