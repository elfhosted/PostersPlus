# Kubernetes manifests

Minimal kustomize base for hosting PostersPlus on Kubernetes with the ElfHosted fork's hosted-mode backends.

## What's here

```
deploy/k8s/
├── kustomization.yaml      # bundles everything below
├── namespace.yaml          # creates the postersplus namespace
├── configmap.yaml          # non-secret env (backend URLs, RPS, etc.)
├── secret.yaml             # placeholder shape — see below
├── deployment.yaml         # 2 replicas by default, hardened SecurityContext
├── service.yaml            # ClusterIP on 8000
├── hpa.yaml                # 2-10 replicas, CPU 70% / mem 80% targets
├── pdb.yaml                # minAvailable: 1 during voluntary disruptions
└── networkpolicy.yaml      # in-namespace + 443 egress allowlist
```

This is a **base**, not a complete environment. You still need to provision Postgres, Redis, and S3 (or compatible) separately — there are good operators for each. The ConfigMap's `DATABASE_URL` / `REDIS_URL` / `OBJECT_STORE_URL` assume in-namespace services named `postgres` and `redis`.

## Quickstart

```bash
# 1. Edit configmap.yaml to point at your real Postgres / Redis / S3 endpoints
# 2. Replace secret.yaml with your actual key material (see below)
# 3. Push your image and update the kustomization images: section

kubectl apply -k deploy/k8s/
kubectl -n postersplus get pods
```

## Secrets

**Do not commit real keys.** The `secret.yaml` file is a placeholder showing the expected shape. In production, replace it with one of:

- **[external-secrets-operator](https://external-secrets.io/)** — `ExternalSecret` pointing at Vault / AWS Secrets Manager / etc. Remove `secret.yaml` from `kustomization.yaml resources:`; add your `ExternalSecret` manifest instead.
- **[sealed-secrets](https://sealed-secrets.netlify.app/)** — `kubeseal` the secret and commit the encrypted version.
- **Helm values file outside the repo** — convert this kustomize base into a Helm chart and pass secrets via `--set-string` or a values file kept in your secrets store.

## Probes

- `/live` — process is alive. Liveness probe; restart the pod if this stops responding.
- `/ready` — pod is ready to serve traffic. Returns 503 + JSON breakdown when any of the configured backends (storage / coordinator / blobstore) is unreachable. The pod will leave LB rotation until it recovers, but won't be restarted (a transient Postgres or Redis hiccup shouldn't kill the process).
- `/health` — alias of `/live`, for legacy callers and the upstream Docker healthcheck.

## Metrics

`/metrics` exposes Prometheus counters/histograms (see `metrics.py`). The deployment annotates pods with `prometheus.io/scrape: "true"` for static scrape-config or PodMonitor-driven setups.

When `WORKERS > 1`, multiprocess aggregation is enabled automatically by `entrypoint.sh` (sets `PROMETHEUS_MULTIPROC_DIR=/tmp/postersplus-prom`). The Deployment mounts an `emptyDir` at that path.

If you want `/metrics` access-controlled, set `METRICS_ACCESS_KEY` in `secret.yaml` and pass `?access_key=…` from your scraper.

## Tuning

- `RENDER_CONCURRENCY` (ConfigMap) — match to per-pod CPU limit. With `limits.cpu: 2`, set to `4`-ish (Pillow renders block one core; some slack for queueing).
- `RATE_LIMIT_RPS` — 0 disables. With operator-keys-only deployments, `0` is fine. With tenants bringing their own keys, set high enough that legitimate library refreshes don't hit it (~20–50 RPS is a reasonable starting point).
- HPA — CPU and memory triggers. If you want to scale on `postersplus_render_inflight` instead, add a custom-metrics adapter (KEDA / Prometheus adapter) and switch the HPA's `metrics:` section.

## Network policy

The included `NetworkPolicy` is intentionally loose on egress (any `tcp/443` allowed) because vanilla Kubernetes NetworkPolicies can't match FQDNs. For strict FQDN allowlisting (TMDB / MDBList / your S3 endpoint only), replace with a CNI-specific policy:

- **Cilium**: `CiliumNetworkPolicy` with `toFQDNs:`
- **Calico Enterprise**: GlobalNetworkPolicy with `domains:`

See the upstream API hosts list in [RESILIENCE.md](../../RESILIENCE.md).
