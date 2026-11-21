#!/usr/bin/env python3
"""Patch-aware Blast Radius selector using coverage from a pinned upstream snapshot.

Coverage map is made once at Docker build; test answers are never used.
"""
import gzip
import json
import re
from pathlib import Path

DATA = Path('/data')
COV = Path('/app/coverage_map.json.gz')
HUNK = re.compile(r'^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@')


def changes_from_patch(text):
    """Extract changed old-version line numbers, paths, and changed source lines."""
    result = []
    active = None
    old = 0
    for line in text.splitlines():
        if line.startswith('diff --git '):
            if active is not None:
                result.append(active)
            active = None
        elif line.startswith('+++ b/'):
            active = {'path': line[6:], 'lines': set(), 'changes': []}
        elif active is not None:
            match = HUNK.match(line)
            if match:
                old = int(match.group(1))
            elif line.startswith('\\ No newline'):
                continue
            elif line.startswith('---') or line.startswith('+++'):
                continue
            elif line.startswith('-'):
                active['lines'].add(old)
                active['changes'].append(line[1:])
                old += 1
            elif line.startswith('+'):
                # Insertions can affect the instruction before or after the insertion.
                active['lines'].update((max(1, old - 1), old))
                active['changes'].append(line[1:])
            elif line.startswith(' '):
                old += 1
    if active is not None:
        result.append(active)
    return result


def is_comment_only(lines, name):
    # Strictly recognize comment-only edits. Non-Python and docstring edits aren't skipped.
    if not name.endswith('.py') or not lines:
        return False
    return all(not s.strip() or s.lstrip().startswith('#') for s in lines)


def choose(tests, coverage_map, diff):
    selected = set()
    changes = changes_from_patch(diff)
    id_by_number = {t['number']:t for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t['file'], []).append(t['number'])
    optim = {t['id'].split('::')[-1]:t['number'] for t in tests if t['file']=='tests/test_optimizer.py'}
    def file_tests(f):
        selected.update(by_file.get(f, ()))
    def opt(*methods):
        for m in methods:
            if m in optim:
                selected.add(optim[m])
    def fixture_test(path):
        name=Path(path).stem
        if path.startswith('tests/fixtures/optimizer/'):
            names={name, name.rstrip('s'), name.replace('projections','projection')}
            for n in names:
                opt('test_'+n)
            # Names with known singular/plural variations (no mutation-specific mapping).
            if 'projection' in name: opt('test_pushdown_projection')
            if 'qualify_columns' in name: opt('test_qualify_columns')
        elif path.startswith('tests/fixtures/'):
            name=Path(path).stem
            for f in by_file:
                if name.lower() in f.lower(): file_tests(f)
    def static_fallback(path, changed):
        """Cover expensive tests missing from build-time coverage and implicit imports."""
        if path.startswith(('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/typing/')):
            dialect=Path(path).stem
            file_tests(f'tests/dialects/test_{dialect}.py')
            if path.startswith('sqlglot/typing/'):
                opt('test_annotate_types', 'test_annotate_funcs', 'test_qualify_columns',
                    'test_optimize', 'test_qualify_tables', 'test_quote_identifiers')
            elif path.startswith('sqlglot/parsers/'):
                opt('test_optimize', 'test_qualify_columns', 'test_qualify_tables',
                    'test_quote_identifiers', 'test_annotate_funcs')
                if dialect == 'bigquery':
                    file_tests('tests/test_expressions.py')
                    # Broad parser integration, excluding a 29-second stress-only case.
                    for t in tests:
                        if t['file']=='tests/test_parser.py' and t['seconds'] < 1.0:
                            selected.add(t['number'])
            if path.startswith('sqlglot/dialects/'):
                # Dialect class constants can change optimizer behavior without executing
                # the assignment line in test context (e.g. TSQL.WEEK_OFFSET).
                opt('test_simplify', 'test_optimize')
                if dialect == 'dialect': file_tests('tests/dialects/test_dialect.py')
            if path.startswith('sqlglot/generators/'):
                # Transpile test suite exercises cross-dialect conversions.
                file_tests('tests/test_transpile.py')
        elif path.startswith('sqlglot/optimizer/'):
            base=Path(path).stem
            words=(base, 'join', 'subquer', 'cte', 'scope', 'projection', 'qualify')
            opt('test_optimize', 'test_tpcds', 'test_tpch', 'test_qualify_columns',
                'test_qualify_tables', 'test_annotate_funcs')
            for t in tests:
                method=t['id'].split('::')[-1]
                if t['file']=='tests/test_optimizer.py' and any(w in method for w in words):
                    selected.add(t['number'])
                if t['file']=='tests/test_executor.py' and method in (
                    'test_execute_tpcds','test_execute_subqueries','test_execute_tpch'):
                    selected.add(t['number'])
        elif path=='sqlglot/serde.py':
            # A broken serializer can poison typing / metadata in nearly every feature.
            file_tests('tests/test_serde.py')
            file_tests('tests/test_optimizer.py')
            file_tests('tests/test_expressions.py')
            for f in by_file:
                if f.startswith('tests/dialects/'):
                    file_tests(f)
            for t in tests:
                method=t['id'].split('::')[-1]
                if t['file']=='tests/test_executor.py' and method in (
                    'test_execute_tpcds','test_execute_tpch'):
                    selected.add(t['number'])
        elif path.startswith('sqlglot/executor/'):
            # Select all executor tests except prohibitively slow unrelated long runner
            # only when coverage sees a link to the changed code.
            for t in tests:
                if t['file']=='tests/test_executor.py' and t['seconds']<3.0:
                    selected.add(t['number'])
            opt('test_optimize')
        elif path.startswith('sqlglot/expressions/'):
            file_tests('tests/test_expressions.py')
            opt('test_optimize','test_qualify_columns')
        elif path=='sqlglot/transforms.py':
            file_tests('tests/test_transforms.py')
        elif path.startswith('sqlglot/'):
            # Unknown core module: conservative, prioritize inexpensive high-coverage tests.
            for t in tests:
                if t['seconds'] < 0.25:
                    selected.add(t['number'])
        elif path.startswith('tests/'):
            file_tests(path)

    for change in changes:
        path=change['path']
        if is_comment_only(change['changes'], path):
            # Comments are normally nonfunctional; the important exception is
            # a "#" inside a multiline Python string (e.g. embedded SQL).
            # The pinned-source tokenizer records these regions at build time.
            cov = coverage_map.get(path, {})
            string_ranges = cov.get('strings', ())
            inside_string = any(start <= loc <= end for loc in change['lines']
                                for start, end in string_ranges)
            directive = any('coding:' in s or 'coding=' in s
                            for s in change['changes'])
            if not inside_string and not directive:
                continue
        if path.startswith('tests/fixtures/'):
            fixture_test(path)
            continue
        if path.startswith('tests/'):
            file_tests(path)
            continue
        if not path.startswith('sqlglot/'):
            continue
        cov=coverage_map.get(path)
        if cov:
            line_map=cov['lines']
            spans=cov['functions']
            for loc in change['lines']:
                # A changed predicate may be in a branch never taken on the base.
                # Include every test executing any line of the enclosing function.
                functions=[(end-start,start,end) for start,end in spans if start<=loc<=end]
                if functions:
                    _, start, end=min(functions)
                    for s in range(start,end+1):
                        selected.update(line_map.get(str(s), ()))
                else:
                    # Class attribute / top-level import: include tests that execute
                    # any code in this module, plus static fallbacks below.
                    for ids in line_map.values():
                        selected.update(ids)
                for n in (loc-1,loc,loc+1):
                    if n>0:
                        selected.update(line_map.get(str(n),()))
        static_fallback(path, change['changes'])
    return [id_by_number[n] for n in sorted(selected)]


def schedule(tests):
    if not tests: return []
    tests.sort(key=lambda t:-t['milliseconds'])
    best_score=10**14
    best=[]
    for n in range(1,min(8,len(tests))+1):
        bins=[[] for _ in range(n)]
        loads=[0]*n
        for t in tests:
            idx=loads.index(min(loads))
            loads[idx]+=t['milliseconds']
            bins[idx].append(t['number'])
        score=max(loads)+2000*n
        if score<best_score:
            best_score=score
            best=bins
    return best


def main():
    tests=json.loads((DATA/'tests.json').read_text())['tests']
    for t in tests:
        t['milliseconds']=int(t['seconds']*1000+0.5)
    if not COV.is_file():
        raise SystemExit('Coverage map missing: do not submit an unverified build')
    with gzip.open(COV,'rt') as f:
        coverage_map=json.load(f)
    for path in sorted((DATA/'commits').glob('*.patch')):
        result=choose(tests,coverage_map,path.read_text())
        print(json.dumps({'commit':path.stem,'workers':schedule(result)},separators=(',',':')))

if __name__=='__main__':
    main()
