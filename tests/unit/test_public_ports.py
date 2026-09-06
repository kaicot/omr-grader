"""Public application port contracts stay Result-only and concurrency-safe."""

from __future__ import annotations

import inspect
from typing import get_args, get_origin, get_type_hints

import omr_grader.application.dto as dto
import omr_grader.application.ports as ports
from omr_grader.domain.errors import Err, Ok, Result


def _is_result_annotation(annotation: object) -> bool:
    if get_origin(annotation) is Result:
        return True
    members = get_args(annotation)
    return (
        len(members) == 2 and Err in members and any(get_origin(member) is Ok for member in members)
    )


def test_every_public_protocol_method_returns_result() -> None:
    for port_name in ports.__all__:
        protocol = getattr(ports, port_name)
        for method_name, method in protocol.__dict__.items():
            if method_name.startswith("_") or not inspect.isfunction(method):
                continue
            annotation = get_type_hints(method)["return"]
            assert _is_result_annotation(annotation), (
                f"{port_name}.{method_name} must return Result"
            )


def test_session_lifecycle_operations_take_revisioned_operation_requests() -> None:
    request_annotations = get_type_hints(dto.SessionMutationRequest)
    assert {"session_id", "expected_revision", "operation_id"} <= request_annotations.keys()

    for method_name in (
        "delete_session",
        "restore_session",
        "permanently_delete_session",
    ):
        method_annotations = get_type_hints(getattr(ports.SessionLifecycleUseCase, method_name))
        assert method_annotations["request"] is dto.SessionMutationRequest
        assert _is_result_annotation(method_annotations["return"])


def test_coordinator_commit_authority_is_internal_only() -> None:
    assert "InternalSessionCoordinator" not in ports.__all__
    assert "CommittedSnapshotLease" not in ports.__all__
    assert ports.InternalSessionCoordinator is not None
    assert inspect.isfunction(ports.InternalSessionCoordinator.commit_generation)
    assert all(
        "commit_generation" not in getattr(ports, port_name).__dict__ for port_name in ports.__all__
    )
    coordinator_return = get_type_hints(ports.InternalSessionCoordinator.commit_generation)[
        "return"
    ]
    assert _is_result_annotation(coordinator_return)


def test_session_creation_ports_take_zero_revision_commands() -> None:
    scan_annotations = get_type_hints(dto.ScanCommand)
    import_annotations = get_type_hints(dto.ImportResponseCommand)

    assert tuple(scan_annotations) == (
        "session_id",
        "operation_id",
        "expected_revision",
        "exam_name",
        "exam_year",
        "exam_term",
        "profile_path",
        "roster_path",
        "source",
        "sensitivity",
        "multiprocessing",
    )
    assert tuple(import_annotations) == (
        "validation_token",
        "session_id",
        "operation_id",
        "expected_revision",
    )
    assert get_type_hints(ports.ScanUseCase.run_scan)["command"] is dto.ScanCommand
    assert (
        get_type_hints(ports.ResponseImportUseCase.import_response_book)["command"]
        is dto.ImportResponseCommand
    )


def test_finalization_settings_and_metadata_commands_carry_revision_and_operation_id() -> None:
    for command_type in (dto.FinalizeCommand, dto.SettingsSaveCommand, dto.MetadataEditCommand):
        annotations = get_type_hints(command_type)
        assert {"expected_revision", "operation_id"} <= annotations.keys()

    finalize_annotations = get_type_hints(ports.GradingUseCase.finalize)
    settings_annotations = get_type_hints(ports.SettingsUseCase.save_settings)
    metadata_annotations = get_type_hints(ports.MetadataUseCase.edit_metadata)
    assert finalize_annotations["command"] is dto.FinalizeCommand
    assert settings_annotations["command"] is dto.SettingsSaveCommand
    assert metadata_annotations["command"] is dto.MetadataEditCommand
    assert _is_result_annotation(finalize_annotations["return"])
    assert _is_result_annotation(settings_annotations["return"])
    assert _is_result_annotation(metadata_annotations["return"])
