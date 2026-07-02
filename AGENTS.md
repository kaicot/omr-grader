# AGENTS.md

Instructions for AI coding agents (Claude, Cursor, Copilot, etc.) working in this repo.

## What this is

A single-script Windows tool (`omr_grader.py`) that recognizes marked bubbles on scanned 5-choice OMR answer sheets (fixed 100-question / 8-digit student ID form, see `OCR100.pdf`), grades them against an answer key, and writes an Excel result. Ships as a standalone `.exe` via PyInstaller so end users don't need Python installed.

## Environment

- Windows only. The deliverable is a Windows `.exe`; PyInstaller cannot cross-build a Windows exe from Linux/macOS.
- Python 3.13. Required packages: `numpy`, `opencv-python-headless`, `openpyxl`, `Pillow`, `PyMuPDF`, `pyinstaller`. `tkinter` is stdlib, no install needed.
- Don't add new dependencies without asking — the project deliberately stays on this fixed set.

## Build command

Run exactly this from the repo root (both `omr_grader.py` and `splash.png` must be present):

```bash
python -m PyInstaller --onefile --windowed --name omr_grader --splash splash.png omr_grader.py
```

Output: `dist/omr_grader.exe`. This is the only supported build command — don't add `--onedir`, an installer, or other packaging without being asked.

If `splash.png` is missing, it's a simple generated image (title + "불러오는 중입니다..." + author credit, rendered with Pillow using `C:/Windows/Fonts/malgun.ttf` for Korean text) — regenerate it rather than removing `--splash` from the build.

## Running without building

```bash
python omr_grader.py
```
Opens the GUI directly (no exe needed for local testing).

## Architecture notes

- Everything lives in one file, `omr_grader.py`, by design (bubble-geometry constants, file I/O, image alignment, mark recognition, grading, Excel output, debug overlay, tkinter GUI). Don't split it into modules unless asked.
- The bubble-position constants at the top of `omr_grader.py` (`BLOCK_X_STARTS`, `ROW_Y_PITCH`, `TABLE_BORDER_PT`, etc.) were measured directly from `OCR100.pdf`'s vector coordinates (via PyMuPDF), not guessed. If `OCR100.pdf` ever changes, these need to be re-derived from the new file, not hand-tweaked.
- `align_sheet()` handles scans that come in rotated 90/180/270 degrees (common with flatbed scanners) and detects upside-down flips by comparing ink density at the known header-text region vs. its mirror — this is load-bearing, not incidental complexity.

## Tests

The test suite exists locally (`tests/test_omr_grader.py`, ~26 assert-based tests, run via `python tests/test_omr_grader.py`) but is **not tracked in this repository** by the maintainer's choice. Don't expect to find it after cloning — if you need to verify a change, `python -c "import omr_grader"` (checks for syntax/import errors) and a manual run of `python omr_grader.py` are the available options here.

## Answer key format

xlsx or csv, two columns: 문항번호 (question number, 1..N) and 정답 (correct answer, 1-5). The number of rows determines how many questions are graded — there's no separate question-count setting.
