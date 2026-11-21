#!/usr/bin/env python3
"""Build the impact map once, during the Docker BUILD (unscored).

Runs the pinned sqlglot test-suite under coverage.py (one dynamic context per test), then
converts line coverage into statement-level "units" using the AST, so a changed line inside a
multi-line statement maps to every test that executed any line of that statement.

Environment: BR_SRC (sqlglot checkout, default /app/sqlglot), BR_MAP_DIR (output, default
/app/map), BR_COV_DIR (scratch), BR_JOBS (xdist workers), BR_SKIP_RUN=1 (reuse BR_COV_DIR).
Only the pinned public source tree is used; no patches, labels, or answers are involved.
"""
import ast
import gzip
import json
import os
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(os.environ.get('BR_SRC', '/app/sqlglot')).resolve()
PKG = ROOT / 'sqlglot'
COV_DIR = Path(os.environ.get('BR_COV_DIR', '/tmp/brcov'))
OUT = Path(os.environ.get('BR_MAP_DIR', '/app/map'))
JOBS = int(os.environ.get('BR_JOBS', '4'))
HERE = Path(__file__).resolve().parent

K_STMT, K_HDR, K_DEF, K_CLASS, K_LAM = 0, 1, 2, 3, 4
TRY_TYPES = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, 'TryStar') else ())


def run_tests():
    env = dict(os.environ, BR_PACKAGE_DIR=str(PKG), BR_COV_DIR=str(COV_DIR), COVERAGE_CORE='ctrace',
               SKIP_INTEGRATION='0', PYTHONPATH=f'{ROOT}{os.pathsep}{HERE}', PYTHONDONTWRITEBYTECODE='1')
    cmd = [sys.executable, '-m', 'pytest', '-n', str(JOBS), '-p', 'cov_plugin', '-p', 'no:cacheprovider',
           '--timeout=900', '-q', '--tb=no', '--disable-warnings', 'tests']
    r = subprocess.run(cmd, cwd=ROOT, env=env)
    print(f'build_coverage: pytest exit {r.returncode}', flush=True)


def eager_modules():
    """Package files loaded by a plain `import sqlglot` (their breakage would fail every test)."""
    code = ('import sys, sqlglot\n'
            'for n, m in sorted(sys.modules.items()):\n'
            '    if n.startswith("sqlglot") and getattr(m, "__file__", None):\n'
            '        print(m.__file__)\n')
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, capture_output=True, text=True).stdout
    res = []
    for line in out.splitlines():
        try:
            res.append(Path(line).resolve().relative_to(ROOT).as_posix())
        except ValueError:
            pass
    return res


class Sets:
    """Interns test-set bitmasks; the blob is fixed width so lookups need no parsing."""

    def __init__(self):
        self.index = {0: 0}
        self.items = [0]

    def add(self, mask):
        i = self.index.get(mask)
        if i is None:
            i = len(self.items)
            self.index[mask] = i
            self.items.append(mask)
        return i


def header_end(node):
    """Last line of a compound statement's header (everything before its first child statement)."""
    firsts = []
    for field in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
        kids = getattr(node, field, None)
        if kids:
            firsts.append(kids[0].lineno)
    end = (min(firsts) - 1) if firsts else node.end_lineno
    return max(node.lineno, min(end, node.end_lineno))


def analyze(tree, raw, sets):
    """Return (units, classes). unit = [start, end, kind, imp, owner, setidx]."""
    units = []
    classes = []

    def span_mask(a, b):
        m = 0
        for ln in range(a, b + 1):
            m |= raw.get(ln, 0)
        return m

    def add(a, b, kind, imp, owner, mask=None, full_end=None):
        if mask is None:
            mask = span_mask(a, b)
        units.append([a, b, kind, 1 if imp else 0, owner, sets.add(mask), full_end or b])

    def lambdas_in(node, owner):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Lambda):
                add(sub.lineno, sub.end_lineno, K_LAM, False, owner)

    def visit(stmts, in_func, cls):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([s.lineno] + [d.lineno for d in s.decorator_list])
                first = s.body[0].lineno
                end = max(s.lineno, first - 1) if first > s.lineno else s.lineno
                add(start, end, K_DEF, not in_func, cls, span_mask(s.lineno, s.end_lineno), s.end_lineno)
                for d in s.decorator_list + s.args.defaults + [x for x in s.args.kw_defaults if x]:
                    lambdas_in(d, cls)
                visit(s.body, True, cls)
            elif isinstance(s, ast.ClassDef):
                start = min([s.lineno] + [d.lineno for d in s.decorator_list])
                q = f'{cls}.{s.name}' if cls else s.name
                classes.append([s.lineno, s.end_lineno, q])
                add(start, header_end(s), K_CLASS, not in_func, q, span_mask(s.lineno, s.end_lineno), s.end_lineno)
                visit(s.body, in_func, q)
            elif isinstance(s, TRY_TYPES):
                visit(s.body, in_func, cls)
                for h in s.handlers:
                    add(h.lineno, header_end(h), K_HDR, not in_func, cls)
                    if h.type is not None:
                        lambdas_in(h.type, cls)
                    visit(h.body, in_func, cls)
                visit(s.orelse, in_func, cls)
                visit(s.finalbody, in_func, cls)
            elif isinstance(s, ast.Match):
                add(s.lineno, header_end(s), K_HDR, not in_func, cls)
                for case in s.cases:
                    add(case.pattern.lineno, header_end(case), K_HDR, not in_func, cls)
                    visit(case.body, in_func, cls)
            elif isinstance(s, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                add(s.lineno, header_end(s), K_HDR, not in_func, cls)
                for field in ('test', 'iter'):
                    sub = getattr(s, field, None)
                    if sub is not None:
                        lambdas_in(sub, cls)
                for item in getattr(s, 'items', []):
                    lambdas_in(item, cls)
                visit(s.body, in_func, cls)
                visit(s.orelse if hasattr(s, 'orelse') else [], in_func, cls)
            else:
                add(s.lineno, s.end_lineno, K_STMT, not in_func, cls)
                lambdas_in(s, cls)

    visit(tree.body, False, '')
    return units, classes


def multiline_strings(path):
    out = []
    with open(path, 'rb') as f:
        try:
            for tok in tokenize.tokenize(f.readline):
                if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                    out.append([tok.start[0], tok.end[0]])
        except tokenize.TokenError:
            pass
    return out


def collect_names(tree):
    """(targets, mentions): names assigned at module/class level, and (name, line) mentions of any
    identifier (attribute, bare name, keyword, or a string constant that looks like one)."""
    targets = set()
    mentions = []

    def scope(stmts, top):
        for s in stmts:
            if top and isinstance(s, (ast.Assign, ast.AnnAssign)):
                tgts = s.targets if isinstance(s, ast.Assign) else [s.target]
                for t in tgts:
                    if isinstance(t, ast.Name):
                        targets.add(t.id)
            if isinstance(s, ast.ClassDef):
                scope(s.body, top)
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope(s.body, False)
            else:
                for field in ('body', 'orelse', 'finalbody'):
                    scope(getattr(s, field, None) or [], top)
                for h in getattr(s, 'handlers', None) or []:
                    scope(h.body, top)

    scope(tree.body, True)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            mentions.append((node.attr, node.lineno))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            mentions.append((node.id, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
            mentions.append((node.value, node.lineno))
        elif isinstance(node, ast.keyword) and node.arg:
            mentions.append((node.arg, getattr(node.value, 'lineno', 0)))
    return targets, mentions


def outside_refs(ids, bit):
    """Repo files outside sqlglot/ and tests/ (sqlglotc/setup.py, pyproject.toml, ...) and the tests
    whose source mentions them by name: token -> mask of those tests."""
    tokens = set()
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(('sqlglot/', 'tests/', '.git/')) or '/.git/' in rel:
            continue
        tokens.add(rel)
        tokens.add(p.name)
        tokens.add(rel.split('/')[0])
    texts = {}
    for p in (ROOT / 'tests').rglob('*.py'):
        texts[p.relative_to(ROOT).as_posix()] = p.read_text('utf-8', errors='replace')
    out = {}
    for tok in tokens:
        if len(tok) < 4:
            continue
        m = 0
        for f, text in texts.items():
            if tok in text:
                prefix = f + '::'
                m |= sum(bit[c] for c in ids if c.startswith(prefix))
        if m:
            out[tok] = m
    return out


def multiline_strings(path):  # noqa: F811  (supersedes the simpler version: handles f-strings on 3.12+)
    out = []
    fstack = []
    with open(path, 'rb') as f:
        try:
            for tok in tokenize.tokenize(f.readline):
                if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                    out.append([tok.start[0], tok.end[0]])
                elif getattr(tokenize, 'FSTRING_START', None) is not None and tok.type == tokenize.FSTRING_START:
                    fstack.append(tok.start[0])
                elif getattr(tokenize, 'FSTRING_END', None) is not None and tok.type == tokenize.FSTRING_END:
                    start = fstack.pop() if fstack else tok.start[0]
                    if start != tok.end[0]:
                        out.append([start, tok.end[0]])
        except tokenize.TokenError:
            pass
    return out


def load_raw(merged, bit, src_file):
    raw, imp, modmask = {}, set(), 0
    for line, ctxs in merged.contexts_by_lineno(src_file).items():
        m = 0
        for c in ctxs:
            if c == '_import':
                imp.add(line)
            b = bit.get(c)
            if b:
                m |= b
        if m:
            raw[line] = m
            modmask |= m
    return raw, imp, modmask


def build_map():
    from coverage import CoverageData
    merged = CoverageData(basename=str(COV_DIR / 'merged'))
    merged.erase()
    for f in sorted(COV_DIR.glob('cov.*')):
        d = CoverageData(basename=str(f))
        d.read()
        merged.update(d)
    ids = sorted(c for c in merged.measured_contexts() if c.startswith('tests/'))
    bit = {c: 1 << i for i, c in enumerate(ids)}
    width = (len(ids) + 7) // 8
    sets = Sets()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'files').mkdir(exist_ok=True)

    files = []
    pending = []
    all_targets = set()
    measured = {Path(f).resolve(): f for f in merged.measured_files()}
    for p in sorted(PKG.rglob('*.py')):
        rel = p.resolve().relative_to(ROOT).as_posix()
        src_file = measured.get(p.resolve())
        raw, imp, modmask = load_raw(merged, bit, src_file) if src_file else ({}, set(), 0)
        try:
            tree = ast.parse(p.read_text('utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        units, classes = analyze(tree, raw, sets)
        targets, mentions = collect_names(tree)
        all_targets |= targets
        rec = {'u': units, 'c': classes, 's': multiline_strings(p), 'm': sets.add(modmask),
               'i': sorted(imp)}
        with gzip.open(OUT / 'files' / (rel.replace('/', '__') + '.json.gz'), 'wt') as f:
            json.dump(rec, f, separators=(',', ':'))
        files.append(rel)
        pending.append((units, mentions, imp))

    # which tests execute code that mentions each import-time-defined name (a flag, mapping, ...)?
    name_mask, name_imp = {}, set()
    for units, mentions, imp in pending:
        by_line = {}
        for u in units:
            m = sets.items[u[5]]
            for ln in range(u[0], u[1] + 1):
                by_line[ln] = by_line.get(ln, 0) | m
        for name, ln in mentions:
            if name not in all_targets:
                continue
            if ln in imp:
                name_imp.add(name)  # also read while modules/classes are being created
            name_mask[name] = name_mask.get(name, 0) | by_line.get(ln, 0)

    # class usage and fixture reads, from the per-test facts recorded while the tests ran
    users, fixtures = {}, {}
    for f in sorted(COV_DIR.glob('facts.*.json')):
        facts = json.loads(f.read_text())
        mro = facts['mro']
        for tid, types in facts['types'].items():
            if tid not in bit:
                continue
            for t in types:
                for anc in mro.get(t, [t]):
                    users[anc] = users.get(anc, 0) | bit[tid]
        for key, paths in facts['fixtures'].items():
            if key.startswith('class:') or key.startswith('module:'):
                prefix = key.split(':', 1)[1] + '::'
                m = sum(bit[c] for c in ids if c.startswith(prefix))
            else:
                m = bit.get(key, 0)
            for path in paths:
                fixtures[path] = fixtures.get(path, 0) | m
    gl = {
        'ids': ids,
        'width': width,
        'classes': {k: sets.add(v) for k, v in users.items()},
        'fixtures': {k: sets.add(v) for k, v in fixtures.items()},
        'eager': eager_modules(),
        'files': sorted(files),
        'pickle_files': sorted(r for r in files if r == 'sqlglot/serde.py' or any(k in (ROOT / r).read_text('utf-8', errors='replace') for k in ('__reduce__', '__getstate__', '__setstate__', 'copyreg'))),
        'outside': {k: sets.add(v) for k, v in outside_refs(ids, bit).items()},
        'names': {n: [sets.add(name_mask.get(n, 0)), 1 if n in name_imp else 0] for n in all_targets},
    }
    with gzip.open(OUT / 'global.json.gz', 'wt') as f:
        json.dump(gl, f, separators=(',', ':'))
    blob = b''.join(m.to_bytes(width, 'little') for m in sets.items)
    with gzip.open(OUT / 'sets.bin.gz', 'wb', compresslevel=6) as f:
        f.write(blob)
    total = sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file())
    print(f'build_coverage: {len(ids)} tests, {len(files)} source files, {len(sets.items)} distinct sets, '
          f'{len(users)} classes, {len(fixtures)} fixtures, map {total / 1e6:.1f} MB', flush=True)
    if len(ids) < 1500 or len(files) < 100:
        raise SystemExit('coverage map too sparse - refusing to continue')


def build_fingerprint():
    """Import-time state of the pristine package, from two differently seeded fresh interpreters;
    keys that disagree (sqlglot itself is nondeterministic there) are dropped."""
    snaps = []
    for seed in ('1', '7', '13', '42', '99', '123', '2024', '31337', '5', '777'):
        out = COV_DIR / f'fp_{seed}.json'
        env = dict(os.environ, PYTHONHASHSEED=seed, BR_SRC=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
        subprocess.run([sys.executable, str(HERE / 'fpr.py'), str(out)], cwd=ROOT, env=env, check=True,
                       stderr=subprocess.DEVNULL)
        snaps.append(json.loads(out.read_text()))
    a = snaps[0]['snap']
    stable, dropped = {}, {}
    for mod, keys in a.items():
        stable[mod] = {k: h for k, h in keys.items() if all(sn['snap'].get(mod, {}).get(k) == h for sn in snaps[1:])}
        lost = [k for k in keys if k not in stable[mod]]
        if lost:
            dropped[mod] = lost
    path = OUT / 'fingerprint.json.gz'
    with gzip.open(path, 'wt') as f:
        json.dump({'stable': stable, 'unstable': dropped, 'errors': snaps[0]['errors']}, f, separators=(',', ':'))
    # calibrate against the exact run-time mechanism: whatever it reports on the pristine tree
    # differs for environmental reasons (loggers, "unittest" in sys.modules, ...) and is dropped too
    env = dict(os.environ, BR_SRC=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
    out = subprocess.run([sys.executable, str(HERE / 'xrun.py'), 'calibrate', str(path)], cwd=ROOT, env=env,
                         capture_output=True, text=True).stdout
    res = json.loads(out) if out.strip() not in ('', 'null') else None
    if res is None:
        raise SystemExit('fingerprint calibration failed')
    for mod, key in res['changes']:
        if mod in stable and key in stable[mod]:
            del stable[mod][key]
            dropped.setdefault(mod, []).append(key)
    with gzip.open(path, 'wt') as f:
        json.dump({'stable': stable, 'unstable': dropped, 'errors': snaps[0]['errors']}, f, separators=(',', ':'))
    print(f'build_coverage: calibration dropped {len(res["changes"])} environment-dependent keys', flush=True)
    print(f'build_coverage: fingerprint {sum(len(v) for v in stable.values())} stable keys, '
          f'{sum(len(v) for v in dropped.values())} unstable dropped, import errors {snaps[0]["errors"]}', flush=True)


def main():
    if os.environ.get('BR_SKIP_RUN') != '1':
        COV_DIR.mkdir(parents=True, exist_ok=True)
        run_tests()
    build_map()
    build_fingerprint()


if __name__ == '__main__':
    main()
