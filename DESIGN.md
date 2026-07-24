# OMR Grader Design System

## 1. Atmosphere & Identity

OMR Grader is a calm inspection desk: confident enough for batch grading, quiet enough that a teacher can spot uncertainty without hunting through the screen. The signature is a warm paper workspace framed by deep ink surfaces and a single mint signal color for progress, selection, and success.

## 2. Color

### Palette

| Role | Token | Value | Usage |
|------|-------|-------|-------|
| Window | `COLOR_WINDOW` | `#F4F7F5` | Main app background |
| Surface | `COLOR_SURFACE` | `#FFFFFF` | Cards and panels |
| Surface muted | `COLOR_SURFACE_MUTED` | `#EAF0ED` | Secondary information blocks |
| Ink | `COLOR_INK` | `#17332D` | Titles and primary text |
| Ink muted | `COLOR_INK_MUTED` | `#58706A` | Supporting text |
| Border | `COLOR_BORDER` | `#D7E2DE` | Card and input outlines |
| Accent | `COLOR_ACCENT` | `#1E8E78` | Primary actions and focus |
| Accent dark | `COLOR_ACCENT_DARK` | `#146653` | Hover and pressed state |
| Accent pale | `COLOR_ACCENT_PALE` | `#DDF3EC` | Selection and success background |
| Warning | `COLOR_WARNING` | `#B46A1B` | Review-needed state |
| Error | `COLOR_ERROR` | `#B64242` | Validation and failure |
| Dark panel | `COLOR_DARK_PANEL` | `#17332D` | Activity rail and progress |
| Dark panel deep | `COLOR_DARK_PANEL_DEEP` | `#102A25` | Activity log surface |
| Dark panel muted | `COLOR_DARK_MUTED` | `#A9C5BD` | Text on dark panel |

Rules: one accent family only; warning and error are semantic statuses, never decoration. Primary buttons use `COLOR_ACCENT_DARK` with light text to preserve contrast; `COLOR_ACCENT` is reserved for non-text emphasis and focus. All code colors must come from this table.

## 3. Typography

- Primary: `맑은 고딕` with `Segoe UI` fallback (available on supported Windows systems).
- Numeric/status labels: `Consolas` with `Cascadia Mono` fallback.
- Display: 20 px semibold, line-height 26 px.
- Section: 14 px semibold, line-height 20 px.
- Body: 10–11 px regular, line-height 16 px.
- Caption: 9 px regular, line-height 14 px.

Body text never falls below 9 px in the desktop-only app. Long paths wrap in the visible label so the selected source remains auditable.

## 4. Spacing & Layout

Base unit: 4 px. Use the following Tkinter equivalents: 4, 8, 12, 16, 20, 24, 32.

- Window minimum: 820 × 720 px.
- Header: 24 px horizontal / 20 px vertical padding.
- Main content: two-column shell; left task rail 220 px, right workspace expands.
- Card radius: 12 px where ttk supports it; otherwise use tonal separation and 1 px borders.
- Primary controls: 36 px high; compact controls: 30 px high.

## 5. Components

### App shell
- Structure: dark header + two-column body + activity footer.
- States: idle, ready, running, complete, error.
- Accessibility: every action is keyboard reachable; focus uses accent ring.

### File step card
- Structure: numbered marker, title, supporting path/count, action button.
- Variants: scan source, answer key, output folder.
- States: empty, selected, invalid, disabled while running.
- Spacing: 16 px internal, 12 px between cards.

### Primary action
- States: default, hover, pressed, disabled, running.
- Motion: 120 ms color/relief transition where native ttk permits; no decorative motion.
- Accessibility: text label remains visible; disabled state is conveyed by contrast and state text.

### Activity panel
- Structure: dark tonal panel with status dot, progress label, and scrollable log.
- States: idle, processing, success, failure.
- Accessibility: status is also written to the log text and remains selectable/copyable.

## 6. Motion & Interaction

Tkinter has no compositor-backed animation contract, so motion is intentionally limited to native button press/hover feedback. Long-running recognition runs off the UI thread; status changes are event-driven through the queue poller.

## 7. Depth & Surface

Strategy: tonal-shift + restrained borders. Use the dark header/activity panel for depth, white cards for work surfaces, and pale mint for selected/success states. Avoid heavy shadows that make dense desktop text blurry.

## 8. Accessibility Constraints & Accepted Debt

- Target: practical WCAG 2.1 AA contrast for text and controls.
- Keyboard: tab traversal covers all buttons and the log; Enter activates the focused button.
- Feedback: errors are shown in a dialog and written to the activity panel.
- Accepted debt: native Tk file dialogs cannot be restyled; they remain platform-native for reliability.
- Accepted debt: no automated screen-reader snapshot exists for Tkinter; labels and state text are kept explicit and testable.
