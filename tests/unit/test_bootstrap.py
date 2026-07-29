from __future__ import annotations

import builtins
import logging
from pathlib import Path

import pytest

from omr_grader import bootstrap as bootstrap_module
from omr_grader.domain.errors import Err, ErrorInfo, Ok
from omr_grader.infrastructure import capabilities
from omr_grader.infrastructure.capabilities import CapabilityToken, RootCapability
from omr_grader.infrastructure.config_store import AppConfig
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.scan_runtime import ScanControllerAdapter


def test_bootstrap_prepares_storage_and_config_before_portable_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    token = CapabilityToken.for_testing(tmp_path)
    calls: list[str] = []

    def probe(received: ManagedPaths):
        calls.append("probe")
        assert received == paths
        return Ok(RootCapability(paths, True, token, None))

    def prepare(received: ManagedPaths, received_token: CapabilityToken):
        calls.append("managed")
        assert received == paths
        assert received_token is token
        return Ok(paths)

    config = AppConfig("", 3, True)

    def load(received: ManagedPaths, received_token: CapabilityToken):
        calls.append("config")
        assert received == paths
        assert received_token is token
        return Ok(config)

    def configure(log_path: Path | None = None):
        calls.append("logging")
        assert log_path == paths.data_dir / "omr-grader.log"
        return Ok(logging.getLogger("bootstrap-test"))

    original_import = builtins.__import__

    def reject_qt_import(name: str, *args: object, **kwargs: object):
        if name.startswith("PySide6"):
            raise AssertionError("non-GUI bootstrap must not import Qt")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(bootstrap_module, "probe_root_capability", probe)
    monkeypatch.setattr(bootstrap_module, "bootstrap_managed_paths", prepare)
    monkeypatch.setattr(bootstrap_module, "load_config", load)
    monkeypatch.setattr(bootstrap_module, "configure_logging", configure)
    monkeypatch.setattr(builtins, "__import__", reject_qt_import)

    result = bootstrap_module.bootstrap(paths)

    assert isinstance(result, Ok)
    assert result.value.config == config
    assert result.value.write_enabled is True
    assert calls == ["probe", "managed", "config", "logging"]


def test_bootstrap_disables_writes_when_default_config_save_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    token = CapabilityToken.for_testing(paths.root)
    save_failure = ErrorInfo(
        "ATOMIC_WRITE_FAILED",
        "error.atomic_write_failed",
        context={"reason": "설정 파일을 원자적으로 저장할 수 없습니다."},
        cause_type="OSError",
    )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_root_capability",
        lambda received: Ok(RootCapability(paths, True, token, None)),
    )
    monkeypatch.setattr(
        bootstrap_module, "bootstrap_managed_paths", lambda received, received_token: Ok(paths)
    )
    monkeypatch.setattr(
        bootstrap_module,
        "load_config",
        lambda received, received_token: Err((save_failure,)),
    )
    logging_paths: list[Path | None] = []
    monkeypatch.setattr(
        bootstrap_module,
        "configure_logging",
        lambda log_path=None: logging_paths.append(log_path)
        or Ok(logging.getLogger("bootstrap-test-save-failure")),
    )

    result = bootstrap_module.bootstrap(paths)

    assert isinstance(result, Ok)
    assert result.value.write_enabled is False
    assert result.value.diagnostic == save_failure.context["reason"]
    assert any(
        warning.code == save_failure.code and warning.cause_type == save_failure.cause_type
        for warning in result.warnings
    )
    assert logging_paths == [None]


def test_bootstrap_denied_root_returns_read_only_state_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "portable"
    root.mkdir()
    paths = ManagedPaths.from_root(root)
    before = tuple(tmp_path.rglob("*"))

    def unexpected_bootstrap(*args: object, **kwargs: object) -> None:
        raise AssertionError("read-only bootstrap must not prepare managed paths")

    monkeypatch.setattr(capabilities, "is_path_writable", lambda _: False)
    monkeypatch.setattr(bootstrap_module, "bootstrap_managed_paths", unexpected_bootstrap)
    monkeypatch.setattr(
        bootstrap_module,
        "configure_logging",
        lambda log_path=None: Ok(logging.getLogger("bootstrap-test-read-only")),
    )

    result = bootstrap_module.bootstrap(paths)

    assert isinstance(result, Ok)
    assert result.value.write_enabled is False
    assert result.value.diagnostic is not None
    assert "읽기 전용" in result.value.diagnostic
    assert tuple(tmp_path.rglob("*")) == before
    assert not paths.profiles_dir.exists()
    assert not paths.data_dir.exists()
    assert not paths.config_path.exists()


def test_scan_controller_adapter_keeps_commit_authority_explicit() -> None:
    calls: list[tuple[object, object]] = []

    class ScanOrchestration:
        def run_scan(self, command: object, coordinator: object):
            calls.append((command, coordinator))
            return Ok(None)

        def cancel_scan(self, command: object):
            return Ok(None)

    command = object()
    coordinator = object()
    adapter = ScanControllerAdapter(ScanOrchestration(), coordinator)  # type: ignore[arg-type]

    assert adapter.run_scan(command) == Ok(None)  # type: ignore[arg-type]
    assert calls == [(command, coordinator)]
