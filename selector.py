#!/usr/bin/env python3
"""Blast Radius selector: patch-aware, cheap and conservative on risky changes.

Uses only patch contents and test metadata. Never reads sample answers.
"""
import json
import re
from pathlib import Path

DATA = Path('/data')


def parse_changes(diff):
    """Return (filename, meaningful added/removed lines) per changed file."""
    changes = []
    path = None
    lines = []
    def flush():
        if path is not None:
            changes.append((path, lines[:]))
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            flush()
            path = line[6:].lower()
            lines = []
        elif path is not None and ((line.startswith('+') and not line.startswith('+++')) or
                                     (line.startswith('-') and not line.startswith('---'))):
            code = line[1:].strip()
            if code and not code.startswith(('#', '--', '/*', '* ', '*/')):
                lines.append(code)
    flush()
    return [(p, changed) for p, changed in changes if changed]


def choose(tests, diff):
    changes = parse_changes(diff)
    if not changes:
        return []
    chosen = set()
    # Repeated suffix/prefix checks against small (1620-entry) metadata index are inexpensive.
    def add(pred):
        for t in tests:
            if pred(t['id_lower'], t['file_lower']):
                chosen.add(t['number'])

    def optimizer_smoke():
        # Shared optimizer flows used by changes to parsers, dialects and typing.
        names = ('test_optimize', 'test_qualify_columns', 'test_qualify_tables',
                 'test_annotate_funcs', 'test_quote_identifiers', 'test_simplify',
                 'test_normalize_identifiers', 'test_tpcds', 'test_tpch')
        add(lambda i, f: f == 'tests/test_optimizer.py' and i.split('::')[-1] in names)

    def optimizer_related(words, with_executor=False):
        common = {'test_optimize', 'test_tpcds', 'test_tpch', 'test_qualify_columns'}
        add(lambda i, f: f == 'tests/test_optimizer.py' and
            (i.split('::')[-1] in common or any(w in i.split('::')[-1] for w in words)))
        if with_executor:
            names = ('test_execute_tpcds', 'test_execute_tpch', 'test_execute_subqueries')
            add(lambda i, f: f == 'tests/test_executor.py' and i.split('::')[-1] in names)

    for p, changed in changes:
        stem = Path(p).stem
        if p.startswith('tests/fixtures/optimizer/'):
            # A fixture's file name directly identifies its test method.
            name = stem.lower()
            aliases = {name, name.removesuffix('s'), name.replace('projections', 'projection')}
            add(lambda i, f: f == 'tests/test_optimizer.py' and
                any('test_' + alias in i for alias in aliases))
        elif p.startswith('tests/'):
            add(lambda i, f: f == p)
        elif p.startswith(('sqlglot/dialects/', 'sqlglot/generators/', 'sqlglot/parsers/')):
            add(lambda i, f: f == 'tests/dialects/test_' + stem + '.py')
            if stem == 'dialect':
                # Core dialect class affects dialect-specific syntax/transpilation.
                add(lambda i, f: f == 'tests/dialects/test_dialect.py')
            if p.startswith('sqlglot/parsers/') and stem == 'bigquery':
                optimizer_smoke()
                add(lambda i, f: f == 'tests/test_parser.py' and
                    i.split('::')[-1] not in ('test_max_nodes',))
                add(lambda i, f: f == 'tests/test_expressions.py')
            if stem == 'tsql':
                optimizer_smoke()
        elif p.startswith('sqlglot/optimizer/'):
            # Graph-optimizing passes are covered by the relevant optimizer,
            # integration and query-execution tests.
            if stem == 'eliminate_joins':
                optimizer_related(('join', 'projection', 'eliminate', 'scope'), True)
            elif stem in ('eliminate_subqueries', 'eliminate_ctes', 'merge_subqueries'):
                optimizer_related(('subquer', 'cte', 'merge'), True)
            else:
                optimizer_related((stem, 'join', 'cte', 'projection', 'qualify'), True)
        elif p == 'sqlglot/serde.py':
            # Serialization is used by optimizer/executor as well as typed ASTs.
            add(lambda i, f: f in ('tests/test_optimizer.py', 'tests/test_executor.py',
                                  'tests/test_serde.py', 'tests/test_expressions.py'))
            # Most dialect tests are short; coverage helps with cross-feature changes.
            add(lambda i, f: f.startswith('tests/dialects/'))
        elif p.startswith('sqlglot/typing/'):
            optimizer_smoke()
            add(lambda i, f: f == 'tests/test_optimizer.py' and
                any(k in i for k in ('annotate_', 'type_', 'qualify_', 'scope')))
            add(lambda i, f: f == 'tests/dialects/test_' + stem + '.py')
        elif p.startswith('sqlglot/executor/'):
            add(lambda i, f: f == 'tests/test_executor.py' and
                i.split('::')[-1] not in ('test_execute_tpcds',))
            optimizer_related(('execute',), False)
        elif p.startswith('sqlglot/expressions/'):
            add(lambda i, f: f == 'tests/test_expressions.py')
            add(lambda i, f: f.startswith('tests/dialects/') and
                any(k in i for k in ('test_ddl','test_create_table','test_constraint','test_trigger')))
            optimizer_smoke()
        elif p == 'sqlglot/transforms.py':
            add(lambda i, f: f == 'tests/test_transforms.py')
            optimizer_smoke()
        else:
            # An unknown core module is riskier: prioritize broad coverage,
            # except for unusually slow stress/performance-only tests.
            add(lambda i, f: f != 'tests/test_parser.py' or
                i.split('::')[-1] != 'test_max_nodes')
    return [t for t in tests if t['number'] in chosen]


def schedule(tests):
    if not tests:
        return []
    tests = sorted(tests, key=lambda t: -t['ms'])
    best_score = float('inf')
    best_workers = None
    for n in range(1, min(8, len(tests)) + 1):
        bins = [[] for _ in range(n)]
        loads = [0] * n
        for t in tests:
            idx = loads.index(min(loads))
            loads[idx] += t['ms']
            bins[idx].append(t['number'])
        score = max(loads) + 2000 * n
        if score < best_score:
            best_score = score
            best_workers = bins
    return best_workers


def main():
    tests = json.loads((DATA / 'tests.json').read_text())['tests']
    for t in tests:
        t['id_lower'] = t['id'].lower()
        t['file_lower'] = t['file'].lower()
        t['ms'] = int(t['seconds'] * 1000 + 0.5)
    for patch in sorted((DATA / 'commits').glob('*.patch')):
        workers = schedule(choose(tests, patch.read_text()))
        print(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))


if __name__ == '__main__':
    main()
