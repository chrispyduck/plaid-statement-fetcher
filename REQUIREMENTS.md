# Statement Fetcher Requirements

## Product Scope

Build a production-ready web application that:
- links institutions/accounts via Plaid Link,
- syncs and downloads account statements,
- persists service state locally,
- supports deployment as split frontend/backend services.

## Functional Requirements

### Frontend

- Home page:
  - show linked accounts,
  - start Plaid link flow,
  - navigate to account management and sync pages.
- Account details/config page:
  - show non-secret account/institution metadata,
  - edit alias with save feedback,
  - remove account with explicit confirmation,
  - show account event history.
- Sync page:
  - start statement sync,
  - display job summary and detailed logs.
- Service configuration page:
  - edit non-secret runtime settings.

### Backend API

- Link token create/exchange endpoints.
- Account list/detail/alias/remove endpoints.
- Sync start/status/jobs endpoints.
- Event query endpoint.
- Service config read/update endpoints.

### Persistence

- Use SQLite (single file) for:
  - linked items/accounts,
  - downloaded statements,
  - events,
  - service config overrides.
- Store PDF outputs in filesystem under configured output root.

## Operational Requirements

- One runtime mode flag: `PLAID_ENV=sandbox|production`.
- Backend and frontend must be containerized separately.
- Kubernetes manifests must be split into backend/frontend components with distinct labels.
- Ingress must route API/callback paths to backend and SPA routes to frontend.
- Include PR validation, artifact publication, and release workflows in GitHub Actions.

## Security/Compliance Baseline

- No plaintext secrets in repository manifests.
- Provide secret template and environment-based secret injection.
- Persist runtime data on mounted storage volume in Kubernetes.

## Documentation Requirements

Documentation must cover:
- local development setup,
- runtime configuration,
- container build/run for both services,
- Kubernetes deployment and verification,
- GitHub publish/release workflow.
