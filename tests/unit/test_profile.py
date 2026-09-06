from __future__ import annotations

import json
from decimal import Decimal

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.profile import (
    MAX_PAGE_DIMENSION,
    MAX_PROFILE_DEPTH,
    MAX_PROFILE_REGIONS,
    MAX_PROFILE_STRING_LENGTH,
    parse_profile_bytes,
)


def _template(*, page: bool = True, starts: bool = False) -> dict[str, object]:
    regions: list[dict[str, object]] = [
        {
            "name": "student_id",
            "type": "id",
            "bbox_ratio": {"x": 0, "y": 0, "w": 0.1, "h": 0.1},
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    for index in range(5):
        region: dict[str, object] = {
            "name": f"answer_{index}",
            "type": "answer",
            "bbox_ratio": {"x": Decimal(index) / 5, "y": 0.2, "w": 0.1, "h": 0.7},
            "grid": {"cols": 5, "rows": 20},
        }
        if starts:
            region["question_start"] = index * 20 + 1
        regions.append(region)
    result: dict[str, object] = {"schema_version": 1, "profile_name": "OMR 100", "regions": regions}
    if page:
        result["page"] = {
            "orientation": "landscape",
            "aspect_ratio": 1.4,
            "source_width": 1400,
            "source_height": 1000,
        }
    return result


def _payload(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=float).encode("utf-8")


def _huge_integer_payload(value: dict[str, object]) -> bytes:
    return _payload(value).replace(b"999", b"9" * 10_000, 1)


def test_current_and_legacy_profiles_are_valid_and_legacy_warns() -> None:
    current = parse_profile_bytes(_payload(_template()))
    legacy = parse_profile_bytes(_payload(_template(page=False)))

    assert isinstance(current, Ok)
    assert current.value.page is not None
    assert isinstance(legacy, Ok)
    assert legacy.value.page is None
    assert [warning.code for warning in legacy.warnings] == ["LEGACY_PAGE_METADATA"]


def test_question_starts_must_have_exact_q1_to_q100_union() -> None:
    valid = parse_profile_bytes(_payload(_template(starts=True)))
    assert isinstance(valid, Ok)

    mixed = _template(starts=True)
    del mixed["regions"][1]["question_start"]  # type: ignore[index]
    gap = _template(starts=True)
    gap["regions"][2]["question_start"] = 22  # type: ignore[index]
    overlap = _template(starts=True)
    overlap["regions"][2]["question_start"] = 20  # type: ignore[index]
    for value in (mixed, gap, overlap):
        assert isinstance(parse_profile_bytes(_payload(value)), Err)


def test_schema_version_is_exact_and_rejects_boolean_and_huge_tokens() -> None:
    assert isinstance(parse_profile_bytes(_payload(_template())), Ok)

    for version in (0, 2, True):
        value = _template()
        value["schema_version"] = version
        assert isinstance(parse_profile_bytes(_payload(value)), Err)

    huge = _template()
    huge["schema_version"] = 999
    assert isinstance(parse_profile_bytes(_huge_integer_payload(huge)), Err)


@pytest.mark.parametrize("dimension", ("source_width", "source_height"))
def test_page_dimensions_have_bounded_integer_limits(dimension: str) -> None:
    for value in (1, MAX_PAGE_DIMENSION):
        template = _template()
        template["page"][dimension] = value  # type: ignore[index]
        assert isinstance(parse_profile_bytes(_payload(template)), Ok)

    for value in (0, MAX_PAGE_DIMENSION + 1, True):
        template = _template()
        template["page"][dimension] = value  # type: ignore[index]
        assert isinstance(parse_profile_bytes(_payload(template)), Err)

    huge = _template()
    huge["page"][dimension] = 999  # type: ignore[index]
    assert isinstance(parse_profile_bytes(_huge_integer_payload(huge)), Err)


def test_grid_integer_values_are_semantically_bounded_before_ranges() -> None:
    assert isinstance(parse_profile_bytes(_payload(_template())), Ok)

    mutations = (
        (0, "cols", 7),
        (0, "cols", 9),
        (0, "rows", 9),
        (0, "rows", 11),
        (1, "cols", 4),
        (1, "cols", 6),
        (1, "rows", 0),
        (1, "rows", 101),
        (0, "cols", True),
        (1, "rows", True),
    )
    for region_index, field, value in mutations:
        template = _template()
        template["regions"][region_index]["grid"][field] = value  # type: ignore[index]
        assert isinstance(parse_profile_bytes(_payload(template)), Err)

    for region_index, field in ((0, "cols"), (0, "rows"), (1, "cols"), (1, "rows")):
        huge = _template()
        huge["regions"][region_index]["grid"][field] = 999  # type: ignore[index]
        assert isinstance(parse_profile_bytes(_huge_integer_payload(huge)), Err)


def test_question_start_is_bounded_to_q1_through_q100() -> None:
    lower = _template(starts=True)
    lower["regions"][4]["grid"]["rows"] = 39  # type: ignore[index]
    lower["regions"][5]["grid"]["rows"] = 1  # type: ignore[index]
    lower["regions"][5]["question_start"] = 100  # type: ignore[index]
    assert isinstance(parse_profile_bytes(_payload(lower)), Ok)

    for start in (0, 101, True):
        template = _template(starts=True)
        template["regions"][1]["question_start"] = start  # type: ignore[index]
        assert isinstance(parse_profile_bytes(_payload(template)), Err)

    huge = _template(starts=True)
    huge["regions"][1]["question_start"] = 999  # type: ignore[index]
    assert isinstance(parse_profile_bytes(_huge_integer_payload(huge)), Err)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value["page"].update({"aspect_ratio": "NaN"}),  # type: ignore[index]
        lambda value: value["regions"][0]["bbox_ratio"].update({"w": 1.1}),  # type: ignore[index]
        lambda value: value["regions"].append(value["regions"][1]),  # type: ignore[index]
    ],
)
def test_unknown_nonfinite_and_invalid_semantics_are_errors(mutation: object) -> None:
    template = _template()
    mutation(template)  # type: ignore[operator]
    assert isinstance(parse_profile_bytes(_payload(template)), Err)


def test_canonical_hash_ignores_json_whitespace_and_object_key_order() -> None:
    value = _template()
    first = parse_profile_bytes(_payload(value))
    second = parse_profile_bytes(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=float
        ).encode()
    )

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value.sha256 == second.value.sha256


def test_canonical_hash_materializes_schema_and_question_start_defaults() -> None:
    explicit = _template(starts=True)
    omitted = _template()
    del omitted["schema_version"]
    first = parse_profile_bytes(_payload(explicit))
    second = parse_profile_bytes(_payload(omitted))

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value.sha256 == second.value.sha256


def test_canonical_hash_normalizes_numeric_wire_variants() -> None:
    value = _template()
    first = parse_profile_bytes(_payload(value))
    variant = (
        json.dumps(value, default=float)
        .replace("1.4", "1.400")
        .replace("0.7", "7e-1")
        .replace("0.2", "2e-1")
        .replace("0.1", "1e-1")
        .encode()
    )
    second = parse_profile_bytes(variant)

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value.sha256 == second.value.sha256


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("1e12", True),
        ("1e13", False),
        ("1e-12", True),
        ("1e-13", False),
        ("0.1234567890123456789012345678", True),
        ("0.12345678901234567890123456789", False),
    ],
)
def test_decimal_precision_and_exponent_boundaries(token: str, expected: bool) -> None:
    value = _template()
    if token in {"1e12", "1e13"}:
        value["page"]["aspect_ratio"] = token  # type: ignore[index]
        payload = _payload(value).replace(f'"{token}"'.encode(), token.encode())
    else:
        value["regions"][0]["bbox_ratio"]["w"] = token  # type: ignore[index]
        payload = _payload(value).replace(f'"{token}"'.encode(), token.encode())

    result = parse_profile_bytes(payload)

    assert isinstance(result, Ok) is expected


@pytest.mark.parametrize("token", ("1e1000000", "1e-1000000", "1" * 10_000 + ".0"))
def test_hostile_numeric_tokens_return_typed_errors(token: str) -> None:
    payload = _payload(_template()).replace(b"0.1", token.encode(), 1)

    result = parse_profile_bytes(payload)

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_PROFILE"


def test_practical_quota_boundaries_are_rejected() -> None:
    too_long = _template()
    too_long["profile_name"] = "x" * (MAX_PROFILE_STRING_LENGTH + 1)
    too_many = _template()
    too_many["regions"] = too_many["regions"] * (MAX_PROFILE_REGIONS + 1)
    deep: object = []
    for _ in range(MAX_PROFILE_DEPTH + 2):
        deep = [deep]

    assert isinstance(parse_profile_bytes(_payload(too_long)), Err)
    assert isinstance(parse_profile_bytes(_payload(too_many)), Err)
    assert isinstance(parse_profile_bytes(json.dumps(deep).encode()), Err)
