# Lab image: the organisers' exact base image + their requirements.lock + pinned sqlglot.
# Used only for offline measurement (labeling mutants, coverage experiments). Not the submission.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /lab
COPY requirements.lock /lab/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.lock 'coverage==7.13.3'
COPY tools/lab/fetch_source.py /lab/fetch_source.py
RUN python /lab/fetch_source.py && mv /app/sqlglot /lab/sqlglot && python -m compileall -q /lab/sqlglot
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
