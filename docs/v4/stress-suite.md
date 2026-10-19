# Synthetic recognition stress suite

`tools/stress_suite.py` measures how the recognition engine behaves on synthetic sheets that
look like the real exam card: a printed digit inside every bubble, an 8-digit student ID,
100 (or 60) questions with 5 choices. It changes nothing in the engine. Its job is to give
later engine changes (4.3.0) a fixed yardstick, so "fewer silent errors" is a number.

Pages come from `tests/helpers/synthetic_omr.py`, get the stress marks painted on, then the
generator's scanner blur and noise. Each page goes through the product path
(`recognize_page` with a profile learned from a clean page of the same form) and every
question is compared with the known truth. Everything is deterministic for a given
`--seeds`, and uses synthetic IDs of the form `2026xxxx`.

## Outcomes

- `correct`: a definite answer equal to the truth, or blank where the truth is blank, or
  MULTIPLE with the right two choices where the truth is a double mark.
- `review`: the engine asks for a person (UNCERTAIN). Every question of a held or
  unreadable page counts here.
- `silent_wrong`: a definite answer, MULTIPLE or BLANK that differs from the truth. Nobody
  is asked, so it is graded as read. This is the number to drive to zero.
- `bg_sw`: silent errors on the ordinary background questions of a stress page (should be 0).
  Only every third question is stressed; the rest are ordinary marks, so the per-column
  reference of the printed digits stays realistic.
- ID: `id_ok` (accepted), `id_inv` (sent to review), `id_bad` (accepted but wrong; for a
  truncated ID any accepted ID is wrong). `failed` = pages the engine could not align,
  `held` = pages flagged for manual review.

## Scenarios

| Scenario | How it is synthesized | Truth |
| --- | --- | --- |
| `clean` | One dark disk per question, every 11th blank, jittered position and darkness | the mark |
| `faint_mark` | Single marks at gray 40/80/120/160/200 (full disk) and dark dots of radius 10/8/6.5/5 px | the mark |
| `residue_lone` | Blank question with one light uneven gray ellipse (radius about 14 px) in one cell, gray 210 to 130 | blank |
| `residue_beside_mark` | One dark mark plus the same kind of light residue in another cell of the question | the dark mark |
| `correction_tape` | Dark mark, then a white (252) strip with a faint edge shadow over it that leaves a sliver of the mark of 0 to 12 px (or half of it) visible, or lets the old mark show through as gray 225/205/190; the new mark is in another cell | the new mark |
| `double_equal` | Two equal marks (dark disks, gray-100 disks, or dots of radius 10) | MULTIPLE |
| `cut_column` | Whole page shifted right 130/160/220 px, shifted down 260/300 px, or enlarged 110%, so bubbles fall off the image; every question with a bubble within 17 px of the border is a case, and the mark is always the truth even where it fell outside the image | the mark (never blank) |
| `truncated_id` | 8-digit ID with the last, the last two, a middle or the first column left blank | ID must not be accepted |
| `sensitivity` | `faint_mark`, `residue_lone` and `residue_beside_mark` rerun at sensitivity 3, 5, 7 and 10 | as above |

The level rows show where each behavior starts, which is more useful than the totals when
comparing two engines.

## Running it

```text
python tools/stress_suite.py                       # all scenarios, 3 seeds, sensitivity 5, plus the sweep
python tools/stress_suite.py --scenarios clean cut_column --brief
python tools/stress_suite.py --seeds 1 --questions 60
python tools/stress_suite.py --json out.json       # full summary with levels
python tools/stress_suite.py --record history.jsonl  # one numbers-only line per run
```

Run from a checkout with `PYTHONPATH` set to `src` and the repository root (the tool puts
both on `sys.path` itself). The full run reads about 75 pages and takes around 80 s.
`--record` appends version (`omr_grader.__version__`), git commit (`+dirty` when the tree
has changes), date and the counts per scenario and sensitivity; no images, IDs or levels.
The suite always exits 0: the weak scenarios are measurements, not failures.
`tests/integration/test_stress_suite.py` runs one seed of every scenario (about 25 s) and
asserts only what holds today: `clean` has no silent error and no review, and
`double_equal` stays MULTIPLE. The other scenarios are informational there.

## Baseline: 4.2.4 (commit f4ccd85 engine, suite at 5e829c6), sensitivity 5, 3 seeds

| Scenario | cases | correct | review | silent_wrong | ID |
| --- | ---: | ---: | ---: | ---: | --- |
| `clean` | 300 | 300 | 0 | 0 | 3 of 3 accepted |
| `faint_mark` | 99 | 64 | 31 | 4 | 3 of 3 accepted |
| `residue_lone` | 99 | 14 | 33 | 52 | 3 of 3 accepted |
| `residue_beside_mark` | 99 | 10 | 36 | 53 | 3 of 3 accepted |
| `correction_tape` | 99 | 54 | 31 | 14 | 3 of 3 accepted |
| `double_equal` | 99 | 99 | 0 | 0 | 3 of 3 accepted |
| `cut_column` | 270 | 204 | 0 | 66 | 15 accepted, 3 sent to review |
| `truncated_id` | n/a | n/a | n/a | n/a | 6 of 12 accepted, all 6 wrong |

Where the silent errors start (cases per level, 3 seeds):

- `faint_mark`: gray up to 160 and dots of radius 10 px are read; gray 200 and the 6.5 px
  dot go to review; the 5 px dot is blank in 4 of 9 cases (silent), the 8 px dot is in
  review in 4 of 11.
- `residue_lone` / `residue_beside_mark`: gray 210 is mostly review or correct, gray 190 is
  all review, gray 170 is about two thirds silent (a phantom answer, or MULTIPLE beside a
  real mark), gray 150 and darker is 100% silent.
- `correction_tape`: a sliver of up to 6 px or the tape covering the ring and mark is read
  right; 9 px and a gray 205 or darker ghost go to review; 12 px (5 of 9) and half covered
  (9 of 9) are silent MULTIPLE. In this model tape rarely causes silent errors unless more
  than a thin edge shows, but ghosts and slivers add review work.
- `cut_column`: a page shifted 160 px or more reads the marks that fell outside the image
  as BLANK (17 of 60 cases, 15 of 15 for the shift down 300 px). Shifted 130 px or down 260 px
  the bubbles are still partly visible and read right. The enlarged page is held because
  the ID is cut off; its answers are still not flagged.
- `truncated_id`: with the last one or two columns blank the shorter ID is accepted as a
  normal ID (6 of 6 wrong). A blank middle or first column is correctly sent to review.

Sensitivity sweep (cases 99, silent_wrong in parentheses; correct / review):

| Scenario | 3 | 5 | 7 | 10 |
| --- | --- | --- | --- | --- |
| `faint_mark` | 58 / 37 (4) | 64 / 31 (4) | 68 / 27 (4) | 78 / 17 (4) |
| `residue_lone` | 14 / 39 (46) | 14 / 33 (52) | 14 / 23 (62) | 14 / 11 (74) |
| `residue_beside_mark` | 10 / 44 (45) | 10 / 36 (53) | 10 / 28 (61) | 10 / 17 (72) |

Raising the sensitivity turns review cases into correct answers for real faint marks, but
turns the same number of review cases into silent errors for residue: the blank ceiling does
not move with the setting.

## Limits of the synthetic data

The residue, tape and ghost shapes are plausible recipes, not scans of real erasures: the
real rates will differ, and the levels are meant for comparing engines, not for predicting
a school's error rate. The suite does not cover skew, lens distortion, JPEG artifacts or
forms without printed digits; the batch and perturbation checks in `tools/v4_baseline.py`
remain the place for those.

## 4.3.0 result

Same suite, sensitivity 5, after the four 4.3.0 rules (ratio test for double marks, faint
marks relative to the page's usual strength, coverage of the scanned page, all eight ID
columns). Each rule was added and measured on its own; the record lines are in the private
`results-history.jsonl`.

| Scenario | cases | correct | review | silent_wrong |
| --- | ---: | ---: | ---: | ---: |
| `clean` | 300 | 300 | 0 | 0 |
| `faint_mark` | 99 | 33 | 62 | 4 |
| `residue_lone` | 99 | 14 | 79 | 6 |
| `residue_beside_mark` | 99 | 10 | 89 | 0 |
| `correction_tape` | 99 | 54 | 45 | 0 |
| `double_equal` | 99 | 66 | 33 | 0 |
| `cut_column` | 270 | 0 | 270 | 0 |
| `truncated_id` | 12 pages | | 12 ID to review | 0 wrong IDs accepted |

The remaining silent errors are residue at 0.50-0.54 of the page's usual mark (as dark as
the faintest real answers, 0.57) and 5 px dots read blank. Faint real marks and pairs of
small dots now go to review. The real-data gates (G1 cross-profile, G2 perturbations, E2E)
are unchanged: 0 wrong answers and 0 review cells.
