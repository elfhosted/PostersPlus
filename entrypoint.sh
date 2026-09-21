#!/bin/sh
set -e

# ElfHosted fork: enable prometheus_client multiprocess mode so /metrics
# aggregates counters/histograms across all uvicorn worker processes. The
# directory must exist, be writable by the runtime uid (568), and be empty at
# startup — stale *.db files from a previous run would survive a restart and
# inflate counters.
export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/postersplus-prom}"
mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db

# No gosu / privilege drop — the image already runs as uid 568 (see dockerfile;
# on Kubernetes the deployment SecurityContext owns user + volume policy).
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS:-1}"
