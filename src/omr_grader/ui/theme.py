"""Korean OMR Grader palettes and application stylesheet."""

from __future__ import annotations

from enum import Enum

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


class Theme(str, Enum):
    """Supported application color schemes."""

    LIGHT = "light"
    DARK = "dark"


_LIGHT = {
    "window": "#F4F7FB",
    "surface": "#FFFFFF",
    "sidebar": "#102A43",
    "sidebar_hover": "#1B3E5C",
    "sidebar_active": "#1E5A8A",
    "text": "#172B4D",
    "muted": "#667085",
    "border": "#D9E2EC",
    "primary": "#2563EB",
    "success": "#15803D",
    "error": "#B42318",
    "disabled": "#98A2B3",
}

_DARK = {
    "window": "#17212B",
    "surface": "#22303C",
    "sidebar": "#0B1F33",
    "sidebar_hover": "#173854",
    "sidebar_active": "#215A84",
    "text": "#F1F5F9",
    "muted": "#B2C2D1",
    "border": "#405467",
    "primary": "#7DB5FF",
    "success": "#6EE7A0",
    "error": "#FF9B8F",
    "disabled": "#8796A5",
}


def palette_for(theme: Theme | str) -> QPalette:
    """Return a Qt palette with sufficient contrast for the selected theme."""
    values = _DARK if Theme(theme) is Theme.DARK else _LIGHT
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(values["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(values["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(values["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(values["window"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(values["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(values["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(values["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(values["primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(values["disabled"])
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(values["disabled"])
    )
    return palette


def stylesheet_for(theme: Theme | str) -> str:
    """Return the QSS used by the main shell and its content pages."""
    values = _DARK if Theme(theme) is Theme.DARK else _LIGHT
    return f"""
        QWidget {{
            background: {values["window"]}; color: {values["text"]};
            font-family: "Malgun Gothic", "Segoe UI", sans-serif; font-size: 14px;
        }}
        QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{
            background: {values["window"]};
        }}
        QFrame#sidebar {{ background: {values["sidebar"]}; border: none; }}
        QLabel#brandTitle {{
            background: transparent; color: #FFFFFF; font-size: 22px; font-weight: 700;
        }}
        QLabel#brandSubtitle {{
            background: transparent; color: #D9EAF7; font-size: 11px;
        }}
        QPushButton#navButton {{
            background: transparent; border: 0; border-radius: 8px; color: #D8E6F0;
            min-height: 46px; padding: 0 14px; text-align: left; font-weight: 600;
        }}
        QPushButton#navButton:hover {{ background: {values["sidebar_hover"]}; color: #FFFFFF; }}
        QPushButton#navButton:checked {{ background: {values["sidebar_active"]}; color: #FFFFFF; }}
        QPushButton#navButton:disabled {{ color: #7890A4; background: transparent; }}
        QPushButton#navButton:focus {{
            border: 2px solid {values["primary"]}; color: #FFFFFF;
        }}
        QFrame#sessionCard {{
            background: {values["sidebar_hover"]}; border: 1px solid #4F7898;
            border-radius: 10px;
        }}
        QLabel#sessionCaption {{
            background: transparent; color: #C9E2F3; font-size: 11px; font-weight: 700;
        }}
        QLabel#sessionName {{ background: transparent; color: #FFFFFF; font-weight: 700; }}
        QFrame#topBar {{
            background: {values["surface"]};
            border-bottom: 1px solid {values["border"]};
        }}
        QLabel#pageTitle {{ font-size: 20px; font-weight: 700; }}
        QPushButton#helpButton, QPushButton#themeButton {{
            background: transparent; border: 1px solid {values["border"]}; border-radius: 6px;
            padding: 6px 10px;
        }}
        QPushButton#helpButton:hover, QPushButton#themeButton:hover {{
            background: {values["window"]};
        }}
        QPushButton:focus, QScrollArea:focus {{
            border: 2px solid {values["primary"]}; outline: none;
        }}
        QFrame#placeholderPage, QFrame#scanExamCard, QFrame#scanRosterCard,
        QFrame#scanSourceCard, QFrame#scanSensitivityCard, QFrame#answerKeyUploadCard,
        QFrame#answerKeyValidationCard, QFrame#gradingProgressPanel,
        QFrame#dashboardTableCard {{
            background: {values["surface"]}; border: 1px solid {values["border"]};
            border-radius: 12px;
        }}
        QFrame#rosterImportWidget, QFrame#scanSourceImportWidget {{
            background: {values["window"]}; border: 2px dashed {values["border"]};
            border-radius: 10px;
        }}
        QFrame#rosterImportWidget:hover, QFrame#scanSourceImportWidget:hover {{
            border-color: {values["primary"]};
        }}
        QLabel#placeholderHeading, QLabel#scanPageTitle, QLabel#gradingTitle,
        QLabel#dashboardTitle, QLabel#trashDialogTitle {{
            font-size: 22px; font-weight: 700;
        }}
        QLabel#placeholderBody, QLabel#scanPageSubtitle, QLabel#rosterStatus,
        QLabel#connectedSessionLabel {{
            color: {values["muted"]};
        }}
        QLabel#statusLabel, QLabel#sessionStatusLabel {{
            background: transparent; color: {values["muted"]};
        }}
        QStatusBar {{
            background: {values["surface"]}; color: {values["muted"]};
            border-top: 1px solid {values["border"]};
        }}
        QLabel[role="success"] {{ color: {values["success"]}; font-weight: 600; }}
        QLabel[role="error"] {{ color: {values["error"]}; font-weight: 600; }}
        QWidget:disabled {{ color: {values["disabled"]}; }}
        QWidget#dashboardPage, QWidget#dashboardContent, QScrollArea#dashboardScrollArea,
        QDialog#trashDialog {{
            background: {values["window"]}; color: {values["text"]};
        }}
        QLineEdit#dashboardSearch, QComboBox#dashboardYearFilter, QComboBox#dashboardTermFilter {{
            background: {values["surface"]}; border: 1px solid {values["border"]};
            border-radius: 6px; padding: 6px;
        }}
        QTableView#dashboardTable, QListWidget#trashList {{
            background: {values["surface"]}; alternate-background-color: {values["window"]};
            border: 1px solid {values["border"]}; gridline-color: {values["border"]};
        }}
        QHeaderView::section {{
            background: {values["window"]}; color: {values["text"]};
            border: 0; border-bottom: 1px solid {values["border"]}; padding: 7px;
            font-weight: 600;
        }}
        QPushButton#dashboardBackupButton, QPushButton#dashboardRestoreButton,
        QPushButton#dashboardCombinedButton, QPushButton#dashboardTrashButton,
        QPushButton#dashboardDetailButton, QPushButton#dashboardDeleteButton,
        QPushButton#trashRestoreButton, QPushButton#trashPermanentDeleteButton,
        QPushButton#trashEmptyButton {{
            background: {values["surface"]}; border: 1px solid {values["border"]};
            border-radius: 6px; padding: 7px 12px;
        }}
        QPushButton#dashboardBackupButton:hover, QPushButton#dashboardRestoreButton:hover,
        QPushButton#dashboardCombinedButton:hover, QPushButton#dashboardTrashButton:hover,
        QPushButton#dashboardDetailButton:hover, QPushButton#trashRestoreButton:hover {{
            border-color: {values["primary"]}; background: {values["window"]};
        }}
        QPushButton#dashboardDeleteButton, QPushButton#trashPermanentDeleteButton,
        QPushButton#trashEmptyButton {{ color: {values["error"]}; }}
    """


def apply_theme(application: QApplication, theme: Theme | str) -> Theme:
    """Apply a palette and QSS without retaining process-global theme state."""
    selected = Theme(theme)
    application.setPalette(palette_for(selected))
    application.setStyleSheet(stylesheet_for(selected))
    return selected


__all__ = ["Theme", "apply_theme", "palette_for", "stylesheet_for"]
