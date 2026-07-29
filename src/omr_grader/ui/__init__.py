"""PySide6 presentation layer for OMR Grader."""

from .main_window import MainWindow
from .theme import Theme, apply_theme, palette_for, stylesheet_for

__all__ = [
    "MainWindow",
    "Theme",
    "apply_theme",
    "palette_for",
    "stylesheet_for",
]
