# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

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

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="OMR Grader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
