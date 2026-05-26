# PostersPlus — ElfHosted fork

This is [ElfHosted](https://elfhosted.com)'s fork of [UmbraProjects/PostersPlus](https://github.com/UmbraProjects/PostersPlus), adapted for **public, multi-tenant hosting at scale**.

The upstream project is designed to be small, self-contained, and operator-friendly for a single user or family running it at home. We love that — but a hosted environment serving many tenants needs a different shape: shared coordination state, horizontally scalable storage, observability hooks, per-tenant quotas, and graceful degradation when upstream APIs misbehave.

This fork adds those things **as opt-in backends**. The original single-file SQLite + local-disk path is still the default, so:

- Anyone running PostersPlus privately can use this fork with **zero config changes** — it behaves like upstream.
- Operators running it for many users can flip env vars to enable Postgres, Redis, S3-compatible object storage, Prometheus metrics, etc.
- Either side of the fork can cherry-pick improvements from the other without ceremony.

## Backend matrix

| Concern               | Default (private)            | Hosted (opt-in)                             |
| --------------------- | ---------------------------- | ------------------------------------------- |
| Metadata cache        | SQLite (WAL)                 | PostgreSQL                                  |
| Coordination state    | In-process dicts             | Redis                                       |
| Image bytes           | Local disk + SQLite blobs    | S3-compatible object store (+ CDN)          |
| Background jobs       | In-process asyncio tasks     | Leader-elected (Postgres advisory lock)     |
| Metrics               | None                         | Prometheus `/metrics`                       |
| Logging               | Plain text                   | Structured JSON + request IDs               |
| Rate limiting         | None                         | Per-tenant via Redis                        |

Selecting a backend is purely an env-var change — see [.env](.env) for the full surface area. Every opt-in is independent; you can run "Postgres + local disk + no Redis" or any other combination.

> **Note:** as of this commit only the **default (private)** column is implemented. Each hosted-mode backend lands in its own phase — see [RESILIENCE.md](RESILIENCE.md) for current status. The hosted quickstart below is the **target shape** and won't fully work until the relevant phases ship.

## Relationship to upstream

- **Upstream:** [UmbraProjects/PostersPlus](https://github.com/UmbraProjects/PostersPlus) — the canonical project.
- **This fork:** maintained by ElfHosted to support our hosted offering.
- **Sync direction:** we periodically pull from upstream and rebase our hosting-specific changes on top. Where our changes are generally useful, we'll upstream them as PRs.
- **Cherry-pickability:** changes are kept in narrow, self-contained commits where possible, with new functionality gated behind feature flags / env vars rather than woven into existing code paths.
- **Versioning:** this fork's tags mirror upstream's. When we cherry-pick upstream's `vX.Y.Z`, we tag our equivalent commit `vX.Y.Z`. Fork-only patches that ship between upstream releases get a `-elf.N` pre-release suffix (e.g. `v1.0.3-elf.1`) so they sort *before* the next upstream tag and we can never get ahead of upstream's numbering. Mechanically this is enforced by writing `Release-As: <version>` in every release-tracking commit footer.

## Hosting-mode quickstart (target — see status above)

```bash
# Private/single-user (same as upstream — works today)
docker compose up -d

# Hosted, with Postgres + Redis + S3 (target — phases 1, 2, 3 land these)
cat > .env.hosted <<'EOF'
ACCESS_KEY=<strong-random>
TMDB_API_KEY=<operator-key>
MDBLIST_API_KEY=<operator-key>

# Storage backends
DATABASE_URL=postgresql://postersplus:****@postgres:5432/postersplus
REDIS_URL=redis://redis:6379/0
OBJECT_STORE_URL=s3://postersplus-cache?endpoint=https://s3.example.com
OBJECT_STORE_PUBLIC_URL=https://cdn.example.com/postersplus

# Resource ceilings
COMPOSITE_MAX_ENTRIES=20000
RENDER_CONCURRENCY=4
EOF
docker compose --env-file .env.hosted up -d
```

See [RESILIENCE.md](RESILIENCE.md) for the phased rollout plan and per-phase design notes.

## License

Same license as upstream (see [LICENSE](LICENSE)).
