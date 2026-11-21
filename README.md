# RWX Blast Radius — coverage + import-state impact analysis

For every patch (a unified diff against pinned SQLGlot `eb11aec4`) the selector decides which tests
could fail and how to split them over at most 8 workers (cost = busiest worker + 2 s per worker, +300 s
per missed failing test, plus CPU seconds).

## Build time (unscored)
* `fetch_source.py` downloads the pinned SQLGlot tree; `requirements.lock` reproduces the labeling environment.
* `build_coverage.py` runs the whole suite once under coverage.py (C tracer, one dynamic context per test)
  through `cov_plugin.py`. The optimizer and executor tests do their real work in spawned
  `ProcessPoolExecutor` workers, so the plugin tags every submitted task with the submitting test
  (switching context while the worker unpickles the task) and measures inside the workers too.
* Line coverage is mapped to AST statement units (a changed line inside a multi-line statement maps to every
  test that ran any line of it). The map also records which tests instantiate each dialect / parser /
  generator / tokenizer / expression class, which test reads each fixture file, which tests execute code
  mentioning each import-time name, and a baseline fingerprint of SQLGlot's import-time state
  (`fpr.py`), calibrated against the exact run-time mechanism and across ten hash seeds.

## Run time
* `selector.py` (stdlib only) maps each patch to the tests that executed the changed statements.
  Comment-only edits select nothing unless they sit inside a multi-line string.
* Edits that run while modules and classes are created (class attributes, module constants, functions called
  at import) are resolved by importing the patched package in a throw-away forked child and diffing its
  import-time state against the baseline (`xrun.py`), which yields exactly which classes/names changed and
  any import failures; those map to the tests using them.
* Slow tests (>= 3 s: the TPC executor tests, `test_max_nodes`, ...) are a gate: when they are candidates the
  cheap candidates are executed for real against the patched sources (patched modules are served by an import
  hook, fixtures by an `open()` wrapper; nothing is written to disk). If none fails the slow tests are skipped.
* Worker count is chosen to minimise the exact simulated cost; a wall-clock deadline falls back to plain
  selection for any commit that could not be probed.

## Validation
`tools/` holds the lab used for validation: a mutation generator in the sample's style, a labeler that applies
a patch and runs the real suite in the organisers' base image (it reproduces the official sample answers
exactly), and analysis scripts. Only the pinned upstream source, PyPI packages and the starter's test metadata
are used; no answers or per-patch lookups.
