# AGENTS.md

Instructions for AI coding agents (Claude, Cursor, Copilot, etc.) working in this repo.

## What this is

A single-script Windows tool (`omr_grader.py`) that recognizes marked bubbles on scanned 5-choice OMR answer sheets (fixed 100-question / 8-digit student ID form, see `OCR100.pdf`), grades them against an answer key, and writes an Excel result. It also preserves local grading history, original inputs, review edits, item analysis, and diagnostic logs. Ships as a standalone `.exe` via PyInstaller so end users don't need Python installed.

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
- `RunStore` is the v1.0 persistence boundary. It stores run metadata, per-student responses, review state, diagnostic events, and JSONL logs in `%LOCALAPPDATA%\OMRGrader` (or `.omr_grader_data` when `LOCALAPPDATA` is unavailable). Do not add a server, account system, or new storage dependency without being asked.
- `run_pipeline()` must continue to write the existing `채점결과.xlsx` and `debug/` output while also creating a persistent run record. Keep original scan snapshots connected to the run so re-analysis and review remain auditable.
- Re-analysis stores new recognition separately from the original response. Manual corrections take precedence when calculating the effective response. Re-grading and re-analysis must present a preview before applying changes in the GUI.
- `delete_run()` is intentionally recoverable: it moves local run artifacts and logs to the store's `trash/` directory before removing database records. Do not replace it with irreversible deletion.
- `App` is the professor-facing summary workspace; `HistoryWindow` and `RunDetailWindow` provide the evidence-first review flow. Preserve background grading and do not run image recognition on the Tkinter UI thread.
- Processing runs that survive a crash are marked `interrupted` on the next launch. Errors belong in both user-visible status feedback and the persistent diagnostic log.

## Data safety and compatibility

- Treat `%LOCALAPPDATA%\OMRGrader` as user data, not build output. Never delete, reset, or bulk-rewrite it during development or tests.
- Preserve SQLite schema compatibility for existing local stores. Additive schema changes and guarded migrations are acceptable; destructive migrations require explicit user approval.
- Keep logs useful for debugging without copying answer-sheet images or unnecessary student-response content into diagnostic messages.
- Keep input validation at file boundaries and retain the current dependency set unless the user explicitly approves a change.

## Tests

The local, intentionally untracked regression suite is `tests/test_omr_grader.py`. On this Windows environment, run it with:

```bash
py -3.13 -m py_compile omr_grader.py
py -3.13 -m pytest -q tests/test_omr_grader.py
```

The suite covers recognition behavior and the local history store, including interrupted runs, re-grading previews, manual corrections, re-analysis previews, and recoverable deletion. Do not assume it is present after cloning. At minimum, run `python -c "import omr_grader"` and manually open `python omr_grader.py` when the suite is unavailable.

## Answer key format

xlsx or csv, two columns: 문항번호 (question number, 1..N) and 정답 (correct answer, 1-5). The number of rows determines how many questions are graded — there's no separate question-count setting.

## Versioning and releases

- Follow the simplified Semantic Versioning policy in `README.md` and record every released change in `CHANGELOG.md`.
- **MAJOR** versions change the core grading/review workflow, persistent data model, or result format. **MINOR** versions add meaningful recognition, review, report, or GUI workflow capability. **PATCH** versions are compatible fixes and documentation or wording corrections.
- A release commit must update `README.md` and `CHANGELOG.md` together when the user-visible version or behavior changes. Create an annotated `vX.Y.Z` tag only when the user asks to publish a release.
