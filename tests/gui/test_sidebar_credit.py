from __future__ import annotations

from omr_grader import __version__
from omr_grader.ui.main_window import AUTHOR_CREDIT, MainWindow
from omr_grader.ui.theme import Theme, stylesheet_for


def test_the_sidebar_names_the_author_and_version_under_the_menu(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    text = window.credit_label.text()

    assert text == f"프로그램 제작\n{AUTHOR_CREDIT}\nv{__version__}"
    assert "kaic21@gmail.com" in AUTHOR_CREDIT
    layout = window.credit_label.parentWidget().layout()
    assert layout.indexOf(window.credit_label) == layout.indexOf(window.nav_buttons[-1]) + 2
    assert f"프로그램 제작: {AUTHOR_CREDIT} · v{__version__}" in window._help_html


def test_the_combine_button_is_styled_like_its_neighbours() -> None:
    for theme in Theme:
        sheet = stylesheet_for(theme)
        assert "QPushButton#dashboardCombineButton," in sheet
        assert "QPushButton#dashboardCombineButton:hover" in sheet
