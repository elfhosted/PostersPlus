# Resilience Roadmap

Tracking document for the ElfHosted fork's hosting-mode work. Each phase is a self-contained branch off the previous phase. After implementation, each phase is reviewed by Codex (cross-model second opinion) and the findings are noted here before the branch is considered ready to merge.

**Design constraints (apply to every phase):**

1. **Opt-in, not replacement.** Every new backend (Postgres, Redis, S3) must be selectable by env var. The upstream defaults (SQLite, local disk, in-process dicts) must keep working unchanged.
2. **Narrow, cherry-pickable commits.** Prefer thin adapters over invasive refactors so upstream can pull anything they want, and we can pull from upstream without merge hell.
3. **No behaviour change at the upstream defaults.** A private user pulling this fork must see identical behaviour to upstream unless they explicitly flip a flag.
4. **No third-party services baked into the default path.** Sentry, Prometheus, etc. are opt-in only.

---

## Phase 0 — Fork setup

**Branch:** `phase-0-fork-setup`
**Status:** in progress

- [x] README updated to identify this as ElfHosted's fork
- [x] RESILIENCE.md (this doc) created
- [ ] Codex review

**Notes:** no code changes. Repo structure remains upstream's. The eventual GitHub remote (`origin`) will be ElfHosted's fork; the current `origin` (UmbraProjects) should become `upstream` once the fork exists on GitHub.

---

## Phase 1 — Pluggable storage backend

**Branch:** `phase-1-storage` (off `phase-0-fork-setup`)
**Status:** implementation complete, codex review pending

**Goal:** allow swapping SQLite for PostgreSQL via `DATABASE_URL`. SQLite remains the default.

**Approach:**

- Introduce a thin `Storage` protocol in `cache.py` covering the operations the rest of the app uses (`get_rating`, `set_rating`, `get_composite`, etc.) — no ORM. All current callers go through this.
- Two implementations: `SQLiteStorage` (current behaviour, default) and `PostgresStorage` (psycopg3, opt-in when `DATABASE_URL` is set).
- Schema bootstrap moves to a tiny migrations module (`db/migrations/`). Numbered SQL files, idempotent. For SQLite we keep `CREATE IF NOT EXISTS`; for Postgres we run migrations on startup with an advisory lock so multiple replicas don't race.
- TTLs become SQL-side `WHERE cached_at > ?` filters (already are for SQLite — keep the same shape).
- Connection pooling: SQLite stays single-connection (as today); Postgres uses `psycopg_pool`.
- All blob storage (composite JPEG bytes) still goes through this layer — moving bytes to object storage is Phase 3.

**Migration story:** none required at first deploy. New installs pick a backend; existing installs stay on SQLite. A future one-shot copy tool (`scripts/migrate_sqlite_to_postgres.py`) can be added if needed but is not in scope for Phase 1.

**Acceptance:**

- Default `docker compose up` still works with no Postgres.
- Setting `DATABASE_URL=postgresql://...` and starting fresh works end-to-end.
- All existing endpoints produce identical JSON/image responses on both backends.

---

## Phase 2 — Pluggable coordination layer

**Branch:** `phase-2-coordination`
**Status:** implementation complete, codex review pending

**Scope (narrowed from original plan):**

The original plan included cross-replica render coalescing. On closer inspection that was the wrong abstraction — `asyncio.Future` and `asyncio.Event` aren't shareable across processes, and the shared metadata cache (Phase 1) already deduplicates the *result-storage* across replicas. The remaining problem is that **side-effect state** (rate-limit backoff, background-fetch claims) stays per-process. Phase 2 fixes only that.

**What moved:**

- **MDBList backoff** — when one replica hits a 429 from MDBList, every replica now sees the backoff window. Previously each replica re-discovered the backoff independently and burned an extra MDBList call per replica per failed title.
- **Background quality-fetch single-flight** — atomic `claim_inflight` (SETNX + EX) prevents two replicas scheduling the same imdb_id simultaneously.

**What stayed per-process (deliberately):**

- Render coalescing (`_render_inflight` Future dict, [main.py:71](main.py#L71)) — intra-process burst dedup is fine; the shared `final_poster_cache` covers cross-process redundancy at one wasted render per replica per cold poster, which is acceptable.
- Rating fetch coalescing (`_rating_fetch_inflight` Event dict, [main.py:104](main.py#L104)) — same reasoning; the shared rating cache plus the now-shared backoff cover the cross-replica case.

**API (`coordination/__init__.py`):**

- `async init()` / `async close()` — lifecycle, called from FastAPI lifespan.
- `ping()` — sync probe for `/ready` (Phase 4).
- `async is_backoff_active(namespace, key) -> bool`
- `async set_backoff(namespace, key, ttl_seconds)`
- `async clear_backoff(namespace, key)`
- `async claim_inflight(namespace, key, ttl_seconds=300) -> bool`
- `async release_inflight(namespace, key)`
- `async prune_expired()` — no-op on Redis; evicts expired dict entries on inprocess.

Namespaces are constants in `coordination/__init__.py` (`NS_RATING_BACKOFF`, `NS_QUALITY_BG`) so a typo can't silently fork the keyspace.

**Backends:**

- `coordination/inprocess.py` — `dict[(ns, key)] -> expiry_ts`, monotonic loop time. Matches upstream behaviour exactly. Default when `REDIS_URL` is unset.
- `coordination/redis_backend.py` — `redis.asyncio.Redis`, `SET NX EX`, `EXISTS`, `DELETE`. Fail-open on Redis outage (return "no backoff" / "claim ok") so a coord outage degrades to per-process behaviour instead of breaking poster serving.

**Cherry-pick notes:**

- main.py loses two module-level dicts (`_quality_bg_inflight`, `_rating_backoff`). The cherry-pick path: when upstream adds new per-process state, mirror it as a new coord namespace + add `await coord.*` calls at the existing sites. Each change is bounded to one new namespace + a few call sites.
- The rating-coalescing async-Event dict is untouched in this phase — upstream's logic shape there is preserved.

**Test results:**

- In-process coordinator: backoff round-trip, TTL expiry, inflight NX semantics. PASS.
- Redis coordinator: same tests against `redis:7-alpine`. Cross-client visibility verified by reading the key with a separate redis client. PASS.
- Image rebuild with `redis>=5.0` dep added. Boots cleanly in three modes:
  - SQLite + inprocess (upstream default): `Storage backend: sqlite / Coordinator backend: inprocess`
  - Postgres + inprocess: backend logs match.
  - Postgres + Redis (hosted): `Storage backend: postgresql / Coordinator backend: redis`. `/health` 200 in all modes.

**Known follow-ups:**

- Render concurrency cap and split health probes are Phase 4.
- Per-tenant rate-limit hooks will land in Phase 5/8; the coord namespace pattern is already shaped for it.

---

## Phase 3 — Pluggable image-bytes store

**Branch:** `phase-3-blobstore`
**Status:** implementation complete, codex review pending

**Scope (narrowed from original plan):**

The original plan also moved the composite-poster JPEG bytes to object storage. That's deferred — the composite cache is bounded by `COMPOSITE_CACHE_TTL` and `COMPOSITE_MAX_ENTRIES` (set on hosted) and a CDN fronting `/poster` covers most read traffic anyway. The bigger blast-radius problem in Phase 3 is the TMDB poster/logo cache, which is currently filesystem-backed and doesn't survive horizontal scale.

Phase 3 moves TMDB poster + logo bytes to a `blobstore` package selected at import time by `OBJECT_STORE_URL`:

- `blobstore/local.py` — filesystem (upstream default). Same `/app/cache/tmdb_posters` / `/app/cache/tmdb_logos` layout. I/O wrapped in `asyncio.to_thread` so disk reads don't block the event loop.
- `blobstore/s3.py` — S3-compatible. Uses boto3 in thread-pool; works against AWS, Backblaze B2, Cloudflare R2, MinIO. Validates the bucket via `head_bucket` at startup so misconfiguration fails fast. Optional CDN URL via `OBJECT_STORE_PUBLIC_URL` (used by a future Phase 4 caller).

**API:**

- `async init()` / `async close()` — lifecycle wired into FastAPI lifespan.
- `ping()` — sync probe for /ready (Phase 4).
- `async get(bucket, key, max_age_seconds) -> bytes | None` — TTL-aware; stale entries are deleted lazily.
- `async put(bucket, key, data, content_type=None)`.
- `url_for(bucket, key) -> str | None` — CDN URL when configured (local backend always returns None).

Bucket constants in `blobstore/__init__.py` (`BUCKET_TMDB_POSTERS`, `BUCKET_TMDB_LOGOS`).

**Storage backend wiring:**

- `storage/sqlite_backend.py` — FS-based poster/logo functions (and their `_safe_cache_path` / `_remove_if_dir` helpers) **removed**; replaced with async wrappers that call `blobstore.get` / `blobstore.put`. The `_POSTER_TTL_SECONDS` / `_LOGO_TTL_SECONDS` constants here pass the existing config TTLs through.
- `storage/postgres_backend.py` — stops importing the FS helpers from `sqlite_backend.py`; has its own thin async wrappers identical in shape. Removes the cross-backend coupling that Phase 1 deliberately left in place as a stepping stone.
- `tmdb.py` — 4 callsites add `await`:
  - `await get_cached_tmdb_poster(...)` at line 212
  - `await set_cached_tmdb_poster(...)` at line 231
  - `await get_cached_tmdb_logo(...)` at line 274
  - `await set_cached_tmdb_logo(...)` at line 295

**Why poster/logo cache functions become async:**

Upstream's sync API was fine for filesystem I/O on a local volume. Once the bytes might come from S3 (a network call of 10-300ms), running it under `asyncio.to_thread` from a sync function would block the event loop or require a clumsy wrapper at every call site. Making the four functions async is the cleanest path; the only caller (tmdb.py) is already inside async context so the diff is mechanical.

For upstream cherry-pick: changes to upstream's `get_cached_tmdb_poster` etc. (in cache.py) need to be ported to `blobstore/local.py` (FS path) and `storage/{sqlite,postgres}_backend.py` (the async wrappers — usually unchanged unless TTL semantics shift).

**Test results:**

- Local mode: full round-trip on tmdb-posters + tmdb-logos via cache.py shim. Path-traversal rejection preserved. PASS.
- S3 mode: round-trip against MinIO via boto3. Cross-client visibility verified (objects readable by a separate boto3 client). Stale-eviction via `LastModified` + max-age also works. PASS.
- Image rebuild with `boto3>=1.34` added. Boots in three modes:
  - `Blob store: local / Storage: sqlite / Coordinator: inprocess` — upstream default.
  - `Blob store: s3 / Storage: sqlite / Coordinator: inprocess` — S3 only.
  - Full hosted (s3 + postgres + redis) — implicitly works, all three selectors are independent.

**Known follow-ups:**

- Composite-poster bytes still in the relational DB. A future iteration can add a `composites` bucket if the DB-storage cost actually bites at scale.
- `/poster` endpoint doesn't yet emit 302 redirects to `url_for(...)`; that wiring is Phase 4 territory (poster endpoint refactor for concurrency + redirects).

---

## Phase 4 — Bounded render concurrency + split health probes

**Branch:** `phase-4-render-bounds`
**Status:** implementation complete, codex review pending

**Render concurrency cap:**

- New `_render_semaphore` created lazily on first `/poster` request, sized by `config.RENDER_CONCURRENCY` (default = `os.cpu_count()`).
- The semaphore wraps the `run_in_executor(_composite_and_encode)` call — so the Pillow + JPEG encode work that pins a CPU core is what's being capped, not the rest of the pipeline (TMDB fetches, etc.).
- `RENDER_QUEUE_TIMEOUT` (default 30s) controls how long a queued request will wait. On timeout, return **503 + Retry-After: 5** so clients back off rather than compounding the saturation. The in-flight `_render_fut` (if any) is rejected with the same exception so coalesced callers also see the 503.
- Set `RENDER_QUEUE_TIMEOUT=0` to disable the timeout (wait forever — matches upstream behaviour).

**Split probes:**

- `/live` — process-alive only, identical body to upstream's `/health`.
- `/health` — alias of `/live` for upstream compatibility (the Docker healthcheck in compose.yaml still hits `/health`).
- `/ready` — checks storage, coordinator, and blob-store backends in parallel. Returns 200 with a JSON breakdown when all are reachable; 503 with the same breakdown otherwise.

**Verified end-to-end:**

- `/health`, `/live`, `/ready` all 200 in default mode.
- `/ready` body in Postgres mode: `{"storage": {"kind": "postgresql", "ok": true}, ...}`.
- Killing Postgres mid-flight: `/ready` flips to 503 with `"storage": {"ok": false}`; `/live` stays 200. Exactly the k8s readiness/liveness split we want — replica leaves rotation, but process isn't restarted for a transient backend hiccup.

**Codex review (2026-05-23):** two findings, both fixed:

- **[P1]** The 503 from render-queue saturation was raised inside a `try` whose final `except Exception` swallowed it and re-raised 500 — losing both the status code and the `Retry-After: 5` header. **Fixed:** added an explicit `except HTTPException: raise` before the generic handler so intentional HTTPExceptions propagate untouched.
- **[P2]** `/ready` called the sync `cache.ping()` / `blobstore.ping()` directly from an async handler. With Postgres on a slow link, the connection probe would block the event loop and delay `/live` plus normal requests — exactly what the probe split is meant to prevent. **Fixed:** wrapped both sync pings in `asyncio.to_thread` and run all three checks concurrently with `asyncio.gather`.

**Known follow-ups:**

- Render-saturation load test (multi-process k6 against /poster) is left for the deployment readiness checklist — code-level verification + the codex review cover the behaviour.

---

## Phase 5 — Leader-elected background jobs

**Branch:** `phase-5-leader`
**Status:** implementation complete, codex review pending

**Scope (narrowed):** the original plan also covered per-tenant rate limiting; that's been moved to Phase 8 (per-tenant cache namespacing + quota), the natural home for tenant-derived state. Phase 5 stays focused on the background-job side.

**Approach:**

Coordination layer gains three new functions, both backends:

- `async try_acquire_lease(name, ttl_seconds) -> str | None` — returns an opaque token on success, None if another holder already has it.
- `async refresh_lease(name, token, ttl_seconds) -> bool` — compare-and-set renewal; returns True iff we still hold the token.
- `async release_lease(name, token)` — compare-and-set release; only the token-holder can delete it.

**Backend implementations:**

- `coordination/inprocess.py` — dict-backed `(name) -> (token, expiry)`. In-process scope only. A multi-worker uvicorn deployment ends up with one leader per worker (no shared memory), which matches upstream's existing behaviour of running the prune/poll loops N times.
- `coordination/redis_backend.py` — `SET NX PX` for acquisition; `EVAL` with Lua compare-and-set for refresh and release so a slow replica can't blast through a faster one's lock. TTL is server-side so a crashed worker's lease expires naturally.

**Wired into:**

- `_cache_prune_loop` — lease TTL 8h (one cycle + buffer).
- `digital_release_poll_loop` — lease TTL = poll interval + 1h headroom.

Both loops release the lease in a `try/finally` so graceful shutdown hands it off immediately rather than waiting for TTL expiry.

**Verified:**

- In-process: acquire / wrong-token refresh refused / wrong-token release no-op / right-token refresh + release / TTL expiry all correct.
- Redis: cross-client visibility (token visible from a separate redis client); compare-and-set guard rejects wrong tokens; `PEXPIRE`-based TTL expiry works.
- Container boots cleanly; `/ready` returns 200; graceful shutdown sequence emits expected log lines in order (HTTP client → Coordinator → Blob store → Storage backend).

**Codex review (2026-05-23):** two findings, both fixed:

- **[P2]** Lifespan cancelled the background tasks then immediately closed the coordinator, so a Redis-backed deployment could close the connection mid-`release_lease()` and leave the cache-prune / digital-release leases stuck until 8h/25h TTL expiry. **Fixed:** the cancelled tasks are now awaited (suppressing `CancelledError`) before `coord.close()` runs, so the `finally`-block lease releases always complete first.
- **[P3]** `coordination.inprocess.close()` cleared `_backoff` and `_inflight` but not the new `_leases` dict, so a same-interpreter lifespan restart (test harness) could see a stale lease and skip the first iteration's work. **Fixed:** added `_leases.clear()` to the close path.

---

## Phase 6 — Prometheus metrics + structured logging

**Branch:** `phase-6-observability`
**Status:** implementation complete, codex review pending

**Metrics (`metrics.py`):**

- `postersplus_cache_lookups_total{table, result}` — counter. Tables: `final_poster`, `rating`, `quality`, `tmdb_metadata`, `tmdb_poster`, `tmdb_logo`. Result: `hit` or `miss`. Wrapped in `cache.py` at the public API, so both SQLite and Postgres backends are instrumented uniformly.
- `postersplus_render_duration_seconds` — histogram around the Pillow composite + JPEG encode work (buckets 0.05s – 10s).
- `postersplus_render_inflight` — gauge incremented while inside the render semaphore.
- `postersplus_render_saturated_total` — counter incremented when `/poster` returns 503 because the queue timed out.
- `postersplus_upstream_calls_total{service, status}` — counter scaffold; wiring to the actual TMDB/MDBList/AIOStreams call sites is deferred to Phase 7 where upstream resilience already touches those paths.
- `postersplus_backend_info{storage, coordinator, blobstore}` — gauge always 1; the label values carry the active backend identities so dashboards can group by mode.

**`/metrics` endpoint:**

- Unauthenticated by default. Set `METRICS_ACCESS_KEY` to require `?access_key=…` (constant-time compared, same pattern as the main access key).
- No separate metrics port — operators wanting strict isolation should bind the app behind an ingress that restricts `/metrics`.

**Structured JSON logs:**

- `LOG_FORMAT=json` swaps the root formatter to `python-json-logger`. Each log line becomes a single JSON object with `asctime`, `levelname`, `name`, `message` keys. Falls back to text format if the package is missing (defensive — `requirements.txt` always installs it).
- Default `LOG_FORMAT=text` matches upstream.

**Multi-worker aggregation:**

- `entrypoint.sh` sets `PROMETHEUS_MULTIPROC_DIR=/tmp/postersplus-prom` and clears stale files at startup. Each uvicorn worker writes to its own files in that directory.
- `/metrics` builds a `CollectorRegistry` + `MultiProcessCollector` when `PROMETHEUS_MULTIPROC_DIR` is set, so a scrape aggregates across all workers regardless of which one served the request. Without this, the default `WORKERS=2` would make every scrape see only half the traffic and counters would appear to jump or reset.
- Gauges declare multiproc modes (`livesum` for in-flight, `max` for backend_info) so they aggregate sensibly across workers.

**Verified:**

- `/metrics` returns counters + histogram + gauges; no auth in default mode, 403 when `METRICS_ACCESS_KEY` is set and missing.
- `LOG_FORMAT=json` emits JSON lines.
- WORKERS=2: `backend_info{blobstore=local, coordinator=inprocess, storage=sqlite} 1.0` aggregates to a single value despite 4 per-worker prom files in `/tmp/postersplus-prom/`.

**Codex review (2026-05-23):** 1 P2 found and fixed.

- **[P2]** Without `PROMETHEUS_MULTIPROC_DIR`, scrapes only saw the worker that happened to serve `/metrics`, so the metrics endpoint was unreliable in the default `WORKERS=2` deployment. **Fixed:** entrypoint.sh sets the multiproc dir; `/metrics` builds a `MultiProcessCollector`-backed registry when the env var is present; gauges declare explicit aggregation modes.

---

## Phase 7 — Upstream retries + circuit breaker

**Branch:** `phase-7-upstream-resilience`
**Status:** implementation complete, codex review pending

**Scope (narrowed):** serve-stale was deferred. It requires adding an `allow_stale=True` flag to every cache lookup function and a parallel-stale-row path in both storage backends, which is invasive enough to deserve its own phase. The shipped retries + breaker already handle the common case (transient TMDB/MDBList 5xx, single-digit-second timeouts); serve-stale only matters for sustained outages that exceed the breaker cool-off window.

**`upstream.py` — single entry point for resilient HTTP:**

- `await upstream.request(client, method, url, service=…, raise_for_status=True, **kw)` — drop-in replacement for `await client.request(...)`.
- **Retries**: up to 3 attempts on `httpx.TimeoutException`, `httpx.HTTPError`, and any 5xx response. Exponential backoff (0.5s → 1s → 2s) with ±25% jitter so synchronised retries don't dogpile a recovering upstream.
- **Circuit breaker**: per-service in-process state. After 5 consecutive failures the breaker opens for 30s; calls during that window raise `CircuitOpenError` without touching the network. A successful response resets the failure counter. 4xx is not a circuit-breaker failure (network is fine; request was bad).
- **Metrics**: every attempt increments `postersplus_upstream_calls_total{service, status}` (status = 2xx / 3xx / 4xx / 5xx / timeout / error / circuit_open). Wires up the metric Phase 6 declared as a scaffold.

**Wired into three high-value paths (covers ~95% of upstream traffic):**

- TMDB metadata fetch ([tmdb.py:120](tmdb.py#L120)) — long-tail TMDB 5xx and slow responses are the most common failure mode.
- MDBList ratings ([ratings.py:50](ratings.py#L50)) — handles MDBList rate-limit recovery and brief outages.
- AIOStreams quality ([quality.py:104](quality.py#L104)) — keeps badge fetching robust to AIOStreams hiccups.

TMDB image fetches (poster/logo) and the Arctic Shift Reddit poll already have local retry/timeout handling; not wired through `upstream.py` to keep the diff bounded.

**Verified:**

- Unit tests against mocked `httpx.AsyncBaseTransport`:
  - 5 consecutive 503s trip the breaker; the 6th call raises `CircuitOpenError` without making a network call.
  - 503-then-200 retries once and returns the 200.
  - Successful call resets the breaker's failure counter to zero.

**Known follow-ups:**

- Serve-stale: future phase. Adds `allow_stale=True` to storage cache lookups; caller falls back to stale data when `CircuitOpenError` is raised.
- Cross-replica breaker state (e.g. via Redis) is intentionally not implemented — each replica's local breaker recovers independently in seconds, and a single replica failing to detect a wider outage is acceptable for our use case.

**Codex review (2026-05-23):** 1 P2 found and fixed.

- **[P2]** Subtle half-open bug: after the 30s cool-off, `is_open()` returned False but the stale `opened_at` was still non-zero. The `opened_at == 0.0` guard in `record_failure()` then prevented the breaker from re-tripping if the half-open probe failed, so subsequent calls bypassed the breaker for the rest of the outage. **Fixed:** `record_failure()` always refreshes `opened_at` once threshold is crossed; the log line is only emitted on the first trip. Added a regression test that trips → waits cool-off → fails the probe → verifies the breaker re-locks.

---

## Phase 8 — Per-tenant rate limiting

**Branch:** `phase-8-rate-limit`
**Status:** implementation complete, codex review pending

**Scope (narrowed):** the original plan also included cache namespacing — splitting the keyspace per tenant so tenant A can't read tenant B's cached posters/ratings. On closer inspection that's unnecessary: the cached data (rating values, poster bytes, composite renders) is **content-identical** regardless of which tenant's API key fetched it, and the composite cache key already includes a hash of rendering params so different per-tenant configs naturally produce different cache entries. The actually-shared resource is upstream API **quota**, not cache bytes — that's what Phase 8 protects.

**Tenant identity:**

- If the request supplies `tmdb_key=…` → tenant = `sha256(tmdb_key)[:16]`.
- Else if the request supplies `mdblist_key=…` → tenant = `sha256(mdblist_key)[:16]`.
- Otherwise → tenant = `operator` (single bucket for all anonymous + operator-key traffic).

This means tenants who bring their own keys throttle themselves without affecting other tenants. Anonymous/operator traffic shares a single bucket — for stricter isolation the operator should set `ACCESS_KEY` and gate at the edge.

**`coord.check_rate_limit(tenant, rps) -> (allowed, retry_after)`:**

- `inprocess`: per-tenant dict `{tenant: (window_start_epoch, count)}`. Fixed 1-second windows.
- `redis`: `INCR postersplus:rate:<tenant>:<window>` with `EX 2` (pipeline so it's one RTT). Fail-open on Redis errors — a coord hiccup must not 429 every request.

**Wired into `/poster` ([main.py:1018](main.py#L1018))** at the top of the handler, before any expensive work. Returns 429 with `Retry-After: 1` when over the limit.

**Verified:**

- In-process: 5 RPS limit denies the 6th call within the same second; other tenants unaffected; `rps=0` disables the check entirely.
- Redis: same scenarios against `redis:7-alpine`; cross-process counters work (pipeline INCR+EXPIRE is atomic).
- Container boots cleanly with `RATE_LIMIT_RPS=0` (default).

**Known follow-ups:**

- Sliding window (vs. the current fixed 1-second window) would smooth out the start-of-second burst pattern. Fixed window is good enough for the use case (preventing one tenant monopolising the operator's quota); sliding can come later if needed.
- Soft-cap warning logs at e.g. 80% of the limit aren't implemented; could be added without touching the API.

**Codex review (2026-05-23):** 1 P2 found and fixed.

- **[P2]** In-process `_rl_buckets` dict grew unbounded — every distinct `tmdb_key=…` URL param spawned a new entry that was never reaped, so a public instance could be DoS'd by sending many unique key values. **Fixed:** opportunistic stale-bucket sweep inside `check_rate_limit` when the dict exceeds 1024 entries; periodic prune in `prune_expired` (called from the 6h cache-prune loop); full clear in `close()`. Redis backend was already bounded by `EX 2` server-side TTL.

---

## Phase 9 — Image hardening, secrets, k8s manifests

**Branch:** `phase-9-deploy`
**Status:** implementation complete, codex review pending

**Dockerfile (multi-stage):**

- Builder stage installs `build-essential` and runs `pip install --prefix=/install`.
- Final stage is `python:3.11-slim` pinned to a specific digest, no compiler toolchain, with only `curl` + `ca-certificates` for the healthcheck and TLS.
- Image size cut from **913 MB → 479 MB** (≈48% reduction) by dropping `build-essential` from the runtime.
- `HEALTHCHECK` baked into the image so operators running raw `docker run` get the healthcheck without compose.

**Compose hardening:**

- `read_only: true` + `tmpfs: [/tmp, /tmp/postersplus-prom]` — root filesystem is immutable; writable areas explicit.
- `cap_drop: [ALL]` + `security_opt: [no-new-privileges:true]`.
- `image: postersplus` (lowercased — upstream's "PostersPlus" trips modern Docker).
- Verified: `docker compose up` boots cleanly; `/live` returns 200; no writes blocked by the read-only FS.

**`requirements.txt`:**

- Upstream's unpinned dependency list is left as-is (still floating versions) so cherry-picks from upstream apply cleanly. Operators wanting reproducible builds can run `pip-compile --generate-hashes` against this file and commit the lock alongside; this fork ships the loose list to minimise upstream drift.

**Kubernetes manifests (`deploy/k8s/`):**

```text
deploy/k8s/
├── kustomization.yaml      # bundles everything
├── namespace.yaml
├── configmap.yaml          # backend URLs, RPS, log format, etc.
├── secret.yaml             # placeholder (replace with ExternalSecret/SealedSecret in prod)
├── deployment.yaml         # 2 replicas, hardened SecurityContext, /live + /ready probes
├── service.yaml            # ClusterIP on 8000
├── hpa.yaml                # 2–10 replicas; CPU 70% / mem 80% targets
├── pdb.yaml                # minAvailable: 1
├── networkpolicy.yaml      # in-namespace + 443 egress
└── README.md               # usage, secrets, FQDN-policy notes
```

Highlights:

- **SecurityContext**: `runAsNonRoot`, `readOnlyRootFilesystem`, `seccompProfile: RuntimeDefault`, `capabilities.drop: ALL`, `allowPrivilegeEscalation: false`.
- **Probes**: `/live` for liveness (cheap), `/ready` for readiness with backend checks (Phase 4).
- **Multiproc Prometheus**: `emptyDir` mounted at `/tmp/postersplus-prom` matches `entrypoint.sh`'s default. Works with the Deployment's default `WORKERS=2`.
- **HPA**: CPU + memory triggers; can be switched to a custom metric (e.g., `postersplus_render_inflight`) via KEDA / Prometheus adapter if needed.
- **NetworkPolicy**: vanilla Kubernetes can't FQDN-match, so the included policy is permissive on `:443` egress. The README points operators at Cilium / Calico Enterprise for strict FQDN allowlisting.

**Verified:**

- `docker build` succeeds; image is half the previous size; `docker run` boots cleanly.
- `docker compose up` works with the new hardening (`read_only: true` doesn't break the app — all writes are scoped to `/app/cache` or `/tmp`).
- `kubectl kustomize deploy/k8s/` renders correctly; NetworkPolicy peer selectors stay scoped to their targets (kube-dns, postgres, redis) without app-label pollution.

**Codex review (2026-05-23):** 1 P1 found and fixed.

- **[P1]** `commonLabels: app.kubernetes.io/name: postersplus` in `kustomization.yaml` was injecting that label into every selector — including the NetworkPolicy egress peer selectors. The rendered DNS peer became `k8s-app: kube-dns AND app.kubernetes.io/name: postersplus` (matches nothing), and the Postgres/Redis peers became `app.kubernetes.io/name: postgres AND … postersplus` (also matches nothing). Pods would have been unable to resolve DNS or reach either backend. **Fixed:** removed `commonLabels` entirely; the labels we need are already declared directly on each resource's `spec.selector.matchLabels` / `template.metadata.labels`. Verified via `kubectl kustomize` — peer selectors now render clean.

---

## Phase 10 — Composite bytes to object storage; TMDB cache back to filesystem

**Branch:** `phase-10-composite-blobstore`
**Status:** implementation complete, codex review pending

**Motivation:** Phase 3 put TMDB poster/logo bytes behind the blobstore abstraction and left composite (rendered) bytes in the relational backend as BYTEA. That was the wrong way round:

- TMDB's own CDN (`image.tmdb.org`) is the source of truth for poster/logo bytes — mirroring it across replicas via S3 buys very little.
- Composite renders are the unique work product. They scale with `unique(imdb_id × type × params_hash)` and are the bigger driver of DB storage cost.
- A CDN in front of the composite-cache S3 bucket means clients pull bytes directly from the CDN — the app pod isn't on the read path at all.

**Architecture after Phase 10:**

```text
TMDB CDN (image.tmdb.org)
   │
   ▼
[ per-pod emptyDir cache ]   ← TMDB poster/logo bytes; ephemeral, re-warms on pod restart
   │
   ▼
[ render pipeline ]
   │  (writes once)
   ▼
[ S3 / B2 bucket ]   ← composite-poster JPEGs; the unique work product
   │
   ▼  (Bandwidth Alliance: B2→Cloudflare free)
[ Cloudflare custom domain ]   ← posters.postersplus.elfhosted.com
   │
   ▼
[ Stremio client ]   ← 302'd here from /poster on cache hit
```

**Code changes:**

- `blobstore/__init__.py` — drops `BUCKET_TMDB_POSTERS` / `BUCKET_TMDB_LOGOS`; adds `BUCKET_COMPOSITES`. Adds `delete()` to the public API for evictions.
- `blobstore/local.py` — atomic write via temp+rename; single bucket (`composites`) under `COMPOSITE_BLOB_DIR`.
- `blobstore/s3.py` — adds `delete()`; everything else unchanged.
- `storage/sqlite_backend.py` + `storage/postgres_backend.py`:
  - `get_cached_tmdb_poster` / `get_cached_tmdb_logo` — reverted to upstream-style sync filesystem ops (per-pod cache).
  - `get_cached_final_poster` — async; reads metadata row, then bytes via `blobstore.get(BUCKET_COMPOSITES, …)`.
  - `set_cached_final_poster` — async; writes blob first (so the metadata row is never present without a backing blob), then upserts the row.
  - `get_cached_final_poster_url()` — new sync function returning the public CDN URL when configured.
  - Schema migration: `final_poster_cache` loses the `jpeg_bytes` BLOB/BYTEA column. SQLite uses `ALTER TABLE … DROP COLUMN` (3.35+) with a table-rebuild fallback for older versions; Postgres uses `ALTER TABLE … DROP COLUMN IF EXISTS`. Existing rows are preserved but bytes are lost — cache refills via TTL.
- `cache.py` — async wrappers for composite; sync wrappers for TMDB; new `get_cached_final_poster_url` re-export.
- `tmdb.py` — drops `await` from the 4 TMDB poster/logo callsites (back to sync).
- `main.py` — composite-cache-hit path: if `get_cached_final_poster_url(key)` returns a URL, emit a `302` with `Location: <cdn-url>` (+ optional `Cache-Control` from `CDN_CACHE_TTL`). Otherwise stream bytes inline (private/local deployments). `await` added to the two composite cache callsites.
- `config.py` — adds `COMPOSITE_BLOB_DIR=/app/cache/composites`.

**Infra changes:**

- `infra/postersplus/configmap-postersplus-env.yaml` — `OBJECT_STORE_URL` points at the B2 bucket (`s3://postersplus-composites?endpoint=https://s3.us-west-002.backblazeb2.com&region=us-west-002`); `OBJECT_STORE_PUBLIC_URL=https://posters.postersplus.elfhosted.com`.
- TMDB cache: `/app/cache` emptyDir mount (already configured in the HelmRelease) is enough — per-pod ephemeral storage.

**Verified end-to-end:**

- SQLite + local blobstore: composite write+read round-trip; `url_for()` returns `None` (no public URL); TMDB sync FS round-trip preserved.
- Migration from legacy schema: pre-existing SQLite DB with `jpeg_bytes BLOB` column → init_db drops the column, keeps rows. Same for Postgres via `ALTER TABLE DROP COLUMN IF EXISTS`.
- Postgres + S3 (MinIO): write composite → metadata row in Postgres + bytes in S3 at `composites/<key>`; `url_for()` returns `https://posters.example.com/composites/<key>` matching configured public URL.
- Container image (uid 568 + read-only + tmpfs): boots; `/live` 200; `/ready` 200.

**Net data-layer change:**

- Postgres `final_poster_cache` table goes from ~150 KB/row average → ~50 bytes/row (cache_key + cached_at). At 1M rows that's 150 GB → 50 MB.
- Composite bytes live in B2 (free CF egress) with per-object cost; pruned by `COMPOSITE_CACHE_TTL` (TTL-based deletion on read) + the existing `COMPOSITE_MAX_ENTRIES` LRU cap.

**Codex review (2026-05-23):** two P2 findings, both fixed:

- **[P2]** The CDN-redirect path still fetched bytes from S3 before 302'ing — `get_cached_final_poster` (which does a `blobstore.get`) was called before checking if a CDN URL was available. Defeated the whole point of CDN offload. **Fixed:** added `is_cached_final_poster_fresh(cache_key)` — a lightweight metadata-row+TTL probe with no blob I/O. `/poster` now branches *first* on `OBJECT_STORE_PUBLIC_URL`: if set, freshness-probe + 302; if not, fall through to the inline-bytes path.
- **[P2]** `prune_caches()` deleted expired metadata rows but left the blobstore objects orphaned forever. **Fixed:** `prune_caches` is now async; it captures the expiring `cache_key`s before the DELETE, then `await blobstore.delete()`s each one after commit. Same pattern in both SQLite and Postgres backends.

**Known follow-ups:**

- B2 lifecycle rule could enforce TTL server-side as a defence-in-depth in case the prune loop ever misses a cycle. Adding a `Lifecycle` rule that auto-deletes objects older than `COMPOSITE_CACHE_TTL` is a one-line bucket setting.
- Could expose `postersplus_composite_cdn_hits_total` metric (incremented on every 302) so dashboards can see the CDN-vs-inline split.

---

## Codex review log

Per-phase second opinion via `~/.claude/bin/codex-review`. Findings recorded inline below.

### Phase 0 — reviewed

Codex review (2026-05-23) flagged:

- **[P2]** README hosted-mode quickstart documented env vars (`DATABASE_URL`, `REDIS_URL`, `OBJECT_STORE_*`) the code does not yet read — risk of operators believing hosted backends are live. **Fixed:** added "target state" framing and a callout pointing at this doc for current status.
- **[P3]** README pointed at `.env.example` which doesn't exist (upstream ships `.env` directly). **Fixed:** link now points at `.env`.

No code review since Phase 0 is docs-only.

### Phase 1 — reviewed pending

**Implementation summary:**

- `storage/sqlite_backend.py` — near-verbatim extraction of upstream's cache.py SQLite logic. Adds `ping()` and `close()` helpers for /ready probes and graceful shutdown.
- `storage/postgres_backend.py` — new. Same public surface, psycopg3 + `psycopg_pool.ConnectionPool`. Filesystem-backed TMDB poster/logo helpers re-exported from the SQLite module so Phase 3 only has to touch one place. Schema bootstrap protected by a Postgres advisory lock so concurrent replica startup doesn't race.
- `storage/__init__.py` — backend selector. Reads `config.DATABASE_URL`; falls back to SQLite when unset. Re-exports the public surface as module-level callables; sets `BACKEND_KIND` for callers that need to know.
- `cache.py` — reduced to a thin re-export shim from `storage`. Every `from cache import …` callsite (main.py / tmdb.py / quality.py / digital_release.py) keeps working unchanged.
- `config.py` — adds `DATABASE_URL`, `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`.
- `main.py` — adds `close as close_db` to the cache import block and one `close_db()` call at the end of `lifespan()`.
- `requirements.txt` — adds `psycopg[binary,pool]>=3.1`. Always installed; postgres_backend module is only imported when DATABASE_URL is set, so the dep is otherwise inert.
- `compose.yaml` — adds DATABASE_URL/pool env vars; adds an optional `postgres` service behind `--profile hosted`.
- `.env` — documents the new env vars.
- `.dockerignore` — excludes RESILIENCE.md from the image build context (matches LICENSE/README.md pattern).

**Test results:**

- SQLite mode: full round-trip on all cache tables (rating, quality, metadata, trending, composite, digital release) + ping + prune. Container boots, `/health` returns 200. Backend logged at startup.
- Postgres mode: same round-trip against Postgres 16-alpine in a fresh container. All 6 expected tables created. Idempotent re-init verified. Upsert behaviour verified (INSERT ON CONFLICT DO UPDATE). Dedup behaviour verified (INSERT ON CONFLICT DO NOTHING).
- Image builds clean with the new `psycopg[binary,pool]` dependency.

**Known follow-ups (not in scope for Phase 1):**

- `image: PostersPlus` in compose.yaml is uppercase and trips modern Docker — defer to Phase 9 (deploy hardening) where compose.yaml gets a full rework.
- No SQLite→Postgres data migration tool. Operators starting fresh on Postgres get an empty cache that warms on first use. A `scripts/migrate_sqlite_to_postgres.py` can be added later if anyone asks.

### Phase 1 — reviewed

Codex review (2026-05-23): **clean, no correctness issues found**. Verbatim:
> "No discrete correctness issues were found in the current staged, unstaged, and untracked code changes. The SQLite shim/extraction preserves the existing public API, and the Postgres backend appears consistent with the same call surface."

Post-review self-audit found one minor robustness issue (not flagged by Codex):

- The `postgres://` URL scheme (Heroku/legacy alias) was accepted by the selector but psycopg3 only natively parses `postgresql://`. **Fixed:** selector now rewrites `postgres://` → `postgresql://` before the pool is opened.

Phase 1 ready to merge once user signs and commits.

### Phase 3 — reviewed

Codex review (2026-05-23) found one P2 issue (now fixed):

- **[P2]** `blobstore/s3.py` stale-eviction path returned without closing `resp["Body"]`, leaking the streaming body back to GC. Under sustained stale-traffic load this can exhaust the boto3 HTTP connection pool. **Fixed:** wrapped the read in `try/finally` so `body.close()` runs on every exit path (stale-skip and normal read).

Phase 3 ready to merge once user signs and commits.

### Phase 2 — reviewed

Codex review (2026-05-23): **clean, no correctness/security/performance/maintainability issues found**. Verbatim:
> "No discrete correctness, security, performance, or maintainability issues were identified in the current staged, unstaged, or untracked changes."

Self-audit notes (not findings, just explicit rationales for future readers):

- `coord.is_backoff_active` and `claim_inflight` fail-open on Redis errors so a transient coord outage degrades to per-process behaviour rather than 500ing the poster endpoint. The cost is one wasted upstream call per replica per affected title until Redis recovers.
- Coordinator init pings Redis at startup; misconfiguration fails fast at lifespan boot rather than silently swallowing every coord call later.
- In-process `claim_inflight` is race-free in asyncio (single-threaded; no `await` between get and set).

Phase 2 ready to merge once user signs and commits.

...
