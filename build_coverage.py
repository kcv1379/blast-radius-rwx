#!/usr/bin/env python3
"""Generate test-to-source coverage once, during Docker BUILD (unscored).

Only the pinned public sqlglot commit and supplied test metadata are used.
No sample patches, answer labels, or mutation-specific lookups are included.
"""
import ast
import gzip
import json
import os
import tokenize
from pathlib import Path

import coverage
import pytest

ROOT = Path('/app/sqlglot')
PACKAGE = ROOT / 'sqlglot'
TESTS = json.loads(Path('/app/build_tests.json').read_text())['tests']
# Long tests are excluded from coverage measurement to keep build < 10 min.
# At runtime, their selection is handled by static fallbacks.
MAX_BASELINE_SECONDS = 0.8
ALLOWED = {t['id']: t['number'] for t in TESTS if t['seconds'] <= MAX_BASELINE_SECONDS}

class TestContexts:
    def __init__(self, cov):
        self.cov = cov
        self.selected = 0
    def pytest_collection_modifyitems(self, items):
        keep = [it for it in items if it.nodeid in ALLOWED]
        deselect = [it for it in items if it.nodeid not in ALLOWED]
        items[:] = keep
        # Collected, non-competition tests are removed to save Docker build time.
        self.selected = len(keep)
    def pytest_runtest_setup(self, item):
        self.cov.switch_context(item.nodeid)
    def pytest_runtest_teardown(self, item):
        self.cov.switch_context('_outside')


def spans_for_file(filename):
    try:
        root = ast.parse(Path(filename).read_text('utf-8'))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return [], []
    spans = []
    for node in ast.walk(root):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and hasattr(node, 'end_lineno'):
            spans.append([node.lineno, node.end_lineno])
    multiline_strings = []
    with open(filename, 'rb') as f:
        try:
            for tok in tokenize.tokenize(f.readline):
                if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                    multiline_strings.append([tok.start[0], tok.end[0]])
        except tokenize.TokenError:
            pass
    return spans, multiline_strings


def main():
    os.chdir(ROOT)
    cov = coverage.Coverage(source=[str(PACKAGE)], data_file='/tmp/build_coverage.sqlite', branch=False, config_file=False)
    plugin = TestContexts(cov)
    cov.start()
    try:
        result = pytest.main([
            '-q', '--tb=no', '--disable-warnings', '--continue-on-collection-errors',
            '-p', 'no:cacheprovider', 'tests'
        ], plugins=[plugin])
    finally:
        cov.stop()
    data = cov.get_data()
    lookup = {t['id']: t['number'] for t in TESTS}
    mapping = {}
    total_edges = 0
    for src in data.measured_files():
        src = Path(src)
        try:
            rel = src.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if not rel.startswith('sqlglot/') or not rel.endswith('.py'):
            continue
        lines = {}
        for line, contexts in data.contexts_by_lineno(str(src)).items():
            ids = sorted({lookup[c] for c in contexts if c in lookup})
            if ids:
                lines[str(line)] = ids
                total_edges += len(ids)
        if lines:
            functions, strings = spans_for_file(src)
            mapping[rel] = {'lines': lines, 'functions': functions, 'strings': strings}
    if plugin.selected < 1200 or len(mapping) < 80 or total_edges < 10000:
        raise RuntimeError(
            f'Coverage too sparse: selected={plugin.selected}, files={len(mapping)}, '
            f'edges={total_edges}, pytest_exit={result}. Check build logs.'
        )
    out = Path('/app/coverage_map.json.gz')
    with gzip.open(out, 'wt', encoding='utf-8', compresslevel=4) as f:
        json.dump(mapping, f, separators=(',', ':'))
    print(f'Coverage generated: {plugin.selected} selected tests, {len(mapping)} source files, '
          f'{total_edges} line-test edges, {out.stat().st_size} compressed bytes, pytest exit {result}', flush=True)

if __name__ == '__main__':
    main()
