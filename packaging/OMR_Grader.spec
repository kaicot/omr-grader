# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"

datas = collect_data_files("omr_grader") + collect_data_files("tzdata")

hiddenimports = ["fitz", "omr_grader.bootstrap"]
hiddenimports.extend(collect_submodules("tzdata"))

analysis = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(SRC_ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "pytestqt", "hypothesis", "mypy", "ruff", "pip", "setuptools"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

icon_application = QGuiApplication.instance() or QGuiApplication([])
icon_renderer = QSvgRenderer(
    str(PROJECT_ROOT / "src" / "omr_grader" / "resources" / "app_icon.svg")
)
if not icon_renderer.isValid():
    raise RuntimeError("Could not render the application icon SVG.")
icon_image = QImage(256, 256, QImage.Format.Format_ARGB32)
icon_image.fill(Qt.GlobalColor.transparent)
icon_painter = QPainter(icon_image)
icon_renderer.render(icon_painter)
icon_painter.end()
icon_path = Path(workpath) / "omr-grader.ico"
if not icon_image.save(str(icon_path), "ICO"):
    raise RuntimeError("Could not write the application icon.")

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    name="OMR Grader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    exclude_binaries=True,
    icon=str(icon_path),
)
collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="OMR Grader",
)
