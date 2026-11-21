"""Deterministic fingerprints of sqlglot's import-time state (class attributes, module constants).

snapshot() -> {module: {key: hash}} for every imported sqlglot module. Keys are 'name' for module
globals and 'Class.attr' for class attributes (nested classes use their qualified name). Functions
are skipped on purpose: their bodies are covered by line coverage; this catches what executes while
modules and classes are created (dict/flag edits, tries and mappings derived at import, base classes).
"""
import enum
import hashlib
import logging
import re
import sys
import types

_MAX_DEPTH = 6
_SKIP_FIELDS = {'_hash', 'parent', '_parent'}
_SKIP_ATTRS = {'__module__', '__qualname__', '__dict__', '__weakref__', '__doc__', '__annotations__',
               '__firstlineno__', '__static_attributes__', '__orig_bases__', '__parameters__',
               '__abstractmethods__', '_abc_impl', '__slotnames__', '__hash__', '__classcell__',
               '__type_params__'}


def _enc(o, depth, out, seen):
    if o is None or isinstance(o, (bool, int, float, str, bytes)):
        out.append(type(o).__name__[0] + repr(o))
    elif isinstance(o, enum.Enum):
        out.append('E' + type(o).__qualname__ + '.' + o.name)
    elif isinstance(o, type):
        out.append('C' + o.__module__ + '.' + o.__qualname__)
    elif isinstance(o, logging.Logger):
        out.append('LOGGER')
    elif isinstance(o, re.Pattern):
        out.append('R' + o.pattern + str(o.flags))
    elif isinstance(o, (types.FunctionType, types.BuiltinFunctionType, types.MethodType, staticmethod,
                        classmethod, property, types.ModuleType)):
        out.append('F' + getattr(o, '__qualname__', type(o).__name__))
    elif depth >= _MAX_DEPTH or id(o) in seen:
        out.append('~' + type(o).__name__)
    elif isinstance(o, (list, tuple)):
        seen = seen | {id(o)}
        out.append(('[' if isinstance(o, list) else '(') + str(len(o)))
        for x in o:
            _enc(x, depth + 1, out, seen)
        out.append(']')
    elif isinstance(o, (set, frozenset)):
        parts = []
        for x in o:
            sub = []
            _enc(x, depth + 1, sub, seen)
            parts.append(''.join(sub))
        out.append('{S' + '|'.join(sorted(parts)) + '}')
    elif isinstance(o, dict):
        seen = seen | {id(o)}
        out.append('{D' + str(len(o)))
        for k, v in o.items():
            _enc(k, depth + 1, out, seen)
            out.append(':')
            _enc(v, depth + 1, out, seen)
        out.append('}')
    else:
        out.append('O' + type(o).__qualname__)
        d = getattr(o, '__dict__', None)
        if isinstance(d, dict) and depth < 3:
            seen = seen | {id(o)}
            for k in sorted(d):
                if k in _SKIP_FIELDS:
                    continue
                out.append(k + '=')
                _enc(d[k], depth + 1, out, seen)


def fp(o):
    out = []
    try:
        _enc(o, 0, out, frozenset())
    except Exception:  # noqa: BLE001
        return 'ERR'
    return hashlib.blake2b(''.join(out).encode('utf-8', 'replace'), digest_size=6).hexdigest()


def _class_entries(cls, prefix, mod_name, d, depth=0):
    d[prefix + '.__bases__'] = fp(tuple(b.__qualname__ for b in cls.__bases__))
    for k, v in vars(cls).items():
        if k in _SKIP_ATTRS:
            continue
        if isinstance(v, type) and v.__module__ == mod_name and v.__qualname__.startswith(cls.__qualname__ + '.') and depth < 4:
            _class_entries(v, f'{prefix}.{k}', mod_name, d, depth + 1)
        elif isinstance(v, (types.FunctionType, staticmethod, classmethod, property)):
            continue
        else:
            d[f'{prefix}.{k}'] = fp(v)


def snapshot():
    out = {}
    for name, mod in list(sys.modules.items()):
        if not (name == 'sqlglot' or name.startswith('sqlglot.')) or mod is None:
            continue
        d = {}
        for k, v in list(vars(mod).items()):
            if k.startswith('__') or isinstance(v, types.ModuleType):
                continue
            if isinstance(v, type):
                if v.__module__ == name and '.' not in v.__qualname__:
                    _class_entries(v, k, name, d)
                else:
                    d[k] = fp(v)  # imported / aliased class
            elif isinstance(v, (types.FunctionType, types.BuiltinFunctionType)):
                d[k] = 'F'
            else:
                d[k] = fp(v)
        out[name] = d
    return out


def diff(base, new):
    """Changed / added / removed keys: [(module, key)]"""
    changes = []
    for mod in set(base) | set(new):
        b, n = base.get(mod), new.get(mod)
        if b is None or n is None:
            changes.append((mod, '*'))
            continue
        for k in set(b) | set(n):
            if b.get(k) != n.get(k):
                changes.append((mod, k))
    return changes


def diff_stable(stable, snap):
    """Compare a fresh snapshot with the stable baseline: [(module, key)], '*' = module missing."""
    changes = []
    for mod, keys in stable.items():
        cur = snap.get(mod)
        if cur is None:
            changes.append((mod, '*'))
            continue
        for k, h in keys.items():
            if cur.get(k) != h:
                changes.append((mod, k))
    for mod, cur in snap.items():
        keys = stable.get(mod)
        if keys is None:
            continue
        for k in cur:
            if k not in keys and k not in _UNSTABLE.get(mod, ()):
                changes.append((mod, k))
    return changes


_UNSTABLE = {}


def import_all():
    """Import every module of the package; returns {module: error text} for the ones that fail."""
    import importlib
    import pkgutil
    errors = {}
    import sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):
        if info.name.endswith('__main__'):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as e:  # noqa: BLE001
            errors[info.name] = type(e).__name__
    return errors


if __name__ == '__main__':
    import json
    import os
    sys.path.insert(0, os.environ.get('BR_SRC', '/app/sqlglot'))
    sys.dont_write_bytecode = True
    errs = import_all()
    json.dump({'snap': snapshot(), 'errors': errs}, open(sys.argv[1], 'w'), separators=(',', ':'))
