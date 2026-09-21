"""Local dev runner. Patches the hardcoded /app/cache paths in config to
point at a writable directory under the project, so the app boots on
macOS/Linux without needing the container's /app filesystem layout.

Usage:  PRESET_ENABLED=true .venv/bin/python run-local.py
"""
import os
import config

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_CACHE = os.path.join(ROOT, ".local-cache")
os.makedirs(LOCAL_CACHE, exist_ok=True)

config.DB_PATH               = os.path.join(LOCAL_CACHE, "cache.db")
config.TMDB_POSTER_CACHE_DIR = os.path.join(LOCAL_CACHE, "tmdb_posters")
config.TMDB_LOGO_CACHE_DIR   = os.path.join(LOCAL_CACHE, "tmdb_logos")
config.COMPOSITE_BLOB_DIR    = os.path.join(LOCAL_CACHE, "composites")
config.BADGE_DIR             = os.path.join(ROOT, "badges")

os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", os.path.join(LOCAL_CACHE, "prom"))
_prom_dir = os.environ["PROMETHEUS_MULTIPROC_DIR"]
os.makedirs(_prom_dir, exist_ok=True)
# Mirror the container entrypoint: clear stale *.db files from prior runs
# so /metrics doesn't aggregate counters from dead processes.
for _f in os.listdir(_prom_dir):
    if _f.endswith(".db"):
        os.remove(os.path.join(_prom_dir, _f))

import uvicorn
uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False, workers=1)
