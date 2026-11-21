"""Slow-test gate analysis over labeled corpora (static candidates from a map vs real failures).

usage: gate_analysis.py MAP TESTS_JSON corpus:labels [corpus:labels ...]   e.g. mut_h:out/labels_h mut_a:out/labels_a
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['BR_MAP_DIR'] = sys.argv[1]
import selector  # noqa: E402

tests = json.loads(Path(sys.argv[2]).read_text())['tests']
by_id = {t['id']: t for t in tests}
imp = selector.Impact()
pos = {tid: i for i, tid in enumerate(imp.ids)}
tbf = {}
for t in tests:
    i = pos.get(t['id'])
    if i is not None:
        tbf[t['file']] = tbf.get(t['file'], 0) | (1 << i)
HEAVY = [t['id'] for t in tests if t['seconds'] >= 3.0]
rows = []
for spec in sys.argv[3:]:
    corpus, labels = spec.split(':')
    for lp in sorted(Path(labels).glob('*.json')):
        lab = json.loads(lp.read_text())
        if 'error' in lab:
            continue
        text = Path(corpus, f'{lp.stem}.patch').read_text()
        cand, _ = selector.select_for_patch(imp, tbf, text)
        cids = {imp.ids[i] for i in range(len(imp.ids)) if cand >> i & 1} | (set(HEAVY) if lab.get('cands_only') or labels.endswith('_h') else set())
        fail = set(lab['failing'])
        light_c = {c for c in cids if by_id[c]['seconds'] < 3.0}
        light_f = light_c & fail
        rows.append({'id': lp.stem, 'cids': cids, 'fail': fail, 'light_c': light_c, 'light_f': light_f})
print('mutants', len(rows))
O_T = 'tests/test_optimizer.py::TestOptimizer::test_tpcds'
O_H = 'tests/test_optimizer.py::TestOptimizer::test_tpch'
for h in HEAVY:
    name = h.split('::')[-1]
    cases = [r for r in rows if h in r['cids']]
    f = [r for r in cases if h in r['fail']]
    nolf = [r for r in f if not r['light_f']]
    lf = [r for r in cases if r['light_f']]
    f_lf = [r for r in lf if h in r['fail']]
    print(f"{name:28} cand={len(cases):3} fail={len(f):3} | light-failing cases={len(lf):3} heavy fails there={len(f_lf):3} | heavy fails with NO light failing={len(nolf)}")
    if 'tpcds' in name and 'execute' in name:
        fam = lambda r: any(('test_executor' in t) for t in r['light_f']) or O_T in r['light_f']
        print('   family rule (opt test_tpcds or any cheap executor test fails): heavy fails without family signal =',
              sum(1 for r in f if not fam(r)), 'of', len(f), '; family signal but heavy passes =', sum(1 for r in cases if fam(r) and h not in r['fail']))
