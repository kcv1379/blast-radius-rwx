"""Functional test of xrun on commits with known answers (runs in the lab container).

usage: xtest.py PATCH_DIR TESTS_JSON ANSWERS_JSON [--max-secs 2.5] [--only a,b]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchlib  # noqa: E402
import selector  # noqa: E402
import xrun  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('patches')
    ap.add_argument('tests')
    ap.add_argument('answers')
    ap.add_argument('--max-secs', type=float, default=2.5)
    ap.add_argument('--only', default='')
    ap.add_argument('--max-secs-all', type=float, default=0)
    a = ap.parse_args()
    tests = json.loads(Path(a.tests).read_text())['tests']
    by_id = {t['id']: t for t in tests}
    by_num = {t['number']: t for t in tests}
    ans = json.loads(Path(a.answers).read_text())['commits']
    imp = selector.Impact()
    pos = {tid: i for i, tid in enumerate(imp.ids)}
    tbf = {}
    for t in tests:
        i = pos.get(t['id'])
        if i is not None:
            tbf[t['file']] = tbf.get(t['file'], 0) | (1 << i)
    root = Path(xrun.ROOT)
    t_all = time.process_time()
    ok = bad = 0
    for p in sorted(Path(a.patches).glob('*.patch')):
        cid = p.stem
        if a.only and cid not in a.only.split(','):
            continue
        text = p.read_text(encoding='utf-8')
        cand, notes = selector.select_for_patch(imp, tbf, text)
        ids = [imp.ids[i] for i in range(len(imp.ids)) if cand >> i & 1 and imp.ids[i] in by_id]
        cheap = [i for i in ids if by_id[i]['seconds'] <= a.max_secs]
        py, fx = {}, {}
        for fp in patchlib.parse(text):
            rel = fp['new'] or fp['old']
            old = (root / rel).read_text(encoding='utf-8')
            new = patchlib.apply_to_text(old, fp['hunks'])
            (fx if rel.startswith('tests/fixtures/') else py)[rel] = new
        py = {k: v for k, v in py.items() if k.endswith('.py') and k.startswith('sqlglot/')}
        c0 = time.process_time()
        w0 = time.time()
        r = xrun.run_tests(py, fx, cheap, timeout=120) if not os.environ.get('NOPK') else xrun.start(py, fx, cheap, 120, False, False).result()
        cpu = time.process_time() - c0 + sum(os.times()[2:4]) * 0
        if r is None:
            print(cid, 'RUNNER FAILED')
            bad += 1
            continue
        failing_truth = {by_num[n]['id'] for n in ans[cid]['failing']}
        expect = failing_truth & set(cheap)
        got = set(r['failed'])
        for secs, tid in r.get('prof', [])[:14]: print('      prof %.2fs %s' % (secs, tid))
        status = 'OK ' if got == expect else 'DIFF'
        ok += got == expect
        bad += got != expect
        print(f'{cid} {status} cand={len(ids)} ran={r["ran"]} cheap={len(cheap)} wall={time.time() - w0:.2f}s '
              f'failed={len(got)} expect={len(expect)} fx_used={len(r["fx_used"])}/{len(fx)}',
              '' if got == expect else f'extra={sorted(got - expect)[:3]} missing={sorted(expect - got)[:3]}')
    t = os.times()
    print(f'matches {ok} diffs {bad}; process cpu user={t[0]:.2f} sys={t[1]:.2f} children user={t[2]:.2f} sys={t[3]:.2f}')


if __name__ == '__main__':
    main()
