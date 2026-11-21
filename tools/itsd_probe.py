"""usage: itsd_probe.py [patchfile]  -- snapshot import-time state (optionally with a patch applied)."""
import importlib
import json
import os
import pkgutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = '/lab/sqlglot'
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True
import patchlib  # noqa: E402
import xrun  # noqa: E402
import fpr  # noqa: E402

if len(sys.argv) > 1:
    text = open(sys.argv[1], encoding='utf-8').read()
    py = {}
    for fp_ in patchlib.parse(text):
        rel = fp_['new'] or fp_['old']
        if rel.startswith('sqlglot/') and rel.endswith('.py'):
            py[rel] = patchlib.apply_to_text(open(os.path.join(ROOT, rel), encoding='utf-8').read(), fp_['hunks'])
    sys.meta_path.insert(0, xrun._Finder(py))
t0 = time.process_time()
errors = {}
import sqlglot  # noqa: E402,F401
for info in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):
    if info.name.endswith('__main__'):
        continue
    try:
        importlib.import_module(info.name)
    except BaseException as e:  # noqa: BLE001
        errors[info.name] = repr(e)[:100]
t1 = time.process_time()
snap = fpr.snapshot()
t2 = time.process_time()
out = {'snap': snap, 'errors': errors}
json.dump(out, open('/tmp/snap_%s.json' % (os.environ.get('SNAP_NAME', 'x')), 'w'))
print('import cpu %.2fs snapshot cpu %.2fs modules %d entries %d errors %d' % (
    t1 - t0, t2 - t1, len(snap), sum(len(v) for v in snap.values()), len(errors)))
