#!/usr/bin/env python3
"""Generate synthetic single-line mutation patches against a sqlglot checkout (offline validation).

usage: mutate.py SRC_ROOT OUT_DIR COUNT SEED [--weights code:16,fixture:8,multi:3,comment:3]
Patches mimic the official sample: operator swaps, constant tweaks, fixture corruption,
inserted comments, multi-file changes.
"""
import argparse
import ast
import difflib
import io
import json
import random
import re
import tokenize
from pathlib import Path

COMMENTS = [
    '# Cheap check first', '# Keep in sync with the generator', '# Intentionally not cached',
    '# Kept for backwards compatibility', '# Fast path', '# See dialect docs', '# NOTE: order matters',
    '# TODO: revisit', '# Handled elsewhere',
]
SWAPS = {
    '==': '!=', '!=': '==', '<=': '<', '>=': '>', '<': '<=', '>': '>=',
    'and': 'or', 'or': 'and', 'True': 'False', 'False': 'True', '+': '-', '-': '+',
}


def read(path):
    return path.read_text(encoding='utf-8').replace('\r\n', '\n')


def py_sites(text):
    """Mutation sites: (line_idx0, col_start, col_end, replacement)."""
    sites = []
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    for i, tok in enumerate(toks):
        s, e = tok.start, tok.end
        if s[0] != e[0]:
            continue
        v = tok.string
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        prev = toks[i - 1] if i else None
        if tok.type == tokenize.NAME:
            if v in ('and', 'or', 'True', 'False'):
                sites.append((s[0] - 1, s[1], e[1], SWAPS[v]))
            elif v == 'is':
                if nxt is not None and nxt.string == 'not' and nxt.start[0] == s[0]:
                    sites.append((s[0] - 1, s[1], nxt.end[1], '=='))
                    sites.append((s[0] - 1, s[1], nxt.end[1], 'is'))
                else:
                    sites.append((s[0] - 1, s[1], e[1], '=='))
                    sites.append((s[0] - 1, s[1], e[1], 'is not'))
            elif v == 'not' and nxt is not None and nxt.string != 'in' and (prev is None or prev.string != 'is'):
                if nxt.start[0] == s[0]:
                    sites.append((s[0] - 1, s[1], nxt.start[1], ''))
        elif tok.type == tokenize.OP and v in ('==', '!=', '<=', '>=', '<', '>'):
            sites.append((s[0] - 1, s[1], e[1], SWAPS[v]))
        elif tok.type == tokenize.OP and v in ('+', '-'):
            if prev is not None and (prev.type in (tokenize.NAME, tokenize.NUMBER) or prev.string in (')', ']')):
                if prev.string not in ('return', 'in', 'and', 'or', 'not', 'if', 'else', 'lambda'):
                    sites.append((s[0] - 1, s[1], e[1], SWAPS[v]))
        elif tok.type == tokenize.NUMBER and re.fullmatch(r'\d+', v):
            n = int(v)
            for r in {n + 1, max(0, n - 1), 0 if n else 1}:
                if r != n:
                    sites.append((s[0] - 1, s[1], e[1], str(r)))
    return sites


def mk_patch(rel, old_text, new_text):
    body = list(difflib.unified_diff(old_text.split('\n'), new_text.split('\n'),
                                     f'a/{rel}', f'b/{rel}', n=3, lineterm=''))
    if not body:
        return None
    return f'diff --git a/{rel} b/{rel}\nindex 0000000..0000001 100644\n' + '\n'.join(body) + '\n'


def code_mutant(root, rng, files):
    for _ in range(200):
        path = rng.choice(files)
        text = read(path)
        try:
            sites = py_sites(text)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            continue
        if not sites:
            continue
        li, a, b, rep = rng.choice(sites)
        lines = text.split('\n')
        old_line = lines[li]
        lines[li] = old_line[:a] + rep + old_line[b:]
        new = '\n'.join(lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        if new == text:
            continue
        return path.relative_to(root).as_posix(), text, new, f'L{li + 1} {old_line.strip()[:60]} -> {rep!r}'
    return None


def comment_mutant(root, rng, files):
    path = rng.choice(files)
    text = read(path)
    lines = text.split('\n')
    i = 1
    for _ in range(50):
        i = rng.randrange(1, len(lines))
        if lines[i].strip():
            break
    indent = re.match(r'\s*', lines[i]).group(0)
    new_lines = lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:]
    return path.relative_to(root).as_posix(), text, '\n'.join(new_lines), f'comment before L{i + 1}'


def fixture_mutant(root, rng, files):
    for _ in range(100):
        path = rng.choice(files)
        text = read(path)
        lines = text.split('\n')
        cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(('--', '#')) and len(l) > 8]
        if not cand:
            continue
        i = rng.choice(cand)
        words = list(re.finditer(r'\S+', lines[i]))
        if not words:
            continue
        m = rng.choice(words)
        mode = rng.choice(['insert_tok', 'insert_tok', 'dup_char', 'drop_char'])
        l = lines[i]
        if mode == 'insert_tok':
            tok = rng.choice(['1', 'x', 'xa', 'a', '0'])
            new_line = l[:m.start()] + tok + ' ' + l[m.start():]
        elif mode == 'dup_char':
            p = rng.randrange(m.start(), m.end())
            new_line = l[:p] + l[p] + l[p:]
        else:
            p = rng.randrange(m.start(), m.end())
            new_line = l[:p] + l[p + 1:]
        if new_line == l:
            continue
        lines[i] = new_line
        return path.relative_to(root).as_posix(), text, '\n'.join(lines), f'fixture L{i + 1} {mode}'
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('count', type=int)
    ap.add_argument('seed', type=int)
    ap.add_argument('--weights', default='code:16,fixture:8,multi:3,comment:3')
    a = ap.parse_args()
    root = Path(a.src)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    py_files = sorted(p for p in (root / 'sqlglot').rglob('*.py') if p.name != '__init__.py' or p.parent.name == 'sqlglot')
    fixtures = sorted((root / 'tests' / 'fixtures').rglob('*.sql'))
    kinds, wts = zip(*[(k, int(w)) for k, w in (x.split(':') for x in a.weights.split(','))])
    gens = {'code': (code_mutant, py_files), 'comment': (comment_mutant, py_files), 'fixture': (fixture_mutant, fixtures)}
    meta = {}
    n = tries = 0
    while n < a.count and tries < a.count * 20:
        tries += 1
        kind = rng.choices(kinds, wts)[0]
        subkinds = [kind] if kind != 'multi' else rng.sample(['code', 'comment', 'fixture'], rng.choice([2, 3]))
        parts = []
        for sk in subkinds:
            fn, pool = gens[sk]
            r = fn(root, rng, pool)
            if r and all(r[0] != p[0] for p in parts):
                parts.append(r)
        if not parts or (kind == 'multi' and len(parts) < 2):
            continue
        patches = [mk_patch(rel, old, new) for rel, old, new, _ in parts]
        if any(p is None for p in patches):
            continue
        suffix = ''.join(rng.choice('0123456789abcdefghijklmnopqrstuvwxyz') for _ in range(3))
        cid = f'm{a.seed}x{n:04d}{suffix}'
        (out / f'{cid}.patch').write_text(''.join(patches), encoding='utf-8', newline='\n')
        meta[cid] = {'kind': kind, 'desc': [p[3] for p in parts], 'files': [p[0] for p in parts]}
        n += 1
    (out / '_meta.json').write_text(json.dumps(meta, indent=1))
    print(f'wrote {n} patches to {out}')


if __name__ == '__main__':
    main()
