"""Opt-in source Qt QA using a fresh copy of synthetic saved-session Data."""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtWidgets import QApplication

from omr_grader.domain.errors import Ok
from omr_grader.infrastructure.dashboard_repository import DashboardRepository
from omr_grader.infrastructure.logging_setup import configure_logging
from omr_grader.infrastructure.session_store import SessionStore
from omr_grader.ui.app_controller import AppController, ServicePorts
from omr_grader.ui.main_window import MainWindow


def hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-data", required=True, type=Path)
    parser.add_argument("--work-parent", required=True, type=Path)
    args = parser.parse_args()
    source = args.source_data.resolve(strict=True)
    parent = args.work_parent.resolve(strict=True)
    if parent == source or parent.is_relative_to(source):
        parser.error("the QA work parent must be outside the supplied Data")
    original = hashes(source)
    root = Path(tempfile.mkdtemp(prefix="dashboard-copy-", dir=parent))
    shutil.copytree(source, root / "Data")
    preserved = hashes(root / "Data")
    helpers = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "gui" / "test_dashboard_warning_transport.py")
    )
    store = SessionStore(root / "Data")
    helpers["add_unprojectable_session"](store)
    repository = DashboardRepository(
        store.root / "dashboard_index.json", store.discover_active_committed_leases
    )
    listings = []

    def load():
        result = repository.list_active()
        assert isinstance(result, Ok), result
        listings.append(result.value)
        return result

    configure_logging(root / "qa.log")
    app = QApplication([])
    window = MainWindow()
    controller = AppController(
        window,
        window.scan_page,
        window.grading_page,
        ServicePorts(dashboard_load=load),
        write_enabled=True,
    )
    phases: list[dict[str, object]] = []
    failures: list[str] = []
    poll = QTimer()

    def finish() -> None:
        poll.stop()
        controller.close()
        app.quit()

    def check() -> None:
        if controller._active_bridge is not None:
            return
        try:
            listing = listings[-1]
            assert len(listing.entries) == window.dashboard_page.model.rowCount() == 1
            assert window.dashboard_page.model.entry_at(0) == listing.entries[0]
            warning = next(
                item for item in listing.warnings if item.code == "DASHBOARD_SESSION_QUARANTINED"
            )
            assert window.status_label.text() == warning.context["reason"]
            phases.append(
                {
                    "phase": "startup" if not phases else "refresh",
                    "row_count": 1,
                    "revision": listing.entries[0].revision,
                    "warning_code": warning.code,
                    "displayed_warning": window.status_label.text(),
                }
            )
            if len(phases) == 1:
                controller._reload_dashboard()
            else:
                finish()
        except Exception as error:
            failures.append(repr(error))
            finish()

    def timeout() -> None:
        failures.append("Qt startup/refresh timed out")
        finish()

    poll.timeout.connect(check)
    poll.start(20)
    QTimer.singleShot(15_000, timeout)
    window.show()
    exit_code = app.exec()
    after = hashes(root / "Data")
    original_unchanged = hashes(source) == original
    copied_session_unchanged = all(
        after.get(path) == digest
        for path, digest in preserved.items()
        if path != "dashboard_index.json"
    )
    warning_logged = "DASHBOARD_SESSION_QUARANTINED" in (root / "qa.log").read_text(
        encoding="utf-8"
    )
    success = (
        not failures
        and len(phases) == 2
        and original_unchanged
        and copied_session_unchanged
        and warning_logged
    )
    result = {
        "result": "PASS" if success else "FAIL",
        "qt": qVersion(),
        "exit_code": exit_code,
        "phases": phases,
        "failures": failures,
        "source_data_unchanged": original_unchanged,
        "copied_session_bytes_unchanged": copied_session_unchanged,
        "warning_code_logged": warning_logged,
    }
    (root / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"REPORT {root / 'result.json'}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
