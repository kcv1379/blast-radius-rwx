import json, os, sys
from pathlib import Path
sys.path.insert(0, 'C:/Users/player/br-work/dev')
os.environ['BR_MAP_DIR'] = sys.argv[1]
import selector, patchlib
imp = selector.Impact()
K_STMT, K_HDR, K_DEF, K_CLASS, K_LAM = 0, 1, 2, 3, 4
patches, labels = Path(sys.argv[2]), Path(sys.argv[3])
rows = {}
for lp in sorted(labels.glob('*.json')):
    lab = json.loads(lp.read_text())
    if 'error' in lab: continue
    text = (patches / f'{lp.stem}.patch').read_text()
    cat = set()
    for fp in patchlib.parse(text):
        rel = fp['new'] or fp['old']
        if not (rel.startswith('sqlglot/') and rel.endswith('.py')): cat.add('nonpy'); continue
        rec = imp.file(rel)
        if rec is None: cat.add('unmapped'); continue
        ilines = set(rec['i'])
        for b in selector.blocks_of(fp):
            if all(selector.is_comment_or_blank(t) for t in b['dtext'] + b['adds']): cat.add('comment'); continue
            for ln in (b['dels'] or [b['pos']]):
                us = selector.units_at(rec, ln)
                if any(u[3] and u[2] in (K_STMT, K_HDR, K_CLASS) for u in us) and not any(u[2] == K_LAM and u[0] < ln < u[1] for u in us):
                    cat.add('import-stmt')
                elif ln in ilines: cat.add('body-at-import')
                else: cat.add('body')
    rows[lp.stem] = (cat, len(lab['failing']))
from collections import Counter
c = Counter(); f = Counter()
for k, (cat, n) in rows.items():
    key = '+'.join(sorted(cat)); c[key] += 1; f[key] += (n > 0)
for key, v in c.most_common(): print(f'{key:30} n={v:3} with-failures={f[key]}')
