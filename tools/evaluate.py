#!/usr/bin/env python3
"""Score selector output against labels with the official formula (SCORING.md).

usage: evaluate.py OUTPUT.jsonl TESTS_JSON LABEL_DIR [--meta _meta.json] [--answers answers.json] [-v]
Labels: LABEL_DIR/<commit>.json with {"failing": [test ids]} (lab labeler) or an official
answers.json ({"commits": {id: {"failing": [numbers]}}}).
"""
import argparse
import json
import math
from pathlib import Path


def ms(sec):
    return math.floor(sec * 1000 + 0.5)


def cost_of(workers, dur):
    ws = [w for w in workers if w]
    if not ws:
        return 0
    return max(sum(dur[n] for n in w) for w in ws) + 2000 * len(ws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('output')
    ap.add_argument('tests')
    ap.add_argument('labels', nargs='?')
    ap.add_argument('--meta')
    ap.add_argument('--answers')
    ap.add_argument('-v', action='store_true')
    a = ap.parse_args()
    tests = json.loads(Path(a.tests).read_text())['tests']
    dur = {t['number']: ms(t['seconds']) for t in tests}
    by_id = {t['id']: t['number'] for t in tests}
    name = {t['number']: t['id'].split('::', 1)[1] for t in tests}
    meta = json.loads(Path(a.meta).read_text()) if a.meta else {}
    truth = {}
    if a.answers:
        for c, v in json.loads(Path(a.answers).read_text())['commits'].items():
            truth[c] = set(v['failing'])
            meta.setdefault(c, {})['kind'] = v.get('kind')
    elif a.labels:
        for p in Path(a.labels).glob('*.json'):
            r = json.loads(p.read_text())
            if 'error' in r:
                continue
            truth[p.stem] = {by_id[i] for i in r['failing'] if i in by_id}
    out = {}
    for line in Path(a.output).read_text().splitlines():
        if line.strip():
            j = json.loads(line)
            out[j['commit']] = j['workers']
    total = 0
    misses = []
    per_kind = {}
    oracle = 0
    n = 0
    for c in sorted(truth):
        if c not in out:
            print('MISSING commit in output:', c)
            continue
        n += 1
        workers = out[c]
        sel = {x for w in workers for x in w}
        cost = cost_of(workers, dur)
        miss = truth[c] - sel
        total += cost + (300000 if miss else 0)
        k = meta.get(c, {}).get('kind', '?')
        d = per_kind.setdefault(k, [0, 0, 0])
        d[0] += 1
        d[1] += cost
        d[2] += 1 if miss else 0
        if truth[c]:
            srt = sorted(truth[c], key=lambda x: -dur[x])
            best = 10 ** 12
            for kk in range(1, min(8, len(srt)) + 1):
                loads = [0] * kk
                for x in srt:
                    loads[loads.index(min(loads))] += dur[x]
                best = min(best, max(loads) + 2000 * kk)
            oracle += best
        if miss:
            misses.append((c, sorted(miss), len(sel), len(truth[c])))
        if a.v:
            print(f'{c} kind={k} sel={len(sel)} cost={cost / 1000:.1f}s failing={len(truth[c])} miss={len(miss)}')
    print(f'commits {n}  simulated {total / 1000:.3f}s  misses {len(misses)}  '
          f'(time without penalties {(total - 300000 * len(misses)) / 1000:.3f}s; oracle {oracle / 1000:.3f}s)')
    for k, (cnt, cst, ms_) in sorted(per_kind.items()):
        print(f'  kind {k}: {cnt} commits, {cst / 1000:.1f}s, {ms_} missed')
    for c, miss, nsel, nf in misses:
        print(f'  MISS {c}: selected {nsel}, failing {nf}, missing {[name[x] for x in miss][:5]} {meta.get(c, {}).get("desc", "")}')


if __name__ == '__main__':
    main()
