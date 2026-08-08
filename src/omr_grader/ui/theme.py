"""Korean OMR Grader palettes and application stylesheet."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleOption, QWidget


class Theme(str, Enum):
    """Supported application color schemes."""

    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    """Semantic design tokens shared by palette, QSS, and painted icons."""

    bg_primary: str
    bg_surface: str
    bg_sidebar: str
    bg_sidebar_hover: str
    bg_sidebar_active: str
    text_primary: str
    text_secondary: str
    text_disabled: str
    text_on_accent: str
    text_sidebar: str
    text_sidebar_muted: str
    border_color: str
    icon_primary: str
    icon_brand: str
    link_color: str
    accent_primary: str
    accent_hover: str
    success: str
    error: str
    checkbox_bg: str
    checkbox_border: str
    checkbox_checked_bg: str
    checkbox_checked_border: str
    check_icon_color: str

    def qss_values(self) -> dict[str, str]:
        """Map semantic tokens to compact QSS substitution names."""
        return {
            "window": self.bg_primary,
            "surface": self.bg_surface,
            "sidebar": self.bg_sidebar,
            "sidebar_hover": self.bg_sidebar_hover,
            "sidebar_active": self.bg_sidebar_active,
            "text": self.text_primary,
            "muted": self.text_secondary,
            "disabled": self.text_disabled,
            "on_accent": self.text_on_accent,
            "sidebar_text": self.text_sidebar,
            "sidebar_muted": self.text_sidebar_muted,
            "border": self.border_color,
            "icon": self.icon_primary,
            "brand_icon": self.icon_brand,
            "primary": self.accent_primary,
            "primary_hover": self.accent_hover,
            "link": self.link_color,
            "success": self.success,
            "error": self.error,
        }


_THEME_TOKENS: dict[Theme, ThemeTokens] = {
    Theme.LIGHT: ThemeTokens(
        bg_primary="#F4F7FB",
        bg_surface="#FFFFFF",
        bg_sidebar="#102A43",
        bg_sidebar_hover="#1B3E5C",
        bg_sidebar_active="#1E5A8A",
        text_primary="#172B4D",
        text_secondary="#667085",
        text_disabled="#667085",
        text_on_accent="#FFFFFF",
        text_sidebar="#FFFFFF",
        text_sidebar_muted="#D9EAF7",
        border_color="#CBD5E1",
        icon_primary="#334155",
        icon_brand="#61C3E8",
        link_color="#1D4ED8",
        accent_primary="#2563EB",
        accent_hover="#1D4ED8",
        success="#15803D",
        error="#B42318",
        checkbox_bg="#FFFFFF",
        checkbox_border="#334155",
        checkbox_checked_bg="#BFDBFE",
        checkbox_checked_border="#2563EB",
        check_icon_color="#172554",
    ),
    Theme.DARK: ThemeTokens(
        bg_primary="#17212B",
        bg_surface="#22303C",
        bg_sidebar="#0B1F33",
        bg_sidebar_hover="#173854",
        bg_sidebar_active="#215A84",
        text_primary="#F1F5F9",
        text_secondary="#CBD5E1",
        text_disabled="#94A3B8",
        text_on_accent="#0F172A",
        text_sidebar="#F8FAFC",
        text_sidebar_muted="#D9EAF7",
        border_color="#64748B",
        icon_primary="#E2E8F0",
        icon_brand="#7DD3FC",
        link_color="#93C5FD",
        accent_primary="#7DB5FF",
        accent_hover="#93C5FD",
        success="#6EE7A0",
        error="#FF9B8F",
        checkbox_bg="#334155",
        checkbox_border="#CBD5E1",
        checkbox_checked_bg="#2563EB",
        checkbox_checked_border="#E2E8F0",
        check_icon_color="#F8FAFC",
    ),
}


def tokens_for(theme: Theme | str) -> ThemeTokens:
    """Return immutable semantic design tokens for one theme."""
    return _THEME_TOKENS[Theme(theme)]


class _TokenProxyStyle(QProxyStyle):
    """Paint checkbox indicators from active theme tokens."""

    def __init__(self, tokens: ThemeTokens) -> None:
        super().__init__()
        self._tokens = tokens

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
    ) -> int:
        if metric in {
            QStyle.PixelMetric.PM_IndicatorWidth,
            QStyle.PixelMetric.PM_IndicatorHeight,
        }:
            return 18
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element not in {
            QStyle.PrimitiveElement.PE_IndicatorCheckBox,
            QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
        }:
            super().drawPrimitive(element, option, painter, widget)
            return
        state = QStyle.StateFlag(getattr(option, "state", 0))
        checked = bool(state & QStyle.StateFlag.State_On)
        partial = bool(state & QStyle.StateFlag.State_NoChange)
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        active = checked or partial
        background = (
            self._tokens.checkbox_checked_bg if active else self._tokens.checkbox_bg
        )
        border = (
            self._tokens.checkbox_checked_border if active else self._tokens.checkbox_border
        )
        if not enabled:
            border = self._tokens.text_disabled
        rect = getattr(option, "rect").adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(border), 2))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(rect, 3, 3)
        if checked:
            pen = QPen(QColor(self._tokens.check_icon_color), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(rect.left() + rect.width() * 0.22, rect.center().y()),
                        QPointF(
                            rect.left() + rect.width() * 0.43,
                            rect.bottom() - rect.height() * 0.22,
                        ),
                        QPointF(
                            rect.right() - rect.width() * 0.16,
                            rect.top() + rect.height() * 0.22,
                        ),
                    ]
                )
            )
        elif partial:
            pen = QPen(QColor(self._tokens.check_icon_color), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(rect.left() + rect.width() * 0.22, rect.center().y()),
                QPointF(rect.right() - rect.width() * 0.22, rect.center().y()),
            )
        painter.restore()


def palette_for(theme: Theme | str) -> QPalette:
    """Return a Qt palette with sufficient contrast for the selected theme."""
    tokens = tokens_for(theme)
    values = tokens.qss_values()
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(values["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(values["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(values["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(values["window"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(values["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(values["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(values["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(values["primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens.text_on_accent))
    palette.setColor(QPalette.ColorRole.Link, QColor(values["link"]))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(values["link"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens.text_secondary))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens.bg_surface))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens.text_primary))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(values["disabled"])
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(values["disabled"])
    )
    return palette


def stylesheet_for(theme: Theme | str) -> str:
    """Return the QSS used by the main shell and its content pages."""
    values = tokens_for(theme).qss_values()
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
            selection-color: {values["on_accent"]};
        }}
        QTextBrowser#helpBrowser {{
            border: 1px solid {values["border"]}; border-radius: 8px;
        }}
        QFrame#sidebar {{
            background: {values["sidebar"]}; border: none;
        }}
        QFrame#sidebar QLabel {{ background: transparent; }}
        QLabel#brandMark {{
            background: transparent; color: {values["brand_icon"]};
            font-size: 34px; font-weight: 700;
        }}
        QLabel#brandTitle {{
            background: transparent; color: {values["sidebar_text"]};
            font-size: 22px; font-weight: 700;
        }}
        QLabel#brandSubtitle {{
            background: transparent; color: {values["sidebar_muted"]}; font-size: 11px;
        }}
        QPushButton#navButton {{
            background: transparent; border: 0; border-radius: 8px;
            color: {values["sidebar_muted"]};
            min-height: 46px; padding: 0 14px; text-align: left; font-weight: 600;
        }}
        QPushButton#navButton:hover {{
            background: {values["sidebar_hover"]}; color: {values["sidebar_text"]};
        }}
        QPushButton#navButton:checked {{
            background: {values["sidebar_active"]}; color: {values["sidebar_text"]};
        }}
        QPushButton#navButton:disabled {{
            color: {values["disabled"]}; background: transparent;
        }}
        QPushButton#navButton:focus {{
            border: 2px solid {values["primary"]}; color: {values["sidebar_text"]};
        }}
        QFrame#sessionCard {{
            background: {values["sidebar_hover"]}; border: 1px solid {values["border"]};
            border-radius: 10px;
        }}
        QLabel#sessionCaption {{
            background: transparent; color: {values["sidebar_muted"]};
            font-size: 11px; font-weight: 700;
        }}
        QLabel#sessionName {{
            background: transparent; color: {values["sidebar_text"]}; font-weight: 700;
        }}
        QLabel#sessionProgress {{
            background: transparent; color: {values["sidebar_muted"]}; font-size: 12px;
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
        QFrame#dashboardTableCard, QFrame#settingsPortablePathCard,
        QFrame#settingsProfileCard, QFrame#settingsRecognitionCard {{
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
        QPushButton#answerKeyUploadButton, QPushButton#cancelGradingButton,
        QPushButton#gradingResetButton, QPushButton#detailBackButton,
        QPushButton#detailSaveButton {{
            background: {values["surface"]}; border: 1px solid {values["border"]};
            border-radius: 7px; min-height: 34px; padding: 5px 13px;
            font-weight: 600;
        }}
        QPushButton#freshResponseButton:hover, QPushButton#scanResetButton:hover,
        QPushButton#scanCancelButton:hover,
        QPushButton#profileImportButton:hover, QPushButton#sampleRosterButton:hover,
        QPushButton#sourceFolderButton:hover, QPushButton#sourcePdfButton:hover,
        QPushButton#sampleAnswerKeyButton:hover, QPushButton#answerKeyUploadButton:hover,
        QPushButton#cancelGradingButton:hover, QPushButton#gradingResetButton:hover,
        QPushButton#detailBackButton:hover, QPushButton#detailSaveButton:hover {{
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
        QPushButton#answerKeyUploadButton:pressed, QPushButton#cancelGradingButton:pressed,
        QPushButton#gradingResetButton:pressed, QPushButton#detailBackButton:pressed,
        QPushButton#detailSaveButton:pressed {{
            padding-top: 7px; padding-bottom: 3px;
            border: 2px solid {values["text"]};
        }}
        QPushButton#freshResponseButton:disabled, QPushButton#scanResetButton:disabled,
        QPushButton#scanCancelButton:disabled,
        QPushButton#scanRunButton:disabled, QPushButton#primaryActionButton:disabled,
        QPushButton#profileImportButton:disabled,
        QPushButton#sampleRosterButton:disabled, QPushButton#sourceFolderButton:disabled,
        QPushButton#sourcePdfButton:disabled, QPushButton#sampleAnswerKeyButton:disabled,
        QPushButton#answerKeyUploadButton:disabled, QPushButton#cancelGradingButton:disabled,
        QPushButton#gradingResetButton:disabled, QPushButton#detailBackButton:disabled,
        QPushButton#detailSaveButton:disabled {{
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
        QPushButton#dashboardTrashButton,
        QPushButton#dashboardDetailButton, QPushButton#dashboardDeleteButton,
        QPushButton#trashRestoreButton, QPushButton#trashPermanentDeleteButton,
        QPushButton#trashEmptyButton {{
            background: {values["surface"]}; border: 1px solid {values["border"]};
            border-radius: 6px; padding: 7px 12px;
        }}
        QPushButton#dashboardBackupButton:hover, QPushButton#dashboardRestoreButton:hover,
        QPushButton#dashboardTrashButton:hover,
        QPushButton#dashboardDetailButton:hover, QPushButton#dashboardDeleteButton:hover,
        QPushButton#trashRestoreButton:hover {{
            border-color: {values["primary"]}; background: {values["window"]};
        }}
        QWidget#dashboardActionCell {{ background: transparent; }}
        QPushButton#dashboardDetailButton, QPushButton#dashboardDeleteButton {{
            font-size: 12px; min-height: 24px; padding: 3px 6px;
        }}
        QPushButton#dashboardDeleteButton, QPushButton#trashPermanentDeleteButton,
        QPushButton#trashEmptyButton {{ color: {values["error"]}; }}
    """


def apply_theme(application: QApplication, theme: Theme | str) -> Theme:
    """Apply a palette and QSS without retaining process-global theme state."""
    selected = Theme(theme)
    application.setStyle(_TokenProxyStyle(tokens_for(selected)))
    application.setPalette(palette_for(selected))
    application.setStyleSheet(stylesheet_for(selected))
    return selected


__all__ = [
    "Theme",
    "ThemeTokens",
    "apply_theme",
    "palette_for",
    "stylesheet_for",
    "tokens_for",
]
