"""/metrics shared-secret guard (ElfHosted fork).

The key may arrive as `Authorization: Bearer <key>` (so prometheus-operator can
read it from a Secret) or as `?access_key=<key>`; anything else is a 403, and an
unset METRICS_ACCESS_KEY leaves the endpoint open.
"""
import asyncio
import os
import tempfile
import unittest

import config

_TMP = tempfile.mkdtemp()
config.DB_PATH = os.path.join(_TMP, "c.db")
config.TMDB_POSTER_CACHE_DIR = os.path.join(_TMP, "p")
config.TMDB_LOGO_CACHE_DIR = os.path.join(_TMP, "l")
config.COMPOSITE_BLOB_DIR = os.path.join(_TMP, "comp")

import storage.sqlite_backend as sb
sb.DB_PATH = config.DB_PATH
sb.TMDB_POSTER_CACHE_DIR = config.TMDB_POSTER_CACHE_DIR
sb.TMDB_LOGO_CACHE_DIR = config.TMDB_LOGO_CACHE_DIR

import main
from fastapi import HTTPException
from starlette.requests import Request


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode("utf-8")) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/metrics", "headers": raw, "query_string": b""})


def _call(headers=None, access_key=""):
    return asyncio.run(main.metrics_endpoint(_request(headers), access_key=access_key))


class MetricsAuthTest(unittest.TestCase):
    def setUp(self):
        self._saved = main._cfg.METRICS_ACCESS_KEY
        main._cfg.METRICS_ACCESS_KEY = "s3cret"

    def tearDown(self):
        main._cfg.METRICS_ACCESS_KEY = self._saved

    def assertForbidden(self, **kw):
        with self.assertRaises(HTTPException) as ctx:
            _call(**kw)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_bearer_header_accepted(self):
        self.assertEqual(_call(headers={"Authorization": "Bearer s3cret"}).status_code, 200)

    def test_bearer_scheme_case_insensitive(self):
        self.assertEqual(_call(headers={"Authorization": "bearer s3cret"}).status_code, 200)

    def test_query_param_still_accepted(self):
        self.assertEqual(_call(access_key="s3cret").status_code, 200)

    def test_missing_key_forbidden(self):
        self.assertForbidden()

    def test_wrong_bearer_forbidden(self):
        self.assertForbidden(headers={"Authorization": "Bearer nope"})

    def test_non_bearer_scheme_ignored(self):
        self.assertForbidden(headers={"Authorization": "Basic s3cret"})

    def test_non_ascii_key_is_403_not_500(self):
        self.assertForbidden(headers={"Authorization": "Bearer s3crét"})
        self.assertForbidden(access_key="s3crét")

    def test_unset_key_leaves_endpoint_open(self):
        main._cfg.METRICS_ACCESS_KEY = ""
        self.assertEqual(_call().status_code, 200)


if __name__ == "__main__":
    unittest.main()
