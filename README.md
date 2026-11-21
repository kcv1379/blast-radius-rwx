# RWX Blast Radius — Coverage-based CI test selection

This entry for the Builders Buildathon predicts tests impacted by a patch against pinned SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## How it works

1. **At image build (unscored)**, downloads the **fixed upstream SQLGlot source**, runs baseline tests under `coverage.py` while recording each test's execution context, and saves a compressed map of source lines to test numbers. Tests slower than 0.8 seconds baseline are omitted from instrumentation to keep the build fast; conservative rules select important slow tests at runtime.
2. **At runtime (scored)**, parses each supplied unified diff and uses coverage of affected functions to select tests, with supplemental rules for imports, SQL dialects, SQL fixtures and shared optimizer code. It never reads sample answers.
3. Chooses one to eight workers to minimize the exact official simulated latency cost (max duration plus 2 seconds per nonempty worker).

The Dockerfile is at the repository root. No runtime network or filesystem writes are required.

## Validation

**GitHub Actions automatically runs the official sample scorer on every push to `main`.** Open your repository's Actions tab and check that `Validate Blast Radius sample` passes and reports zero misses **before submitting your new commit**. The workflow downloads the official starter kit only in the CI runner, so sample answers are never committed. You can also run `make sample` locally after copying the original starter kit's `Makefile`, `score.py`, and `sample/` folder into this directory.

An offline test of the **fallback rules with an empty coverage map** on all 30 known sample patches produced **172.265 simulated seconds with 0 misses**. That is **not** a verified result from the actual Docker image; the generated coverage map can change the selection and score. Do not submit without verifying the actual Docker build and sample run.

## Source constraints

Only the pinned upstream SQLGlot commit, PyPI packages, and provided test metadata (`build_tests.json`) are used. No answer files, sample patch-to-failure lookups, or private data are included.

MIT licensed.
