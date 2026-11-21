#!/usr/bin/env python3
"""Mutants of tests/*.py (operator swaps / constants in test code). usage: mutate_tests.py SRC OUT COUNT SEED"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate  # noqa: E402

root, out, count, seed = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
out.mkdir(parents=True, exist_ok=True)
files = sorted(p for p in (root / 'tests').rglob('test_*.py') if p.name not in ('test_docs.py', 'test_sqlglotc.py'))
meta, n, tries = {}, 0, 0
while n < count and tries < count * 30:
    tries += 1
    r = mutate.code_mutant(root, rng, files)
    if not r:
        continue
    rel, old, new, desc = r
    patch = mutate.mk_patch(rel, old, new)
    if not patch:
        continue
    cid = f't{seed}x{n:04d}'
    (out / f'{cid}.patch').write_text(patch, encoding='utf-8', newline='\n')
    meta[cid] = {'kind': 'testcode', 'desc': [desc], 'files': [rel]}
    n += 1
(out / '_meta.json').write_text(json.dumps(meta, indent=1))
print(f'wrote {n} test-code patches to {out}')
