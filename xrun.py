"""Run selected sqlglot tests against patched sources (stdlib only), in a forked child.

run_tests(patched_py, patched_fixtures, node_ids, timeout) ->
    {'failed': [node ids], 'ran': n, 'unused_fixtures': [...]}  or None when the child died/timed out.
Patched python modules are served by an import hook (nothing is written to disk); patched
fixtures by a wrapper around open(). ProcessPoolExecutor is replaced by an in-process executor
that still round-trips arguments and results through pickle (sqlglot pickles via serde).
"""
import builtins
import importlib
import importlib.abc
import importlib.util
import io
import json
import os
import pickle
import select
import signal
import sys
import time
import unittest
from concurrent.futures import Executor, Future

ROOT = os.environ.get('BR_SRC', '/app/sqlglot')
_PRELOADED = False
_DO_PICKLE = True


def preload():
    """Import the third-party libraries every executor/optimizer test module needs, once, so that
    forked children share them. Must not import sqlglot or the tests."""
    global _PRELOADED
    if _PRELOADED:
        return
    _PRELOADED = True
    for var in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ.setdefault(var, '1')
    for name in ('numpy', 'pandas', 'duckdb', 'dateutil', 'pytz'):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001
            pass


class _Loader(importlib.abc.Loader):
    def __init__(self, path, src):
        self.path, self.src = path, src

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__br_patched__ = True
        exec(compile(self.src, self.path, 'exec', dont_inherit=True), module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, files):
        self.by_name = {}
        for rel, src in files.items():
            parts = rel[:-3].split('/')
            pkg = parts[-1] == '__init__'
            if pkg:
                parts.pop()
            self.by_name['.'.join(parts)] = (os.path.join(ROOT, rel), src, pkg)

    def find_spec(self, name, path=None, target=None):
        ent = self.by_name.get(name)
        if ent is None:
            return None
        p, src, pkg = ent
        spec = importlib.util.spec_from_loader(name, _Loader(p, src), origin=p, is_package=pkg)
        if pkg:
            spec.submodule_search_locations = [os.path.dirname(p)]
        spec.has_location = True
        return spec


class _SyncExecutor(Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        f = Future()
        try:
            if not _DO_PICKLE:
                f.set_result(fn(*args, **kwargs))
            else:
                fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                res = pickle.loads(pickle.dumps(fn2(*a2, **k2)))
                f.set_result(res)
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


class _Collector(unittest.TestResult):
    def __init__(self, ids_by_class):
        super().__init__()
        self.prof = []
        self._last = time.process_time()
        self.failed = set()
        self.ids_by_class = ids_by_class

    def _mark(self, test):
        tid = test.id()
        if tid in self.ids_by_class['__all__']:
            self.failed.add(tid)
            return
        # class/module level failure (setUpClass, setUpModule, import): fail everything it covers
        desc = getattr(test, 'description', '') or tid
        for prefix, ids in self.ids_by_class.items():
            if prefix != '__all__' and prefix in desc:
                self.failed.update(ids)
        if not any(p in desc for p in self.ids_by_class if p != '__all__'):
            self.failed.update(self.ids_by_class['__all__'])

    def stopTest(self, test):
        super().stopTest(test)
        now = time.process_time()
        self.prof.append((now - self._last, test.id()))
        self._last = now

    def addFailure(self, test, err):
        self._mark(test)
        if self.failfast:
            self.stop()

    def addError(self, test, err):
        self._mark(test)
        if self.failfast:
            self.stop()

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)
            if self.failfast:
                self.stop()


def _child_itsd(spec):
    import gzip
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.dont_write_bytecode = True
    if spec['py']:
        sys.meta_path.insert(0, _Finder(spec['py']))
    import fpr
    with gzip.open(spec['baseline'], 'rt') as f:
        base = json.load(f)
    fpr._UNSTABLE = base['unstable']
    errors = fpr.import_all()
    changes = fpr.diff_stable(base['stable'], fpr.snapshot())
    return {'changes': changes, 'errors': errors}


def _child(spec):
    if spec.get('kind') == 'itsd':
        return _child_itsd(spec)
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    sys.dont_write_bytecode = True
    os.environ['SKIP_INTEGRATION'] = '0'
    global _DO_PICKLE
    _DO_PICKLE = bool(spec.get('pickle', True))
    if spec['py']:
        sys.meta_path.insert(0, _Finder(spec['py']))
    import concurrent.futures as cf
    cf.ProcessPoolExecutor = _SyncExecutor
    try:
        import duckdb
        real_connect = duckdb.connect

        def connect(database=':memory:', *a, **k):
            cfg = dict(k.pop('config', None) or {})
            cfg.setdefault('threads', 1)
            return real_connect(database, *a, config=cfg, **k)
        duckdb.connect = connect
    except Exception:  # noqa: BLE001
        pass
    fx = spec['fx']
    used = set()
    if fx:
        real_open = builtins.open

        def _open(file, mode='r', *a, **k):
            if isinstance(file, (str, os.PathLike)):
                p = os.path.normpath(os.fspath(file)).replace(os.sep, '/')
                txt = fx.get(p)
                if txt is not None:
                    used.add(p)
                    return io.BytesIO(txt.encode('utf-8')) if 'b' in mode else io.StringIO(txt)
            return real_open(file, mode, *a, **k)
        builtins.open = _open
        io.open = _open
    groups, ids_by_class, uid2node = {}, {'__all__': set()}, {}
    for node in spec['ids']:
        file, cls, meth = node.split('::')
        groups.setdefault((file[:-3].replace('/', '.'), cls), []).append((meth, node))
    for (mod, cls), ms in groups.items():
        key = f'{mod}.{cls}'
        for meth, node in ms:
            uid = f'{key}.{meth}'
            uid2node[uid] = node
            ids_by_class['__all__'].add(uid)
            ids_by_class.setdefault(key, set()).add(uid)
    result = _Collector(ids_by_class)
    result.failfast = bool(spec.get('failfast'))
    for (mod, cls), ms in groups.items():
        try:
            klass = getattr(importlib.import_module(mod), cls)
            suite = unittest.TestSuite([klass(meth) for meth, _ in ms])
        except BaseException:  # noqa: BLE001
            result.failed.update(ids_by_class[f'{mod}.{cls}'])
            continue
        suite.run(result)
    return {'failed': sorted(uid2node[u] for u in result.failed if u in uid2node), 'ran': result.testsRun,
            'fx_used': sorted(used),
            'prof': sorted(result.prof, reverse=True)[:12] if os.environ.get('BR_PROF') else []}


class Job:
    def __init__(self, pid, rfd, deadline):
        self.pid, self.rfd, self.deadline, self.buf, self.done, self.out = pid, rfd, deadline, [], False, None
        self.cpu = 0.0

    def poll(self, block):
        """Read what is available; returns True when finished (result in self.out)."""
        while not self.done:
            left = self.deadline - time.monotonic()
            if left <= 0:
                self._finish(None)
                break
            ready, _, _ = select.select([self.rfd], [], [], left if block else 0)
            if not ready:
                if block:
                    continue
                return False
            chunk = os.read(self.rfd, 65536)
            if not chunk:
                try:
                    out = json.loads(b''.join(self.buf))
                except ValueError:
                    out = None
                self._finish(None if (out is None or 'error' in out) else out)
                break
            self.buf.append(chunk)
        return True

    def _finish(self, out):
        self.done, self.out = True, out
        if out is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            _, _, ru = os.wait4(self.pid, 0)
            self.cpu = ru.ru_utime + ru.ru_stime
        except OSError:
            pass
        os.close(self.rfd)

    def result(self):
        self.poll(True)
        return self.out


def start(patched_py, patched_fixtures, node_ids, timeout, failfast=False, pickle_roundtrip=True):
    """Fork a child that runs node_ids against the patched sources. Returns a Job (or None)."""
    preload()
    spec = {'py': patched_py,
            'fx': {os.path.join(ROOT, rel).replace(os.sep, '/'): txt for rel, txt in patched_fixtures.items()},
            'ids': list(node_ids), 'failfast': failfast, 'pickle': pickle_roundtrip}
    return _fork(spec, timeout)


def start_itsd(patched_py, baseline_path, timeout):
    """Fork a child that imports the patched package and diffs its import-time state against the baseline."""
    return _fork({'kind': 'itsd', 'py': patched_py, 'baseline': baseline_path}, timeout)


def _fork(spec, timeout):
    try:
        r, w = os.pipe()
        pid = os.fork()
    except OSError:
        return None
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            try:
                payload = json.dumps(_child(spec))
            except BaseException as e:  # noqa: BLE001
                payload = json.dumps({'error': repr(e)})
            data = payload.encode()
            while data:
                data = data[os.write(w, data):]
        finally:
            os._exit(0)
    os.close(w)
    return Job(pid, r, time.monotonic() + timeout)


def run_tests(patched_py, patched_fixtures, node_ids, timeout):
    job = start(patched_py, patched_fixtures, node_ids, timeout)
    return job.result() if job else None


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == 'calibrate':
    # python xrun.py calibrate BASELINE.json.gz : run the run-time ITSD child on the pristine tree
    # and print what it reports (those keys differ for environmental reasons only).
    job = start_itsd({}, sys.argv[2], 120)
    res = job.result() if job else None
    sys.stdout.write(json.dumps(res))
