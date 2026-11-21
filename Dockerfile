# The scorer builds this on linux/amd64 and runs it with no network. The base image is the
# one the organisers labeled every commit in (its Python and SQLite decide which tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app

# Pinned upstream sqlglot (the only source tree the impact map is built from).
COPY fetch_source.py /app/fetch_source.py
RUN python /app/fetch_source.py

# The organisers' package set, so the suite behaves exactly as when the commits were labeled.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock 'coverage==7.13.3'

# Build-time only (unscored): run the suite once with per-test coverage and write /app/map.
COPY cov_plugin.py build_coverage.py fpr.py xrun.py patchlib.py /app/
RUN BR_JOBS=4 python /app/build_coverage.py && rm -rf /tmp/brcov
# Byte-compile the pinned tree now: the run-time filesystem is read-only, so nothing can be cached later.
RUN python -m compileall -q /app/sqlglot

# Runtime: stdlib only.
COPY selector.py /app/
RUN python -m compileall -q /app/patchlib.py /app/selector.py /app/xrun.py /app/fpr.py
CMD ["python", "/app/selector.py"]
