#!/usr/bin/env python3
"""Mutants whose changed line is executed by at least one of the slow tests (per the impact map).

usage: mutate_heavy.py SRC_ROOT MAP_DIR OUT_DIR COUNT SEED
Only used to choose mutation SITES; ground truth still comes from really running the tests.
"""
import json
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ['BR_MAP_DIR'] = sys.argv[2]
import mutate  # noqa: E402
import selector  # noqa: E402

HEAVY = [
    'tests/test_parser.py::TestParser::test_max_nodes',
    'tests/test_executor.py::TestExecutor::test_execute_tpcds',
    'tests/test_executor.py::TestExecutor::test_execute_tpch',
    'tests/test_executor.py::TestExecutor::test_optimized_tpch',
    'tests/test_generator.py::TestGenerator::test_generator_memory',
]


def main():
    root, _, out, count, seed = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
    imp = selector.Impact()
    bits = [1 << imp.ids.index(h) for h in HEAVY if h in imp.ids]
    heavy_mask = sum(bits)
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted((root / 'sqlglot').rglob('*.py')):
        rel = p.relative_to(root).as_posix()
        rec = imp.file(rel)
        if rec and imp.mask(rec['m']) & heavy_mask:
            files.append((p, rec))
    meta = {}
    n = tries = 0
    while n < count and tries < count * 50:
        tries += 1
        path, rec = rng.choice(files)
        text = mutate.read(path)
        sites = mutate.py_sites(text)
        good = []
        for s in sites:
            ln = s[0] + 1
            for u in selector.units_at(rec, ln):
                if imp.mask(u[5]) & heavy_mask:
                    good.append(s)
                    break
        if not good:
            continue
        li, a, b, rep = rng.choice(good)
        lines = text.split('\n')
        old = lines[li]
        lines[li] = old[:a] + rep + old[b:]
        new = '\n'.join(lines)
        import ast
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        patch = mutate.mk_patch(rel, text, new)
        cid = f'h{seed}x{n:04d}'
        (out / f'{cid}.patch').write_text(patch, encoding='utf-8', newline='\n')
        meta[cid] = {'kind': 'heavy', 'desc': [f'L{li + 1} {old.strip()[:60]} -> {rep!r}'], 'files': [rel]}
        n += 1
    (out / '_meta.json').write_text(json.dumps(meta, indent=1))
    print(f'wrote {n} heavy-covered patches to {out} (from {len(files)} files)')


if __name__ == '__main__':
    main()
