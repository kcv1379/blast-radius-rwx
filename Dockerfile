FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app
COPY fetch_source.py /app/
# Fixed, official SQLGlot commit; no mutable repository or unrelated downloads.
RUN python /app/fetch_source.py
# pytest and data packages are BUILD-ONLY. Runtime uses Python's standard library.
RUN pip install --no-cache-dir --disable-pip-version-check \
      'pytest==9.2.0' 'coverage==7.13.3' 'pytest-timeout==2.4.0' \
      'numpy==2.5.5' 'pandas==3.1.1' 'duckdb==2.0.0' \
 && SETUPTOOLS_SCM_PRETEND_VERSION=27.0.0 pip install --no-cache-dir --disable-pip-version-check -e /app/sqlglot

COPY build_tests.json build_coverage.py /app/
RUN python /app/build_coverage.py
COPY selector.py /app/selector.py
# The generated coverage map stays; the 50+ MB checkout isn't needed to select.
RUN rm -rf /app/sqlglot /app/build_tests.json /app/build_coverage.py /app/fetch_source.py /tmp/build_coverage.sqlite
CMD ["python", "/app/selector.py"]
