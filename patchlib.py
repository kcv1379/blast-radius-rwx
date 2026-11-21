"""Minimal unified-diff parser/applier (stdlib only) shared by the lab tools and the selector."""
import re

HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse(text):
    """Return [{'old': path|None, 'new': path|None, 'hunks': [(old_start, old_len, lines)]}]
    where lines are raw hunk lines (with leading ' ', '+', '-')."""
    files = []
    cur = None
    hunk = None
    for line in text.split('\n'):
        if line.startswith('diff --git '):
            cur = {'old': None, 'new': None, 'hunks': []}
            files.append(cur)
            hunk = None
        elif cur is not None and hunk is None and line.startswith('--- '):
            cur['old'] = None if line[4:].startswith('/dev/null') else re.sub(r'^a/', '', line[4:].split('\t')[0])
        elif cur is not None and hunk is None and line.startswith('+++ '):
            cur['new'] = None if line[4:].startswith('/dev/null') else re.sub(r'^b/', '', line[4:].split('\t')[0])
        elif cur is not None and line.startswith('@@'):
            m = HUNK.match(line)
            if m:
                hunk = (int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1), [])
                cur['hunks'].append(hunk)
        elif hunk is not None:
            if line.startswith(('+', '-', ' ')):
                hunk[2].append(line)
            elif line[:1] == chr(92):
                continue
            elif line == '':
                hunk = None  # end of patch text (blank context lines keep their leading space)
                continue
            else:
                hunk = None
    return files


def apply_hunks(old_lines, hunks):
    """Apply hunks to a list of lines (no newlines). Raises ValueError if context does not match."""
    out = []
    pos = 0  # index into old_lines
    for old_start, old_len, lines in hunks:
        start = old_start - 1 if old_len else old_start
        if start < pos:
            raise ValueError('overlapping hunks')
        out.extend(old_lines[pos:start])
        pos = start
        for l in lines:
            tag, body = l[0], l[1:]
            if tag == ' ':
                if pos >= len(old_lines) or old_lines[pos] != body:
                    raise ValueError(f'context mismatch at old line {pos + 1}')
                out.append(body)
                pos += 1
            elif tag == '-':
                if pos >= len(old_lines) or old_lines[pos] != body:
                    raise ValueError(f'delete mismatch at old line {pos + 1}')
                pos += 1
            elif tag == '+':
                out.append(body)
    out.extend(old_lines[pos:])
    return out


def apply_to_text(old_text, hunks):
    ends_nl = old_text.endswith('\n')
    old_lines = old_text.split('\n')
    if ends_nl:
        old_lines.pop()
    new = apply_hunks(old_lines, hunks)
    return '\n'.join(new) + ('\n' if (ends_nl or not old_lines) else '')
