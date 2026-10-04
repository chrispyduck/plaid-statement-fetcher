# Statement Fetcher

Statement Fetcher is a split web application for linking accounts with Plaid or Yodlee and downloading statement PDFs. Each linked institution records which provider it was linked through; sync/refresh dispatches to the right provider per institution automatically.

- Backend: FastAPI + SQLite state store
- Frontend: React SPA (Material UI)
- Deployment target: Kubernetes (frontend/backend split)
- Container publication target: GitHub Container Registry (GHCR)

## Architecture

- Frontend service:
  - Serves SPA pages (Home, Account Details, Sync Progress, Service Config)
  - Calls backend over relative `/api` routes
- Backend service:
  - Plaid link token creation + exchange
  - Yodlee FastLink session creation + link completion
  - Linked account and alias management
  - Statement sync orchestration + detailed event logs, dispatched per linked item's provider
  - Runtime service configuration persistence

State is stored in `config/state.db` (SQLite) and PDFs in `config/output`.

## Repository Layout

```text
.
├── Dockerfile                         # Backend image
├── frontend/
│   ├── Dockerfile                     # Frontend image
│   ├── nginx.conf
│   └── src/
├── k8s/
│   └── base/
│       ├── backend/
│       │   ├── deployment.yaml
│       │   ├── service.yaml
│       │   ├── configmap.yaml
│       │   ├── secret.example.yaml
│       │   ├── pvc.yaml
│       │   └── kustomization.yaml
│       ├── frontend/
│       │   ├── deployment.yaml
│       │   ├── service.yaml
│       │   └── kustomization.yaml
│       ├── ingress.yaml
│       └── kustomization.yaml
├── src/statement_fetcher/
└── .github/workflows/
    ├── pr-validation.yml
    ├── publish-artifacts.yml
    └── release.yml
```

## Runtime Configuration

Backend reads configuration from environment variables.

Required:
- `PLAID_CLIENT_ID`
- `PLAID_SECRET`

Core settings:
- `PLAID_ENV=sandbox|production`
- `PLAID_LANGUAGE=en-US`
- `PLAID_PRODUCTS=statements`
- `PLAID_COUNTRY_CODES=US`
- `PLAID_REDIRECT_URI=auto` or explicit callback URL
- `PSF_CONFIG_ROOT=/app/config`
- `PSF_ENCRYPTION_SECRET=your-long-random-secret`
- `PSF_RETRY_MAX_ATTEMPTS=5`
- `PSF_RETRY_BASE_DELAY_SECONDS=1`
- `PSF_RETRY_MAX_DELAY_SECONDS=30`

Notes:
- `PLAID_ENV` is a mode flag only; state path does not branch by env.
- `PSF_ENCRYPTION_SECRET` enables encryption at rest for sensitive persisted values.
- Service configuration page can override selected non-secret settings at runtime.

### Yodlee (optional second provider)

Required:
- `YODLEE_CLIENT_ID`
- `YODLEE_SECRET`
- `YODLEE_LOGIN_NAME` — the Yodlee end-user loginName whose linked institutions this
  app manages. In sandbox, use one of the 5 pre-provisioned test users shown under
  "View Test Users" on the Yodlee developer dashboard (e.g. `sbMem...1`). There is no
  self-service way to create this loginName via the API in sandbox; in production it's
  a loginName you provision for yourself through Yodlee's user-provisioning flow.

Core settings:
- `YODLEE_API_URL` — e.g. `https://sandbox.api.yodlee.com/ysl` (sandbox) or your
  production base URL.
- `YODLEE_FASTLINK_URL` — the FastLink launch URL for the same environment.
- `YODLEE_FASTLINK_CONFIG_NAME` — defaults to `Aggregation`; set from the FastLink
  Configuration Tool in your Yodlee dashboard if you use a different config.

Known limitations, worth reading before relying on this integration:
- Yodlee's Documents API (`/documents/search`, `/documents/{id}`) has no publicly
  documented date field for a statement. `extract_document_date` in `yodlee_api.py`
  tries several plausible field names and a date embedded in the filename, falling
  back to today's date (and logging a warning) if none match. If you see statements
  dated "today" in bulk, the real field name needs to be identified from a live
  response and added there.
- Yodlee's sandbox test institution ("Dag Site") has account/balance test data but no
  document/eStatement test data, so the statement list/download path can only be
  verified end-to-end against a real linked bank, not sandbox.
- Reauthentication detection (Plaid's `ITEM_LOGIN_REQUIRED` equivalent) isn't
  implemented for Yodlee yet, since its error code for a failed/expired login isn't
  confirmed. A Yodlee login failure currently surfaces as a generic sync error rather
  than the "Reconnect" UI treatment Plaid items get.

## Local Development

### Backend

```bash
uv sync --dev
uv run statement-fetcher serve --env sandbox --host 127.0.0.1 --port 8765
```

### Frontend

```bash
cd frontend
npm ci
npm run dev
```

Use `frontend/.env.example` to create local env overrides if needed.

## Docker

### Backend image

```bash
docker build -t statement-fetcher-backend:local -f Dockerfile .
```

Run:

```bash
docker run --rm -p 8765:8765 \
  -e PLAID_ENV=production \
  -e PLAID_CLIENT_ID=... \
  -e PLAID_SECRET=... \
  -v $(pwd)/config:/app/config \
  statement-fetcher-backend:local
```

### Frontend image

```bash
docker build -t statement-fetcher-frontend:local -f frontend/Dockerfile frontend
```

Run:

```bash
docker run --rm -p 8080:8080 statement-fetcher-frontend:local
```

## Kubernetes Deployment

The base uses split components with distinct labels:
- `app.kubernetes.io/component=backend`
- `app.kubernetes.io/component=frontend`

Ingress routes:
- `/api`, `/healthz`, `/plaid` -> backend service
- `/` -> frontend service

### 1. Configure secrets

Create backend secret from template:

```bash
cp k8s/base/backend/secret.example.yaml /tmp/statement-fetcher-secret.yaml
# edit PLAID_CLIENT_ID and PLAID_SECRET
kubectl apply -f /tmp/statement-fetcher-secret.yaml
```

### 2. Set image references

Update image fields in:
- `k8s/base/backend/deployment.yaml`
- `k8s/base/frontend/deployment.yaml`

### 3. Deploy

```bash
kubectl apply -k k8s/base
```

Remote HTTP reference example:

```bash
kubectl apply -k k8s/examples/http-reference
```

This example uses an HTTPS GitHub reference to consume the stack and is defined in:
- `k8s/examples/http-reference/kustomization.yaml`

The example also generates `statement-fetcher-backend-secrets` via `secretGenerator`.
Replace placeholder values for:
- `PLAID_CLIENT_ID`
- `PLAID_SECRET`
- `PSF_ENCRYPTION_SECRET`

### 4. Verify

```bash
kubectl get deploy,svc,ingress
kubectl rollout status deploy/statement-fetcher-backend
kubectl rollout status deploy/statement-fetcher-frontend
```

## GitHub Publication and Release

Workflows:
- `PR Validation` (`.github/workflows/pr-validation.yml`)
  - Backend lint/tests
  - Frontend build
  - Backend/frontend container build (no push)
  - Kustomize render check
- `Publish Artifacts` (`.github/workflows/publish-artifacts.yml`)
  - On `main` pushes, publishes backend/frontend images to GHCR
  - Uploads rendered Kubernetes manifests as workflow artifact
- `Release` (`.github/workflows/release.yml`)
  - On `v*` tags, publishes versioned + latest images
  - Creates GitHub release with:
    - rendered manifest bundle
    - k8s base tarball
    - checksums

### Required GitHub permissions/secrets

- `GITHUB_TOKEN` with package write permission (workflow permissions already set)
- No extra registry secret is needed for GHCR in the same repo/org when using `GITHUB_TOKEN`

### Release process

```bash
git tag v0.1.0
git push origin v0.1.0
```

This triggers the `Release` workflow.

## Using the Service

1. Open frontend URL.
2. Link an institution from the Home page via either "Link via Plaid" or "Link via
   Yodlee" — each linked institution remembers which provider it used.
3. Manage aliases or remove accounts on Account Details page.
4. Start sync from Sync page and watch logs.
5. Adjust non-secret runtime options in Service Configuration page.

## Security and Production Notes

- Access tokens are stored plaintext in SQLite by design currently.
- Run backend PVC on encrypted storage class for production.
- Restrict ingress host/TLS to real domain and managed certs.
- Consider network policies limiting frontend<->backend and egress to Plaid only.
- Consider external secret manager integration for Plaid credentials.
