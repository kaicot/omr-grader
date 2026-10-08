from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from omr_grader.ui.help_content import PAGE_SECTIONS, SECTIONS
from omr_grader.ui.main_window import MainWindow


def _current_key(window: MainWindow) -> str:
    item = window.help_dialog.toc.currentItem()
    return item.data(Qt.ItemDataRole.UserRole)


def test_every_section_has_an_anchor_and_a_toc_entry(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    dialog = window.help_dialog

    assert dialog.toc.count() == len(SECTIONS)
    assert [index for _, index in dialog._section_starts] == list(range(len(SECTIONS)))
    for section in SECTIONS:
        assert f"name='{section.key}'" in window._help_html
        assert section.title in window._help_html


def test_help_opens_at_the_chapter_for_the_current_screen(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    for page, key in enumerate(PAGE_SECTIONS):
        window.pages.setCurrentIndex(page)
        window.show_help()
        assert window.help_dialog.isVisible()
        assert _current_key(window) == key

    window.show_help("combined")
    assert _current_key(window) == "combined"


def test_f1_opens_help(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.help_shortcut.key() == QKeySequence(Qt.Key.Key_F1)
    window.help_shortcut.activated.emit()

    assert window.help_dialog.isVisible()


def test_search_selects_a_match_and_says_when_there_is_none(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    dialog = window.help_dialog

    dialog.search_edit.setText("전원")

    assert dialog.browser.textCursor().selectedText() == "전원"
    first = dialog.browser.textCursor().position()
    assert dialog.find_next()
    assert dialog.browser.textCursor().position() != first
    assert dialog.search_status.text() == ""

    dialog.search_edit.setText("없는말없는말")
    assert dialog.search_status.text() == "찾는 말이 없습니다"


def test_the_manual_follows_the_theme(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    window.toggle_theme()

    sheet = window.help_browser.document().defaultStyleSheet()
    assert "#26334a" in sheet  # dark table headers
    assert "text-decoration: underline" in sheet
    assert window.help_dialog.toc.count() == len(SECTIONS)
