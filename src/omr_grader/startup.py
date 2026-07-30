"""Minimal Qt startup surface loaded before the application bootstrap graph."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen


def configure_application_branding(application: QApplication) -> None:
    icon_path = Path(__file__).resolve().parent / "resources" / "app_icon.svg"
    icon = QIcon(str(icon_path))
    if icon.isNull():
        raise RuntimeError("OMR Grader application icon could not be loaded")
    application.setWindowIcon(icon)
    if sys.platform == "win32":
        try:
            from ctypes import windll

            windll.shell32.SetCurrentProcessExplicitAppUserModelID("OMRGrader.Desktop.2")
        except (AttributeError, OSError):
            pass


def create_splash() -> QSplashScreen:
    pixmap = QPixmap(640, 340)
    pixmap.fill(QColor("#102A43"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#61C3E8"))
    painter.setFont(QFont("Malgun Gothic", 30, QFont.Weight.Bold))
    painter.drawText(48, 82, "OMR Grader")
    painter.setPen(QColor("#FFFFFF"))
    painter.setFont(QFont("Malgun Gothic", 16, QFont.Weight.DemiBold))
    painter.drawText(48, 132, "정확한 답안 판독과 채점")
    painter.setPen(QColor("#D9EAF7"))
    painter.setFont(QFont("Malgun Gothic", 11))
    painter.drawText(48, 235, "프로그램개발: 조승현(kaic21@gmail.com)")
    painter.end()

    splash = QSplashScreen(pixmap, Qt.WindowType.WindowStaysOnTopHint)
    splash.setObjectName("startupSplash")
    splash.setAccessibleName("OMR Grader 시작 화면")
    splash.setAccessibleDescription(
        "OMR Grader 로딩 중. 프로그램개발: 조승현(kaic21@gmail.com)"
    )
    splash.showMessage(
        "프로그램을 준비하고 있습니다...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        QColor("#FFFFFF"),
    )
    return splash


def create_startup() -> tuple[QApplication, QSplashScreen]:
    application = QApplication.instance()
    app = application if isinstance(application, QApplication) else QApplication(sys.argv)
    QApplication.setApplicationName("OMR Grader")
    QApplication.setOrganizationName("OMR Grader")
    QApplication.setApplicationDisplayName("OMR Grader")
    configure_application_branding(app)
    splash = create_splash()
    splash.show()
    app.processEvents()
    return app, splash
