#!/usr/bin/env python3
"""Download exactly the allowed pinned SQLGlot source tree at Docker build."""
import shutil
import tarfile
import urllib.request
from pathlib import Path

COMMIT='eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
URL=f'https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}'
archive=Path('/tmp/sqlglot-source.tar.gz')
with urllib.request.urlopen(URL, timeout=180) as response, archive.open('wb') as output:
    shutil.copyfileobj(response, output)
with tarfile.open(archive, 'r:gz') as tf:
    tf.extractall('/app', filter='data')
Path(f'/app/sqlglot-{COMMIT}').rename('/app/sqlglot')
archive.unlink()
print(f'Fetched pinned SQLGlot at {COMMIT}', flush=True)
