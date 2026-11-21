#!/usr/bin/env python3
"""Blast Radius selector: statement-level coverage impact analysis over the pinned sqlglot tree.

The impact map (see build_coverage.py) is built once at image build time from the pinned public
source; at runtime this only reads the patches and tests.json. Stdlib only, lazily loaded, to
keep CPU seconds (which are added to the score) low.
"""
import gzip
import json
import os
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


def hazard(impact, rec, mod, owner):
    """Import-time code (class attributes, module constants): tests that use the owner class,
    or any code of its module when the owner has no recorded instances."""
    mask = 0
    if owner:
        key = f'{mod}:{owner}'
        if key in impact.classes:
            mask |= impact.mask(impact.classes[key])
        for u in rec['u']:
            if u[2] == K_CLASS and u[4] == owner:
                mask |= impact.mask(u[5])
        prefix = key + '.'
        for k, i in impact.classes.items():
            if k.startswith(prefix):
                mask |= impact.mask(i)
        if not mask:
            mask = impact.mask(rec['m'])
    else:
        mask = impact.mask(rec['m'])
    return mask


def line_candidates(impact, rec, ln, mod):
    """Mask of tests that could observe a change to old line `ln`."""
    us = units_at(rec, ln)
    if not us:
        # blank line, else:, try: ... -> the smallest enclosing def/class body, else the module
        sc = enclosing_scope(rec, ln)
        return impact.mask(sc[5]) if sc else impact.mask(rec['m'])
    inner = [u for u in us if u[2] == K_LAM and u[0] < ln < u[1]]
    if inner:
        us = [min(inner, key=lambda u: u[1] - u[0])]
    mask = 0
    for u in us:
        kind, imp, owner = u[2], u[3], u[4]
        mask |= impact.mask(u[5])
        if imp and kind in (K_STMT, K_HDR, K_CLASS):
            mask |= hazard(impact, rec, mod, owner)
    return mask


def block_candidates(impact, rec, b, mod):
    mask = 0
    for ln in b['dels']:
        mask |= line_candidates(impact, rec, ln, mod)
    if not b['dels']:
        # pure insertion of code: both neighbours plus the whole enclosing function
        p = b['pos']
        for ln in (max(1, p - 1), p):
            mask |= line_candidates(impact, rec, ln, mod)
        sc = enclosing_scope(rec, p)
        mask |= impact.mask(sc[5]) if sc else impact.mask(rec['m'])
    return mask


def select_for_patch(impact, tests_by_file, text):
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
                cand |= tests_by_file.get(rel, 0)
                if rel.rsplit('/', 1)[-1] in ('helpers.py', 'validator.py', '__init__.py'):
                    cand |= impact.all
            continue
        if not (rel.startswith('sqlglot/') and rel.endswith('.py')):
            continue
        rec = impact.file(rel)
        if rec is None:
            cand |= impact.all  # unknown python file inside the package: be safe
            notes.append(f'{rel} not in map -> all')
            continue
        mod = module_name(rel)
        for b in blocks_of(fp):
            texts = b['dtext'] + b['adds']
            if all(is_comment_or_blank(t) for t in texts):
                if not in_string(rec, b):
                    continue  # a plain comment cannot change behaviour
                lines = b['dels'] or [max(1, b['pos'] - 1), b['pos']]
                for ln in lines:
                    cand |= line_candidates(impact, rec, ln, mod)
                continue
            cand |= block_candidates(impact, rec, b, mod)
    return cand, notes


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


def main():
    t0 = time.process_time()
    tests = json.loads((DATA / 'tests.json').read_text())['tests']
    impact = Impact()
    number_of = {t['id']: t for t in tests}
    pos = {tid: i for i, tid in enumerate(impact.ids)}
    tests_by_file = {}
    for t in tests:
        i = pos.get(t['id'])
        if i is not None:
            tests_by_file[t['file']] = tests_by_file.get(t['file'], 0) | (1 << i)
    for patch in sorted((DATA / 'commits').glob('*.patch')):
        cand, notes = select_for_patch(impact, tests_by_file, patch.read_text(encoding='utf-8'))
        chosen = []
        m = cand
        while m:
            low = m & -m
            t = number_of.get(impact.ids[low.bit_length() - 1])
            if t is not None:
                chosen.append((t['number'], int(t['seconds'] * 1000 + 0.5)))
            m ^= low
        print(json.dumps({'commit': patch.stem, 'workers': schedule(chosen)}, separators=(',', ':')))
        if DEBUG:
            print(f'# {patch.stem}: {len(chosen)} tests, {sum(c[1] for c in chosen) / 1000:.1f}s {notes}', file=sys.stderr)
    print(f'selector cpu {time.process_time() - t0:.2f}s', file=sys.stderr)


if __name__ == '__main__':
    main()
