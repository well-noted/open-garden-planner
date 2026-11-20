# Test triage after the plant-recipes merge (2026-11-20)

Question: which failures in the full-suite run after merging
`feature/plant-recipes-georeference` (master 41b7b678) come from the merge?

Method: the same 153 selected tests (`-n 0`, `PYTHONPATH=<tree>/src` so each tree
imports its own code) run on master (41b7b678) and on a clean checkout of c37feb0c
(the parent before the merge). Both runs: 16 failed, 127 passed, 2 skipped,
1 collection error. The failing sets are identical.

**Result: none of these failures is caused by the merge. No code was changed.**

| Test | Baseline | Cause (what the output shows) |
| --- | --- | --- |
| `test_ci_coverage_shards.py` (collection) | fails | `ModuleNotFoundError: No module named 'coverage'`; the package is missing from `venv`. |
| `test_ci_quality_workflows.py` (2 tests) | fails | `uv` 0.13.7 is installed, the repo requires `==0.12.23`; `uv lock --offline` exits 2. |
| `test_packaged_install_guard::test_an_unpackaged_process_reports_no_identity` | fails | `has_package_identity()` is True: the venv's interpreter is the Microsoft Store Python 3.12 (WindowsApps), which has MSIX package identity. |
| `test_quick3d_selftest.py` supervisor tests (9) | fails | Child processes exit 2 with "the process named is not the one that started this supervisor". Likely the same Store-Python launcher indirection (PID of the launcher is not the interpreter's). Not confirmed on a non-Store interpreter. |
| `test_hours_of_sun_from_the_button_to_the_export_and_back` | fails | `'night'` vs `'active'`; consistent with a dependence on the wall clock at run time. Not re-run at another hour. |
| `test_the_shadow_drape_is_the_2d_overlays_own_polygons_cut_at_the_plan` | fails | Area 338281 vs expected 3000000 +-3%. Same on baseline; render/machine dependent, cause not investigated. |
| `test_view3d_plan_revision[a_time_sweep_with_the_sun_simulation_on]` | fails | 6 repaints reported, 10 or more expected; timing/load dependent. |
| `test_canvas_native_pixels::test_same_size_content_changes_repaint_relocated_foreground` | fails | 69 stale pixels in `edge_label`; same on baseline, rendering dependent. |

## Follow-ups (owner's call, not done here)

- Install `coverage` and `uv==0.12.23` in `venv`, and use a python.org (non-Store)
  interpreter, then re-run; three groups above should go green.
- The four rendering/clock-dependent tests need a separate investigation on a
  non-Store interpreter, with `/debug-verbose`, before being called flaky or broken.
