#!/usr/bin/env python3
"""Blast Radius selector: statement-level coverage impact analysis over the pinned sqlglot tree.

The impact map (see build_coverage.py) is built once at image build time from the pinned public
source; at runtime this only reads the patches and tests.json. Stdlib only, lazily loaded, to
keep CPU seconds (which are added to the score) low.
"""
import ast
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import patchlib  # noqa: E402

DATA = Path(os.environ.get('BR_DATA', '/data'))
MAP = Path(os.environ.get('BR_MAP_DIR', '/app/map'))
SRC = Path(os.environ.get('BR_SRC', '/app/sqlglot'))

K_STMT, K_HDR, K_DEF, K_CLASS, K_LAM = 0, 1, 2, 3, 4
DEBUG = os.environ.get('BR_DEBUG') == '1'


class Impact:
    def __init__(self):
        with gzip.open(MAP / 'global.json.gz', 'rt') as f:
            g = json.load(f)
        self.ids = g['ids']
        self.width = g['width']
        self.classes = g['classes']
        self.fixtures = g['fixtures']
        self.eager = set(g['eager'])
        self.files = set(g['files'])
        self._index = None
        self.names = g.get('names', {})
        self.modfile = {}
        for rel in g['files']:
            self.modfile[module_name(rel)] = rel
        self.cls_by_mod = {}
        for k, i in self.classes.items():
            m, _, q = k.partition(':')
            self.cls_by_mod.setdefault(m, []).append((q, i))
        self.outside = g.get('outside', {})
        self.pickle_files = set(g.get('pickle_files', ()))
        self._src = {}
        with gzip.open(MAP / 'sets.bin.gz', 'rb') as f:
            self.blob = f.read()
        self.all = (1 << len(self.ids)) - 1
        self._cache = {}
        self._files = {}

    def mask(self, i):
        m = self._cache.get(i)
        if m is None:
            w = self.width
            m = int.from_bytes(self.blob[i * w:(i + 1) * w], 'little')
            self._cache[i] = m
        return m

    def test_index(self):
        """masks of tests per file, per file::class and per test id"""
        if self._index is None:
            idx = {}
            for i, tid in enumerate(self.ids):
                parts = tid.split('::')
                for k in range(1, len(parts) + 1):
                    key = '::'.join(parts[:k])
                    idx[key] = idx.get(key, 0) | (1 << i)
            self._index = idx
        return self._index

    def src_lines(self, rel):
        lines = self._src.get(rel)
        if lines is None:
            try:
                lines = (SRC / rel).read_text(encoding='utf-8').split(chr(10))
            except OSError:
                lines = []
            self._src[rel] = lines
        return lines

    def src_text(self, rel):
        return chr(10).join(self.src_lines(rel))

    def src_line(self, rel, ln):
        lines = self._src.get(rel)
        if lines is None:
            try:
                lines = (SRC / rel).read_text(encoding='utf-8').split(chr(10))
            except OSError:
                lines = []
            self._src[rel] = lines
        return lines[ln - 1] if 0 < ln <= len(lines) else ''

    def file(self, rel):
        rec = self._files.get(rel)
        if rec is None:
            p = MAP / 'files' / (rel.replace('/', '__') + '.json.gz')
            if not p.exists():
                return None
            with gzip.open(p, 'rt') as f:
                rec = json.load(f)
            self._files[rel] = rec
        return rec


def module_name(rel):
    parts = rel[:-3].split('/')
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def is_comment_or_blank(line):
    s = line.strip()
    return not s or s.startswith('#')


def blocks_of(fp):
    """Group a file patch into change blocks: dels (old line numbers), dtext, adds, pos."""
    out = []
    for old_start, old_len, lines in fp['hunks']:
        old = old_start if old_len else old_start + 1
        cur = None
        for l in lines:
            tag, body = l[0], l[1:]
            if tag == ' ':
                cur = None
                old += 1
                continue
            if cur is None:
                cur = {'dels': [], 'dtext': [], 'adds': [], 'pos': old}
                out.append(cur)
            if tag == '-':
                cur['dels'].append(old)
                cur['dtext'].append(body)
                old += 1
            else:
                cur['adds'].append(body)
    return out


def in_string(rec, block):
    """Does a comment-only block land inside a multi-line string (where '#' is data)?"""
    spans = rec['s']
    for ln in block['dels']:
        if any(a <= ln <= b for a, b in spans):
            return True
    if not block['dels']:
        p = block['pos']  # new text sits between old lines p-1 and p
        return any(a < p <= b for a, b in spans)
    return False


def units_at(rec, ln):
    return [u for u in rec['u'] if u[0] <= ln <= u[1]]


def enclosing_scope(rec, ln):
    """Smallest def/class whose full span (unit[6]) holds `ln`, or None."""
    best = None
    for u in rec['u']:
        if u[2] in (K_DEF, K_CLASS) and u[0] <= ln <= u[6]:
            if best is None or (u[6] - u[0]) < (best[6] - best[0]):
                best = u
    return best


TARGET = re.compile(r'^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)')


def resolve_owner(impact, rec, mod, owner):
    """Longest prefix of `owner` that names a real class (alias attributes like X.tokenizer_class)."""
    known = {q for q, _ in impact.cls_by_mod.get(mod, ())} | {u[4] for u in rec['u'] if u[2] == K_CLASS}
    o = owner
    while o and o not in known:
        o = o.rsplit('.', 1)[0] if '.' in o else ''
    return o


def hazard(impact, rec, mod, owner, name, ignore_imp=False):
    """Import-time code (class attribute, module constant): which tests can observe the change?
    Tests that use the owner class (or run code inside it) AND run code that mentions the changed
    name; both are necessary conditions. Falls back to the wider set when either is unknown."""
    mask = 0
    if owner:
        owner = resolve_owner(impact, rec, mod, owner)
    if owner:
        prefix = owner + '.'
        for q, i in impact.cls_by_mod.get(mod, ()):
            if q == owner or q.startswith(prefix):
                mask |= impact.mask(i)
        for u in rec['u']:
            if u[2] == K_CLASS and u[4] == owner:
                mask |= impact.mask(u[5])
    nm = impact.names.get(name) if name else None
    if nm is not None and (ignore_imp or not nm[1]):
        nmask = impact.mask(nm[0])
        if nmask:
            return (mask & nmask) if mask else nmask
    return mask or impact.mask(rec['m'])


_HAZ = None    # when a list: static hazard masks are collected here instead of being OR-ed in
_TRIG = False  # set when a change touches code that runs while modules/classes are created


def line_candidates(impact, rel, rec, ln, mod):
    """Mask of tests that could observe a change to old line `ln`."""
    us = units_at(rec, ln)
    if not us:
        # blank line, else:, try: ... -> the smallest enclosing def/class body, else the module
        sc = enclosing_scope(rec, ln)
        return impact.mask(sc[5]) if sc else impact.mask(rec['m'])
    inner = [u for u in us if u[2] == K_LAM and u[0] < ln < u[1]]
    if inner:
        us = [min(inner, key=lambda u: u[1] - u[0])]
    global _TRIG
    mask = 0
    iset = rec.get('_iset')
    if iset is None:
        iset = rec['_iset'] = set(rec['i'])
    if ln in iset:
        _TRIG = True
    for u in us:
        kind, imp, owner = u[2], u[3], u[4]
        mask |= impact.mask(u[5])
        if imp:
            _TRIG = True
        if imp and kind in (K_STMT, K_HDR, K_CLASS):
            m = TARGET.match(impact.src_line(rel, u[0])) if kind != K_CLASS else None
            hm = hazard(impact, rec, mod, owner, m.group(1) if m else None)
            if _HAZ is not None:
                _HAZ.append(hm)
            else:
                mask |= hm
    return mask


def block_candidates(impact, rel, rec, b, mod):
    mask = 0
    for ln in b['dels']:
        mask |= line_candidates(impact, rel, rec, ln, mod)
    if not b['dels']:
        # pure insertion of code: both neighbours plus the whole enclosing function
        p = b['pos']
        for ln in (max(1, p - 1), p):
            mask |= line_candidates(impact, rel, rec, ln, mod)
        sc = enclosing_scope(rec, p)
        mask |= impact.mask(sc[5]) if sc else impact.mask(rec['m'])
    return mask


def delta_before(blocks, ln):
    return sum(len(b['adds']) - len(b['dels']) for b in blocks if b['pos'] <= ln)


def compiles(lines):
    try:
        compile('if 1:' + chr(10) + chr(10).join(' ' + l for l in lines), '<window>', 'exec', dont_inherit=True)
        return True
    except SyntaxError:
        return False
    except (ValueError, RecursionError, MemoryError):
        return True


def compiles_whole(lines):
    try:
        compile(chr(10).join(lines), '<file>', 'exec', dont_inherit=True)
        return True
    except SyntaxError:
        return False
    except (ValueError, RecursionError, MemoryError):
        return True


def broken_syntax(impact, rel, fp, blocks, code_blocks):
    """Does the patched file still compile? Checks the outermost function around each code change
    (cheap) and the whole file when a change sits outside any function."""
    old = impact.src_lines(rel)
    if not old:
        return False
    try:
        new = patchlib.apply_hunks(old[:-1] if old and old[-1] == '' else old, fp['hunks'])
    except ValueError:
        return True  # the patch does not even apply to the pinned file
    rec = impact.file(rel)
    whole = False
    for b in code_blocks:
        lns = b['dels'] or [b['pos']]
        for ln in lns:
            defs = [u for u in rec['u'] if u[2] == K_DEF and u[0] <= ln <= u[6]]
            if not defs:
                whole = True
                continue
            d = max(defs, key=lambda u: u[6] - u[0])
            a, e = d[0] + delta_before(blocks, d[0] - 1), d[6] + delta_before(blocks, d[6])
            if not compiles(new[a - 1:e]):
                return True
    if whole:
        return not compiles_whole(new)
    return False


def module_users(impact, rec, mod, rel):
    """Everything that could be affected if the module cannot be imported."""
    if rel in impact.eager:
        return impact.all
    mask = impact.mask(rec['m'])
    prefix = mod + ':'
    for k, i in impact.classes.items():
        if k.startswith(prefix):
            mask |= impact.mask(i)
    return mask


def test_file_candidates(impact, rel, fp):
    """A change to a test module: only the test functions whose code changed (or the whole class /
    module when the change is in shared code such as setUp, class attributes or imports)."""
    idx = impact.test_index()
    whole = idx.get(rel, 0)
    try:
        tree = ast.parse(impact.src_text(rel))
    except (SyntaxError, ValueError):
        return whole
    meths, classes, strings = [], [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append((node.lineno, node.end_lineno, node.name))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([sub.lineno] + [d.lineno for d in sub.decorator_list])
                    meths.append((start, sub.end_lineno, node.name, sub.name))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and getattr(node, 'end_lineno', 0) != node.lineno:
            if not isinstance(node, ast.Constant) or isinstance(node.value, str):
                strings.append((node.lineno, node.end_lineno))
    mask = 0
    for b in blocks_of(fp):
        texts = b['dtext'] + b['adds']
        lines = list(b['dels']) or [max(1, b['pos'] - 1), b['pos']]
        if all(is_comment_or_blank(t) for t in texts):
            inside = (any(a <= ln <= e for ln in b['dels'] for a, e in strings) if b['dels']
                      else any(a < b['pos'] <= e for a, e in strings))
            if not inside:
                continue
        for ln in lines:
            hit = [m for m in meths if m[0] <= ln <= m[1]]
            if hit:
                _, _, cls, name = hit[0]
                if name.startswith('test'):
                    mask |= idx.get(f'{rel}::{cls}::{name}', 0)
                else:
                    mask |= idx.get(f'{rel}::{cls}', 0)
                continue
            cl = [c for c in classes if c[0] <= ln <= c[1]]
            mask |= idx.get(f'{rel}::{cl[0][2]}', 0) if cl else whole
    return mask


def select_for_patch(impact, tests_by_file, text):
    d = select_detail(impact, tests_by_file, text)
    return d['core'] | d['haz'], d['notes']


def select_detail(impact, tests_by_file, text):
    """core: tests that can observe the change through code that runs inside tests (line coverage);
    haz: static guess for import-time effects; trig: the change touches import-time code."""
    global _HAZ, _TRIG
    _HAZ, _TRIG = [], False
    cand = 0
    notes = []
    for fp in patchlib.parse(text):
        rel = fp['new'] or fp['old']
        if rel is None:
            continue
        if rel.startswith('tests/fixtures/'):
            i = impact.fixtures.get(rel)
            if i is not None:
                cand |= impact.mask(i)
            else:
                notes.append(f'fixture {rel} unmapped')
            continue
        if rel.startswith('tests/'):
            if rel.endswith('.py'):
                if rel.rsplit('/', 1)[-1] in ('helpers.py', 'validator.py', '__init__.py', 'conftest.py'):
                    cand |= impact.all
                elif rel in impact.test_index():
                    cand |= test_file_candidates(impact, rel, fp)
                else:
                    cand |= tests_by_file.get(rel, 0)
            continue
        if not (rel.startswith('sqlglot/') and rel.endswith('.py')):
            if not rel.startswith(('sqlglot/', 'tests/')):
                comment_only = rel.endswith('.py') and all(
                    is_comment_or_blank(t) for b in blocks_of(fp) for t in b['dtext'] + b['adds'])
                if not comment_only:
                    for tok in (rel, rel.rsplit('/', 1)[-1], rel.split('/')[0]):
                        i = impact.outside.get(tok)
                        if i is not None:
                            cand |= impact.mask(i)
            continue
        rec = impact.file(rel)
        if rec is None:
            cand |= impact.all  # unknown python file inside the package: be safe
            notes.append(f'{rel} not in map -> all')
            continue
        mod = module_name(rel)
        blocks = blocks_of(fp)
        code_blocks = []
        old_lines = impact.src_lines(rel)
        for b in blocks:
            texts = b['dtext'] + b['adds']
            if all(is_comment_or_blank(t) for t in texts):
                prev = old_lines[b['pos'] - 2] if 2 <= b['pos'] <= len(old_lines) + 1 else ''
                if not in_string(rec, b) and not prev.rstrip().endswith(chr(92)):
                    continue  # a plain comment cannot change behaviour
                lines = b['dels'] or [max(1, b['pos'] - 1), b['pos']]
                for ln in lines:
                    cand |= line_candidates(impact, rel, rec, ln, mod)
                continue
            code_blocks.append(b)
            cand |= block_candidates(impact, rel, rec, b, mod)
        if code_blocks and broken_syntax(impact, rel, fp, blocks, code_blocks):
            cand |= module_users(impact, rec, mod, rel)
            notes.append(f'{rel} does not compile')
    haz = 0
    for m in _HAZ:
        haz |= m
    trig = _TRIG
    _HAZ, _TRIG = None, False
    return {'core': cand, 'haz': haz, 'trig': trig, 'notes': notes}


def schedule(items):
    """items: [(number, ms)] -> workers minimizing max load + 2000 ms per worker (LPT per count)."""
    if not items:
        return []
    items = sorted(items, key=lambda x: -x[1])
    best, best_cost = None, None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n, ms in items:
            i = loads.index(min(loads))
            loads[i] += ms
            bins[i].append(n)
        cost = max(loads) + 2000 * k
        if best_cost is None or cost < best_cost:
            best, best_cost = bins, cost
    return best


MODE = os.environ.get('BR_MODE', 'x')
HEAVY_SECS = float(os.environ.get('BR_HEAVY_SECS', '3.0'))
X_BUDGET = float(os.environ.get('BR_X_BUDGET', '8.0'))
HEAVY_POLICY = os.environ.get('BR_HEAVY_POLICY', 'gate')  # gate: heavy only if a light test failed
PARALLEL = int(os.environ.get('BR_PARALLEL', '2'))
GATE_BUDGET = float(os.environ.get('BR_GATE_BUDGET', '12.0'))
GATE = os.environ.get('BR_GATE', '1') == '1'
ITSD_ON = os.environ.get('BR_ITSD', '1') == '1'
GATE_RUN = os.environ.get('BR_GATE_RUN', '1') == '1'


def sched_cost(items):
    bins = schedule(items)
    if not bins:
        return 0.0
    ms = {n: m for n, m in items}
    return (max(sum(ms[n] for n in b) for b in bins) + 2000 * len(bins)) / 1000.0


GATE_K = float(os.environ.get('BR_GATE_K', '1.5'))      # CPU seconds per recorded second when run in one process
GATE_RATIO = float(os.environ.get('BR_GATE_RATIO', '1.3'))


def patched_sources(impact, text):
    """Patched texts of sqlglot/*.py and tests/fixtures/*; ok=False when the patch touches anything
    else that the runner cannot reproduce faithfully (test code, other python files)."""
    py, fx, ok = {}, {}, True
    for fp in patchlib.parse(text):
        rel = fp['new'] or fp['old']
        if rel is None:
            ok = False
            continue
        if rel.startswith('sqlglot/') and rel.endswith('.py'):
            target = py
        elif rel.startswith('tests/fixtures/'):
            target = fx
        else:
            if rel.startswith('tests/') or rel.endswith('.py'):
                ok = False
            continue
        try:
            old = (SRC / rel).read_text(encoding='utf-8')
            target[rel] = patchlib.apply_to_text(old, fp['hunks'])
        except (OSError, ValueError):
            ok = False
    return py, fx, ok


def mask_to_tests(impact, number_of, mask):
    out = []
    m = mask
    while m:
        low = m & -m
        tid = impact.ids[low.bit_length() - 1]
        t = number_of.get(tid)
        if t is not None:
            out.append(t)
        m ^= low
    return out


CPU_LOG = []


def run_jobs(starters, deadline):
    """starters: {key: callable -> xrun.Job | None}. PARALLEL children at once (smallest first); nothing
    new starts after `deadline` (a monotonic time). Returns {key: result | None}."""
    results, running, queue = {}, {}, list(starters)
    while queue or running:
        while queue and len(running) < PARALLEL and time.monotonic() < deadline:
            c = queue.pop(0)
            job = starters[c]()
            if job is None:
                results[c] = None
            else:
                running[c] = job
        if not running:
            for c in queue:
                results[c] = None
            break
        for c in list(running):
            if running[c].poll(False):
                results[c] = running[c].out
                CPU_LOG.append((c, running[c].cpu))
                del running[c]
        if running:
            time.sleep(0.01)
    return results


def itsd_mask(impact, res):
    """Tests that can observe the import-time state changes (and import failures) a patch causes."""
    mask = 0
    cache = {}
    for mod, key in res['changes']:
        rel = impact.modfile.get(mod)
        rec = impact.file(rel) if rel else None
        if rec is None or key == '*':
            mask |= module_users(impact, rec, mod, rel) if rec else impact.all
            continue
        owner, attr = key.rsplit('.', 1) if '.' in key else ('', key)
        ck = (mod, owner, attr)
        if ck not in cache:
            # import-time uses of the name are already reflected in the state diff itself
            cache[ck] = hazard(impact, rec, mod, owner, attr, True)
        mask |= cache[ck]
    for mod in res['errors']:
        rel = impact.modfile.get(mod)
        rec = impact.file(rel) if rel else None
        mask |= module_users(impact, rec, mod, rel) if rec else impact.all
    return mask


def main():
    t0 = time.process_time()
    wall0 = time.monotonic()
    tests = json.loads((DATA / 'tests.json').read_text())['tests']
    impact = Impact()
    number_of = {t['id']: t for t in tests}
    pos = {tid: i for i, tid in enumerate(impact.ids)}
    tests_by_file = {}
    for t in tests:
        i = pos.get(t['id'])
        if i is not None:
            tests_by_file[t['file']] = tests_by_file.get(t['file'], 0) | (1 << i)
    patches = sorted((DATA / 'commits').glob('*.patch'))
    plan = {}
    for patch in patches:
        text = patch.read_text(encoding='utf-8')
        d = select_detail(impact, tests_by_file, text)
        d['text'] = text
        d['mask'] = d['core'] | d['haz']
        plan[patch.stem] = d
    can_run = MODE == 'x' and hasattr(os, 'fork')
    deadline = wall0 + max(20.0, 2.0 * len(patches))
    baseline = MAP / 'fingerprint.json.gz'
    if can_run and ITSD_ON and baseline.exists():
        import xrun
        starters = {}
        for c, d in plan.items():
            if d['trig']:
                py, _, ok = patched_sources(impact, d['text'])
                if ok and py:
                    starters[c] = (lambda py=py: xrun.start_itsd(py, str(baseline), 30))
        for c, res in run_jobs(starters, deadline).items():
            d = plan[c]
            if res is not None:
                d['mask'] = d['core'] | itsd_mask(impact, res)
                d['notes'].append(f'itsd: {len(res["changes"])} changes, {len(res["errors"])} import errors')
            else:
                d['notes'].append('itsd failed: static hazard')
    for d in plan.values():
        d['chosen'] = mask_to_tests(impact, number_of, d['mask'])
    results = {}
    if can_run and GATE_RUN:
        import xrun
        starters = {}
        for c, d in plan.items():
            heavy = [t for t in d['chosen'] if t['seconds'] >= HEAVY_SECS]
            light = sorted((t for t in d['chosen'] if t['seconds'] < HEAVY_SECS), key=lambda t: t['seconds'])
            tl = sum(t['seconds'] for t in light)
            if not light or any('does not compile' in n for n in d['notes']):
                continue
            if GATE and not heavy:
                continue  # nothing expensive to gate: plain selection is cheaper than running anything
            if GATE:
                full = sched_cost([(t['number'], int(t['seconds'] * 1000 + 0.5)) for t in d['chosen']])
                if full <= GATE_RATIO * (GATE_K * tl + 0.8) + 1.0:
                    continue  # probing would cost about as much CPU as it could save
            elif tl > X_BUDGET:
                continue
            py, fx, ok = patched_sources(impact, d['text'])
            if not ok:
                continue
            ids = [t['id'] for t in light]
            d['fx'] = fx
            pk = any(rel in impact.pickle_files for rel in py)
            starters[c] = (lambda py=py, fx=fx, ids=ids, tl=tl, pk=pk: xrun.start(py, fx, ids, 3 * tl + 15, GATE, pk))
        results = run_jobs(starters, deadline)
    for patch in patches:
        c = patch.stem
        d = plan[c]
        chosen = d['chosen']
        r = results.get(c)
        mode = 's'
        if r is not None and len(r['fx_used']) >= len(d.get('fx', {})):
            failed = set(r['failed'])
            if GATE:
                # light candidates are the probe: nothing failed -> the expensive tests are skipped too
                chosen, mode = ([], 'g0') if not failed else (chosen, 'g1')
            else:
                heavy = [t for t in chosen if t['seconds'] >= HEAVY_SECS]
                chosen = [t for t in chosen if t['id'] in failed]
                if heavy and (failed or HEAVY_POLICY == 'all'):
                    chosen += heavy
                mode = 'x'
        items = [(t['number'], int(t['seconds'] * 1000 + 0.5)) for t in chosen]
        print(json.dumps({'commit': c, 'workers': schedule(items)}, separators=(',', ':')))
        if DEBUG:
            print(f'# {c}: mode={mode} {len(chosen)} tests, {sum(t[1] for t in items) / 1000:.1f}s {d["notes"]}',
                  file=sys.stderr)
    if DEBUG:
        print('# jobcpu ' + ' '.join(f'{c}={v:.2f}' for c, v in CPU_LOG), file=sys.stderr)
    print(f'selector cpu {time.process_time() - t0:.2f}s wall {time.monotonic() - wall0:.1f}s', file=sys.stderr)


if __name__ == '__main__':
    main()
