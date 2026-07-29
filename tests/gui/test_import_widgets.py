from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from omr_grader.ui.import_widgets import ImportDropWidget, ImportKind, ImportSelection


def _drop(widget, paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDropEvent(
        QPointF(4, 4),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.dropEvent(event)


def test_pdf_drop_accepts_declared_pdf_without_opening_it(qtbot):
    widget = ImportDropWidget(ImportKind.PDF)
    qtbot.addWidget(widget)
    selected = []
    widget.selection_changed.connect(selected.append)

    _drop(widget, ["C:/not-present/scan.PDF"])

    assert selected == [ImportSelection(ImportKind.PDF, ("C:/not-present/scan.PDF",))]
    assert widget.selection == selected[0]


def test_folder_and_roster_drops_accept_declared_local_paths(qtbot):
    folder_widget = ImportDropWidget(ImportKind.FOLDER)
    roster_widget = ImportDropWidget(ImportKind.ROSTER)
    qtbot.addWidget(folder_widget)
    qtbot.addWidget(roster_widget)

    _drop(folder_widget, ["C:/not-present/scanned-pages"])
    _drop(roster_widget, ["C:/not-present/roster.XLSX"])

    assert folder_widget.selection == ImportSelection(
        ImportKind.FOLDER, ("C:/not-present/scanned-pages",)
    )
    assert roster_widget.selection == ImportSelection(
        ImportKind.ROSTER, ("C:/not-present/roster.XLSX",)
    )


def test_source_and_roster_reject_wrong_mode_extensions(qtbot):
    pdf_widget = ImportDropWidget(ImportKind.PDF)
    roster_widget = ImportDropWidget(ImportKind.ROSTER)
    qtbot.addWidget(pdf_widget)
    qtbot.addWidget(roster_widget)
    rejected = []
    pdf_widget.rejected.connect(rejected.append)
    roster_widget.rejected.connect(rejected.append)

    _drop(pdf_widget, ["C:/input/scans.png"])
    _drop(roster_widget, ["C:/input/roster.csv"])

    assert pdf_widget.selection is None
    assert roster_widget.selection is None
    assert rejected == [
        "PDF 파일만 선택할 수 있습니다.",
        "명단 엑셀 파일(.xlsx 또는 .xlsm)만 선택할 수 있습니다.",
    ]


def test_profile_drop_accepts_only_declared_template_without_opening_it(qtbot):
    widget = ImportDropWidget(ImportKind.PROFILE)
    qtbot.addWidget(widget)
    selected = []
    widget.selection_changed.connect(selected.append)

    _drop(widget, ["C:/outside/new.OMRTEMPLATE"])
    assert selected == [ImportSelection(ImportKind.PROFILE, ("C:/outside/new.OMRTEMPLATE",))]

    assert not widget.set_selection(("C:/outside/new.json",))
    assert widget.selection == selected[0]


def test_kind_switch_discards_incompatible_selection_but_cancel_preserves_current_selection(qtbot):
    widget = ImportDropWidget(ImportKind.FOLDER)
    qtbot.addWidget(widget)
    assert widget.set_selection(("C:/input/scans",))

    widget.set_kind(ImportKind.PDF)
    assert widget.selection is None
    assert widget.set_selection(("C:/input/scans.pdf",))
    selected = widget.selection
    widget.set_picker_cancelled()

    assert widget.selection == selected
    assert widget.property("pickerState") == "cancelled"


def test_busy_disabled_drop_cannot_mutate_existing_selection(qtbot):
    widget = ImportDropWidget(ImportKind.PDF)
    qtbot.addWidget(widget)
    assert widget.set_selection(("C:/input/first.pdf",))
    widget.setEnabled(False)

    _drop(widget, ["C:/input/second.pdf"])

    assert widget.selection == ImportSelection(ImportKind.PDF, ("C:/input/first.pdf",))


def test_picker_cancellation_preserves_selection_and_visible_state(qtbot):
    widget = ImportDropWidget(ImportKind.ROSTER)
    qtbot.addWidget(widget)
    assert widget.set_selection(("C:/input/roster.xlsx",))

    widget.set_picker_cancelled()

    assert widget.selection == ImportSelection(ImportKind.ROSTER, ("C:/input/roster.xlsx",))
    assert widget.property("pickerState") == "cancelled"
    assert "기존 선택을 유지" in widget._detail.text()


def test_click_requests_controller_browse_not_file_access(qtbot):
    widget = ImportDropWidget(ImportKind.FOLDER)
    qtbot.addWidget(widget)

    with qtbot.waitSignal(widget.browse_requested) as signal:
        qtbot.mouseClick(widget, Qt.MouseButton.LeftButton)

    assert signal.args == [ImportKind.FOLDER]
