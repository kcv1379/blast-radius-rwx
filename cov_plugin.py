"""pytest plugin (build time only): per-test coverage facts about the pinned sqlglot checkout.

Loaded in every xdist worker with `-p cov_plugin`. Each worker writes, into BR_COV_DIR:
  cov.<worker>        coverage.py data with one dynamic context per test (line level)
  facts.<worker>.json  per test: fixture files read, classes instantiated (+ their MROs)
Every module of the package is imported first, under the `_import` context, so module-level
code is never attributed to whichever test happened to trigger a lazy import.
"""
import importlib
import json
import os
import pkgutil
import sys
import traceback

import coverage

PACKAGE_DIR = os.environ['BR_PACKAGE_DIR']
OUT_DIR = os.environ['BR_COV_DIR']
IMPORT_CTX = '_import'
OUTSIDE_CTX = '_outside'

_cov = None
_worker = 'main'
_ctx = None            # current test node id (None outside tests)
_phase = None          # 'setup' | 'call' | 'teardown' | 'collect'
_collect_module = None
_class_of = {}         # node id -> 'file::Class' prefix
_fixture_reads = {}    # key -> set(path); key = node id, or 'class:<file::Class>' / 'module:<file>'
_types_seen = {}       # node id -> set(type name)
_mro = {}              # type name -> [names of sqlglot ancestors]
_type_names = {}       # type -> name
_hook_installed = False


def _tname(t):
    n = _type_names.get(t)
    if n is None:
        n = f'{t.__module__}:{t.__qualname__}'
        _type_names[t] = n
        if n not in _mro:
            _mro[n] = [f'{c.__module__}:{c.__qualname__}' for c in t.mro()
                       if c.__module__.startswith('sqlglot')]
    return n


def _record_type(self):
    if _ctx is not None:
        _types_seen.setdefault(_ctx, set()).add(_tname(type(self)))


def _wrap_init(cls):
    orig = cls.__init__

    def __init__(self, *a, **k):
        _record_type(self)
        orig(self, *a, **k)
    __init__.__wrapped__ = orig
    cls.__init__ = __init__


def _install_instrumentation():
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer
    for cls in (Dialect, Expr, Generator, Parser, Tokenizer):
        _wrap_init(cls)


def _audit(event, args):
    if event != 'open' or _phase is None:
        return
    path = args[0]
    if not isinstance(path, str):
        return
    p = path.replace(chr(92), '/')
    i = p.find('tests/fixtures/')
    if i < 0:
        return
    rel = p[i:]
    if _phase == 'collect' and _collect_module:
        key = f'module:{_collect_module}'
    elif _phase == 'setup' and _ctx in _class_of:
        key = f'class:{_class_of[_ctx]}'
    elif _ctx is not None:
        key = _ctx
    else:
        return
    _fixture_reads.setdefault(key, set()).add(rel)


def _preimport():
    import sqlglot  # noqa: F401
    failed = []
    for info in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.', onerror=failed.append):
        if info.name.endswith('__main__'):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException:  # noqa: BLE001
            failed.append(info.name)
            traceback.print_exc()
    if failed:
        print(f'cov_plugin: modules failed to import: {failed}', file=sys.stderr)


def pytest_sessionstart(session):
    global _cov, _worker, _hook_installed
    config = session.config
    is_worker = hasattr(config, 'workerinput')
    if not is_worker and getattr(config.option, 'numprocesses', None):
        return  # xdist controller: it does not run tests
    _worker = config.workerinput['workerid'] if is_worker else 'main'
    os.makedirs(OUT_DIR, exist_ok=True)
    _cov = coverage.Coverage(
        source=[PACKAGE_DIR], data_file=os.path.join(OUT_DIR, f'cov.{_worker}'),
        branch=False, config_file=False,
    )
    _cov.start()
    _cov.switch_context(IMPORT_CTX)
    _preimport()
    _install_instrumentation()
    _patch_pool()
    _cov.switch_context(OUTSIDE_CTX)
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True


def pytest_collectstart(collector):
    global _phase, _collect_module
    if _cov is not None and type(collector).__name__ == 'Module':
        _phase, _collect_module = 'collect', collector.nodeid


def pytest_collectreport(report):
    global _phase, _collect_module
    if _cov is not None and _phase == 'collect':
        _phase, _collect_module = None, None


def pytest_runtest_protocol(item, nextitem):
    global _ctx
    if _cov is not None:
        _ctx = item.nodeid
        parts = item.nodeid.split('::')
        _class_of[_ctx] = '::'.join(parts[:2]) if len(parts) >= 3 else parts[0]
        _cov.switch_context(item.nodeid)


def pytest_runtest_setup(item):
    global _phase
    _phase = 'setup'


def pytest_runtest_call(item):
    global _phase
    _phase = 'call'


def pytest_runtest_teardown(item):
    global _phase
    _phase = 'teardown'


def pytest_runtest_logfinish(nodeid, location):
    global _ctx, _phase
    if _cov is not None:
        _cov.switch_context(OUTSIDE_CTX)
        _ctx, _phase = None, None


def pytest_sessionfinish(session, exitstatus):
    global _cov
    if _cov is not None:
        _cov.stop()
        _cov.save()
        with open(os.path.join(OUT_DIR, f'facts.{_worker}.json'), 'w') as f:
            json.dump({'fixtures': {k: sorted(v) for k, v in _fixture_reads.items()},
                       'types': {k: sorted(v) for k, v in _types_seen.items()},
                       'mro': _mro}, f)
        _cov = None


# ---- pool-worker support -------------------------------------------------------------------
# TestOptimizer and the executor TPC tests do their real work in spawned ProcessPoolExecutor
# workers. Each task is tagged with the submitting test's id; the worker measures coverage
# and class usage under that context and saves its own data files at exit.

_child_cov = None


def _child_finish():
    global _child_cov
    if _child_cov is None:
        return
    c, _child_cov = _child_cov, None
    c.stop()
    c.save()
    with open(os.path.join(OUT_DIR, f'facts.child{os.getpid()}.json'), 'w') as f:
        json.dump({'fixtures': {k: sorted(v) for k, v in _fixture_reads.items()},
                   'types': {k: sorted(v) for k, v in _types_seen.items()},
                   'mro': _mro}, f)


def _child_start():
    global _child_cov
    import atexit
    import multiprocessing.util
    _child_cov = coverage.Coverage(
        source=[PACKAGE_DIR], data_file=os.path.join(OUT_DIR, f'cov.child{os.getpid()}'),
        branch=False, config_file=False,
    )
    _child_cov.start()
    _child_cov.switch_context(OUTSIDE_CTX)
    _install_instrumentation()
    atexit.register(_child_finish)
    multiprocessing.util.Finalize(None, _child_finish, exitpriority=-100)


def _enter_ctx(ctx):
    """Runs while a worker UNPICKLES a task (before the task body): attribute everything from here
    on - argument unpickling, the call, and pickling the result - to the submitting test."""
    global _ctx
    if _child_cov is None:
        os.makedirs(OUT_DIR, exist_ok=True)
        _child_start()
    _child_cov.switch_context(ctx)
    _ctx = ctx


class _Marker:
    def __init__(self, ctx):
        self.ctx = ctx

    def __reduce__(self):
        return (_enter_ctx, (self.ctx,))


def _ctx_call(marker, fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _patch_pool():
    from concurrent.futures import ProcessPoolExecutor
    orig = ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        return orig(self, _ctx_call, _Marker(_ctx), fn, *args, **kwargs)
    ProcessPoolExecutor.submit = submit
