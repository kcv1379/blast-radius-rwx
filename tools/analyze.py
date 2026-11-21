#!/usr/bin/env python3
"""Compare the selector's candidate sets with ground-truth labels on a labeled mutant corpus.

usage: analyze.py MAP_DIR PATCH_DIR LABEL_DIR TESTS_JSON [--heavy-secs 3.0]
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('map')
    ap.add_argument('patches')
    ap.add_argument('labels')
    ap.add_argument('tests')
    ap.add_argument('--heavy-secs', type=float, default=3.0)
    ap.add_argument('--show', type=int, default=15)
    a = ap.parse_args()
    os.environ['BR_MAP_DIR'] = a.map
    import selector
    tests = json.loads(Path(a.tests).read_text())['tests']
    imp = selector.Impact()
    by_id = {t['id']: t for t in tests}
    pos = {tid: i for i, tid in enumerate(imp.ids)}
    tests_by_file = {}
    for t in tests:
        i = pos.get(t['id'])
        if i is not None:
            tests_by_file[t['file']] = tests_by_file.get(t['file'], 0) | (1 << i)
    meta_p = Path(a.patches) / '_meta.json'
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    rows = []
    for lp in sorted(Path(a.labels).glob('*.json')):
        lab = json.loads(lp.read_text())
        if 'error' in lab:
            continue
        cid = lp.stem
        text = (Path(a.patches) / f'{cid}.patch').read_text(encoding='utf-8')
        cand, notes = selector.select_for_patch(imp, tests_by_file, text)
        cand_ids = {imp.ids[i] for i in range(len(imp.ids)) if cand >> i & 1}
        fail = set(lab['failing'])
        miss = sorted(fail - cand_ids)
        csec = sum(by_id[i]['seconds'] for i in cand_ids if i in by_id)
        heavy_c = sorted(i for i in cand_ids if i in by_id and by_id[i]['seconds'] >= a.heavy_secs)
        heavy_f = sorted(i for i in fail if i in by_id and by_id[i]['seconds'] >= a.heavy_secs)
        rows.append({'id': cid, 'kind': meta.get(cid, {}).get('kind', '?'), 'ncand': len(cand_ids), 'csec': csec,
                     'nfail': len(fail), 'miss': miss, 'heavy_c': heavy_c, 'heavy_f': heavy_f,
                     'desc': meta.get(cid, {}).get('desc', [''])[0][:90]})
    n = len(rows)
    nmiss = [r for r in rows if r['miss']]
    print(f'labeled mutants: {n}; with a failing test: {sum(1 for r in rows if r["nfail"])}; '
          f'commits with a MISS (candidate set lacks a failing test): {len(nmiss)}')
    for r in nmiss[:a.show]:
        print(f'  MISS {r["id"]} [{r["kind"]}] {r["desc"]} missing={[m.split("::", 1)[1] for m in r["miss"]][:4]}')
    print(f'heavy (>= {a.heavy_secs}s) candidates: in {sum(1 for r in rows if r["heavy_c"])} mutants; '
          f'heavy actually failing in {sum(1 for r in rows if r["heavy_f"])}')
    both = [r for r in rows if r['heavy_c']]
    print(f'  among mutants with heavy candidates: heavy-failure rate {sum(1 for r in both if r["heavy_f"])}/{len(both)}')
    print(f'  mean candidate seconds {sum(r["csec"] for r in rows) / max(n, 1):.2f}')


if __name__ == '__main__':
    main()
