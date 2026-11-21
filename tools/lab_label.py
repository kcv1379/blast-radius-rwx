#!/usr/bin/env python3
"""Runs INSIDE the lab container: label patches by running the real tests on a patched copy.

usage: lab_label.py PATCH_DIR OUT_DIR [--jobs 8] [--only a,b] [--skip-heavy-unless-covered FILE_COV]
Result per patch: OUT_DIR/<id>.json {"failing": [test ids], "collect_errors": [...], "seconds": s}
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchlib  # noqa: E402

SRC = Path('/lab/sqlglot')
HEAVY = [
    'tests/test_parser.py::TestParser::test_max_nodes',
    'tests/test_executor.py::TestExecutor::test_execute_tpcds',
    'tests/test_executor.py::TestExecutor::test_execute_tpch',
    'tests/test_executor.py::TestExecutor::test_optimized_tpch',
    'tests/test_generator.py::TestGenerator::test_generator_memory',
]
IMPACT = None
CANDS = None


def heavy_deselect(text):
    """Deselect a stress test when the patch cannot touch anything it executes (sound for edits
    inside function bodies; verified against full-suite runs on a sample)."""
    global IMPACT
    if IMPACT is None:
        import selector
        IMPACT = selector.Impact()
    imp = IMPACT
    skip = []
    for h in HEAVY:
        if h not in imp.ids:
            continue
        bit = 1 << imp.ids.index(h)
        needed = False
        for fp in patchlib.parse(text):
            rel = fp['new'] or fp['old']
            if rel.startswith('tests/fixtures/'):
                i = imp.fixtures.get(rel)
                needed |= i is None or bool(imp.mask(i) & bit)
            elif rel.startswith('tests/') or not rel.endswith('.py'):
                needed = True
            else:
                rec = imp.file(rel)
                needed |= rec is None or bool(imp.mask(rec['m']) & bit)
        if not needed:
            skip += ['--deselect', h]
    return skip


def apply_patch(text, dst):
    for fp in patchlib.parse(text):
        path = fp['new'] or fp['old']
        target = dst / path
        old = target.read_text(encoding='utf-8') if fp['old'] and target.exists() else ''
        new = patchlib.apply_to_text(old, fp['hunks'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding='utf-8', newline='')


def label(pid, text, out_dir, jobs, timeout_s):
    work = Path('/tmp/lbl') / pid
    if work.exists():
        shutil.rmtree(work)
    t0 = time.time()
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns('__pycache__', '.git', 'benchmarks'))
    try:
        apply_patch(text, work)
    except Exception as e:  # noqa: BLE001
        return {'error': f'apply failed: {e}'}
    res_file = work / 'res.json'
    env = dict(os.environ, BR_LABEL_OUT=str(res_file), PYTHONPATH=f'{work}:/dev_tools', PYTHONDONTWRITEBYTECODE='1')
    touched = [fp['new'] or fp['old'] for fp in patchlib.parse(text)]
    cand_ids = CANDS.get(pid) if CANDS else None
    only_tests = all(t.startswith('tests/') and t.rsplit('/', 1)[-1].startswith('test_') and t.endswith('.py')
                     for t in touched)
    targets = touched if only_tests else ['tests']
    if cand_ids is not None:
        targets = cand_ids
    cmd = [sys.executable, '-m', 'pytest', '-n', str(jobs), '-p', 'label_plugin', '-p', 'no:cacheprovider',
           f'--timeout={timeout_s}', '-q', '--tb=no'] + targets + (heavy_deselect(text) if os.environ.get('BR_MAP_DIR') and not only_tests and cand_ids is None else [])
    try:
        subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {'error': 'suite timeout'}
    if not res_file.exists():
        return {'error': 'no result file'}
    res = json.loads(res_file.read_text())
    out = {'failing': res['failed'], 'collect_errors': res['collect_errors'], 'seconds': round(time.time() - t0, 1)}
    shutil.rmtree(work, ignore_errors=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('patches')
    ap.add_argument('out')
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--only', default='')
    ap.add_argument('--timeout', type=int, default=120)
    ap.add_argument('--parallel', type=int, default=1)
    ap.add_argument('--cands', default='', help='json {patch: [node ids]}: run only those tests')
    a = ap.parse_args()
    global CANDS
    if a.cands:
        CANDS = json.loads(Path(a.cands).read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    patches = sorted(Path(a.patches).glob('*.patch'))
    if a.only:
        keep = set(a.only.split(','))
        patches = [p for p in patches if p.stem in keep]
    todo = [p for p in patches if not (out / f'{p.stem}.json').exists()]

    def work(p):
        res = label(p.stem, p.read_text(encoding='utf-8'), out, a.jobs, a.timeout)
        (out / f'{p.stem}.json').write_text(json.dumps(res))
        print(p.stem, len(res.get('failing', [])), res.get('seconds'), res.get('error', ''), flush=True)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=a.parallel) as ex:
        list(ex.map(work, todo))


if __name__ == '__main__':
    main()
