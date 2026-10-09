"""Minimal Qt startup surface loaded before the application bootstrap graph."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget


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


class StartupSplash(QLabel):
    """A frameless picture window shown while the application loads.

    Qt's ``QSplashScreen`` holds ``show()`` for about a second on Windows while it waits for
    the window to be exposed, which delayed every start; a plain window appears at once.
    """

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center() - self.rect().center())

    def finish(self, window: QWidget) -> None:
        """Close once the event loop runs, after the shown main window has painted."""
        del window
        QTimer.singleShot(0, self.close)


def create_splash() -> StartupSplash:
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
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(
        QRect(0, 290, 640, 40),
        int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        "프로그램을 준비하고 있습니다...",
    )
    painter.end()

    splash = StartupSplash(pixmap)
    splash.setObjectName("startupSplash")
    splash.setAccessibleName("OMR Grader 시작 화면")
    splash.setAccessibleDescription(
        "OMR Grader 로딩 중. 프로그램개발: 조승현(kaic21@gmail.com)"
    )
    return splash


def create_startup() -> tuple[QApplication, StartupSplash]:
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
