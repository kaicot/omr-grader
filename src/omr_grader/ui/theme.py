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
    "primary_hover": "#1D4ED8",
    "link": "#1D4ED8",
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
    "primary_hover": "#5B9CF5",
    "link": "#93C5FD",
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
    palette.setColor(QPalette.ColorRole.Link, QColor(values["link"]))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(values["link"]))
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
        QLabel {{ background: transparent; }}
        QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{
            background: {values["window"]};
        }}
        QLineEdit, QComboBox, QTextBrowser, QAbstractItemView {{
            background: {values["surface"]}; color: {values["text"]};
            selection-background-color: {values["primary"]};
            selection-color: #FFFFFF;
        }}
        QTextBrowser#helpBrowser {{
            border: 1px solid {values["border"]}; border-radius: 8px;
        }}
        QFrame#sidebar {{
            background: {values["sidebar"]}; border: none;
        }}
        QFrame#sidebar QLabel {{ background: transparent; }}
        QLabel#brandMark {{
            background: transparent; color: #61C3E8; font-size: 34px; font-weight: 700;
        }}
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
        QLabel#sessionProgress {{
            background: transparent; color: #D9EAF7; font-size: 12px;
        }}
        QFrame#topBar {{
            background: {values["surface"]};
            border-bottom: 1px solid {values["border"]};
        }}
        QFrame#topBar QLabel {{ background: transparent; }}
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
        QFrame#importDropWidget, QFrame#rosterImportWidget, QFrame#scanSourceImportWidget,
        QFrame#profileImportWidget {{
            background: {values["window"]}; border: 2px dashed {values["border"]};
            border-radius: 10px;
        }}
        QFrame#importDropWidget:hover, QFrame#rosterImportWidget:hover,
        QFrame#scanSourceImportWidget:hover,
        QFrame#profileImportWidget:hover,
        QFrame#importDropWidget[dragActive="true"],
        QFrame#rosterImportWidget[dragActive="true"],
        QFrame#scanSourceImportWidget[dragActive="true"],
        QFrame#profileImportWidget[dragActive="true"] {{
            border-color: {values["primary"]};
            background: {values["surface"]};
        }}
        QPushButton#freshResponseButton, QPushButton#scanResetButton, QPushButton#scanCancelButton,
        QPushButton#scanRunButton, QPushButton#primaryActionButton,
        QPushButton#profileImportButton,
        QPushButton#sampleRosterButton, QPushButton#sourceFolderButton,
        QPushButton#sourcePdfButton, QPushButton#sampleAnswerKeyButton,
        QPushButton#answerKeyUploadButton, QPushButton#cancelGradingButton {{
            background: {values["surface"]}; border: 1px solid {values["border"]};
            border-radius: 7px; min-height: 34px; padding: 5px 13px;
            font-weight: 600;
        }}
        QPushButton#freshResponseButton:hover, QPushButton#scanResetButton:hover,
        QPushButton#scanCancelButton:hover,
        QPushButton#profileImportButton:hover, QPushButton#sampleRosterButton:hover,
        QPushButton#sourceFolderButton:hover, QPushButton#sourcePdfButton:hover,
        QPushButton#sampleAnswerKeyButton:hover, QPushButton#answerKeyUploadButton:hover,
        QPushButton#cancelGradingButton:hover {{
            border-color: {values["primary"]}; background: {values["window"]};
        }}
        QPushButton#freshResponseButton, QPushButton#scanRunButton,
        QPushButton#primaryActionButton {{
            background: {values["primary"]}; color: #FFFFFF;
            border-color: {values["primary"]};
        }}
        QPushButton#freshResponseButton:hover:enabled,
        QPushButton#scanRunButton:hover:enabled,
        QPushButton#primaryActionButton:hover:enabled {{
            background: {values["primary_hover"]}; color: #FFFFFF;
            border-color: {values["primary_hover"]};
        }}
        QPushButton#scanCancelButton {{ color: {values["error"]}; }}
        QPushButton#freshResponseButton:pressed, QPushButton#scanResetButton:pressed,
        QPushButton#scanCancelButton:pressed,
        QPushButton#scanRunButton:pressed, QPushButton#primaryActionButton:pressed,
        QPushButton#profileImportButton:pressed,
        QPushButton#sampleRosterButton:pressed, QPushButton#sourceFolderButton:pressed,
        QPushButton#sourcePdfButton:pressed, QPushButton#sampleAnswerKeyButton:pressed,
        QPushButton#answerKeyUploadButton:pressed, QPushButton#cancelGradingButton:pressed {{
            padding-top: 7px; padding-bottom: 3px;
            border: 2px solid {values["text"]};
        }}
        QPushButton#freshResponseButton:disabled, QPushButton#scanResetButton:disabled,
        QPushButton#scanCancelButton:disabled,
        QPushButton#scanRunButton:disabled, QPushButton#primaryActionButton:disabled,
        QPushButton#profileImportButton:disabled,
        QPushButton#sampleRosterButton:disabled, QPushButton#sourceFolderButton:disabled,
        QPushButton#sourcePdfButton:disabled, QPushButton#sampleAnswerKeyButton:disabled,
        QPushButton#answerKeyUploadButton:disabled, QPushButton#cancelGradingButton:disabled {{
            background: {values["window"]}; color: {values["disabled"]};
            border-color: {values["border"]};
        }}
        QLabel#placeholderHeading, QLabel#scanPageTitle, QLabel#gradingTitle,
        QLabel#dashboardTitle, QLabel#trashDialogTitle {{
            font-size: 22px; font-weight: 700;
        }}
        QLabel#placeholderBody, QLabel#scanPageSubtitle, QLabel#rosterStatus,
        QLabel#connectedSessionLabel {{
            color: {values["muted"]};
        }}
        QLabel#scanFormLabel, QLabel#scanSectionLabel {{
            color: {values["text"]}; font-weight: 600; background: transparent;
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
        QLineEdit#dashboardSearch, QComboBox#dashboardYearFilter {{
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
        QPushButton#dashboardDetailButton:hover, QPushButton#dashboardDeleteButton:hover,
        QPushButton#trashRestoreButton:hover {{
            border-color: {values["primary"]}; background: {values["window"]};
        }}
        QWidget#dashboardActionCell {{ background: transparent; }}
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
