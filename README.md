# Agent Data Oracle

Machine-first data infrastructure for verified, time-stamped,
decision-ready information consumed by people, AI agents, and applications.
The first bounded product is a CPSC evidence-queue usage-learning phase. It
produces conditional evidence for human review, never a safety, legal, or
compliance verdict.

## Prerequisites

- Python 3.13.4
- [uv](https://docs.astral.sh/uv/) 0.7.15
- Docker with Compose

Python dependencies resolve exactly from the committed `uv.lock` file. The
same package supplies the web runtime, migration command, and short-lived job
runtime.

## Run locally

Start the repository-isolated PostgreSQL service, install the locked
dependencies, apply migrations explicitly, and launch the web process:

```console
docker compose up -d --wait postgres
uv sync --locked
uv run agent-data-oracle migrate
uv run agent-data-oracle web --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080>. Process and database health are separate:

```console
curl http://127.0.0.1:8080/live
curl http://127.0.0.1:8080/ready
```

Local mode uses an in-memory email capture provider and non-secure localhost
cookies. Tests inject that provider to follow passwordless links without ever
printing token values. A deployed environment must set `APP_ENV=production`, a
stable `AUTH_SECRET` of at least 24 bytes, one or more comma-separated
`FOUNDER_EMAILS`, HTTPS, and an injected `GmailApiEmailProvider`. Production
sessions are `Secure`, HTTP-only, same-site cookies; sign-in links expire after
15 minutes and sessions after 12 hours.

The local default database URL targets the Compose service. Set
`DATABASE_URL` to a SQLAlchemy `postgresql+psycopg://` URL in other
environments. Migrations never run implicitly when the web process starts.

Run a named short-lived job from the same package:

```console
uv run agent-data-oracle job database-check
```

Import a recorded CPSC API response without contacting the live source, then
inspect the current completed revision and last run state:

```console
uv run agent-data-oracle job cpsc-import-fixture \
  --fixture tests/fixtures/cpsc/recall-10887.json \
  --observed-at 2026-09-04T00:00:00Z \
  --expected-record-count 1 \
  --source-url 'https://www.saferproducts.gov/RestWebServices/Recall?RecallID=10887&format=json'
uv run agent-data-oracle job cpsc-status
```

The import stores the received response bytes, creates content-addressed recall
versions and immutable observations, and promotes the complete revision and
current projection in one PostgreSQL transaction. The required expected count
rejects truncated fixture responses. Rejected input and failed promotion are
recorded but never replace the current completed revision. The status output
contains revision metadata and counts, not source payloads.

## Quality gates

With the Compose database running:

```console
uv run ruff format --check src tests migrations
uv run ruff check src tests migrations
uv run mypy src
uv run pytest
docker build --tag agent-data-oracle:local .
docker compose --profile tools run --rm gitleaks
```

The integration suite uses `TEST_DATABASE_URL` when set and otherwise targets
the local Compose database. CI runs unit, migration, and real-PostgreSQL tests
as distinct gates, then builds the production image and scans the repository
for secrets.

## Container roles

The image starts the web process by default and accepts the same command
overrides used for Cloud Run Jobs:

```console
docker run --rm -p 8080:8080 \
  -e DATABASE_URL=postgresql+psycopg://... \
  agent-data-oracle:local

docker run --rm \
  -e DATABASE_URL=postgresql+psycopg://... \
  agent-data-oracle:local migrate

docker run --rm \
  -e DATABASE_URL=postgresql+psycopg://... \
  agent-data-oracle:local job database-check
```

Application request logs are JSON. Each request receives a server-generated
`X-Correlation-ID`; logs include only method, path, response status, and that
identifier by default—never request bodies or query values.
