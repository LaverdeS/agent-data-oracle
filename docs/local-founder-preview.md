# Local founder preview

This repeatable founder-only development preview is local product work: it
does not publish the application, activate the usage-learning phase, deliver
external email, or create hosting evidence.

## Start a manual preview

With Python and Docker Desktop with Compose, run this single command from the
repository root:

```console
python scripts/local_preview.py manual
```

It resets only the `agent-data-oracle-preview` project, builds the production
image, starts an ephemeral PostgreSQL 17.6 database, migrates it, imports the
recorded `recall-10887.json` fixture without a live request, and waits for the
application and proxy. Open <http://127.0.0.1:18080> in a host browser.

## Manual founder walkthrough

Use a fresh `manual` run—automated journeys change the ephemeral data.

1. Open the local address and select **Sign in**. Wait for **Local browser
   admission is ready.** No access code is shown or copied: the proxy gives the
   browser a short-lived, HttpOnly, in-memory admission.
2. Enter `founder@example.com` and select **Email my secure link**. The local
   page opens the captured single-use link without placing its token in a shell
   command or browser history. Select **Continue securely**.
3. Make both declarations and select **Record declaration**.
4. Open **Founder controls**. Enrol the TOTP secret in an authenticator,
   verify its code, and store recovery codes outside the preview. Do not
   screenshot any code or recovery value.
5. Open **New evidence queue**, select `model`, enter `HANS0002`, confirm the
   authorization checkbox, and create the queue. It must show **Founder review
   pending**.
6. Return to **Founder controls**, inspect the submitted literal and match
   basis, and approve the exact immutable evaluation. Reopen the queue: it
   must show **Possible recall-to-listing action records**. Select **Official
   CPSC notice** only when intentionally opening the official authority in the
   host browser; the application never follows that redirect.
7. Select `not sure` and record the human acknowledgement.
8. Open **Agent keys**, select `queues:submit`, `queues:read`,
   `evidence:read`, and `reviews:report-agent`, then create a key. Copy it only
   into the hidden prompt of this helper—never into a command line, screenshot,
   log, or saved file:

```console
python scripts/manual_preview_agent_api.py
```

The helper creates a no-candidate queue, retrieves evidence, and records an
agent review through the scoped API without printing the key. Back in **Agent
keys**, select **Revoke**. Prove immediate revocation by supplying the same key
to the hidden prompt again:

```console
python scripts/manual_preview_agent_api.py --expect-revoked
```

It succeeds only on a `401` response.

## Automated Docker Desktop regression

Run the deterministic headless journey separately:

```console
python scripts/local_preview.py run
```

It first checks the host-loopback UI and drives a pinned Chromium browser
through the manual proxy admission, then runs the existing Chromium journey in
the internal application network namespace. Success ends with:

```text
Founder browser and delegated-agent journeys passed.
Local preview journeys passed.
```

The journey retains coverage for rejected admission and recipients,
passwordless sign-in and replay rejection, declaration, TOTP, candidate audit
and release, evidence action, acknowledgement, scoped API use, and revocation.

## Isolation and image roles

The application-side network is `internal: true`. Only the `manual` proxy also
joins a separate ingress network so Docker Desktop can publish its loopback
port; neither `app` nor `postgres` joins that ingress network.

| Service | Role | Host exposure |
| --- | --- | --- |
| `manual` | Nginx local-only proxy with the manual-preview marker | `127.0.0.1:18080` only |
| `app` | Production runtime image and local-only harness | None |
| `postgres` | In-memory, ephemeral database | None |
| `manual-check` | One-off Chromium check of proxy admission | None |
| `journeys` | One-off Playwright regression browser | None |

The proxy is selected over publishing `app` directly: Docker Desktop preserves
the loopback binding when the stack starts with `compose up`, while proxy
ingress stays distinguishable from the isolated journey and leaves `app` on the
internal-only network. Direct `app` publication was rejected because it would
place the application on ingress and cannot safely provide the browser-only
handoff. A separate SMTP inbox was rejected because it adds a transport and
persistent inbox to a preview with no external delivery.

Preview codes and admissions are generated in process. Only admission digests
are retained in memory for five minutes; the admission is consumed when the
sign-in link is claimed. Secrets and links are not printed, committed, placed
in container environment/metadata, stored in PostgreSQL, or included in
request logs. The manual cookie is HttpOnly, SameSite strict, and a browser
session cookie. `/_local/*` routes require the local harness and are omitted
from the API schema, so production routes remain absent.

## Cleanup

Remove only harness-owned containers, network, and ephemeral data:

```console
python scripts/local_preview.py down
```

The ordinary developer database in `compose.yaml` is a different project,
port, and persistent volume, so this command does not remove it. Optionally
remove preview images after cleanup:

```console
docker image rm agent-data-oracle-preview-journeys:latest
docker image rm agent-data-oracle:local-preview
docker image rm nginx:1.27.5-alpine
```

This preview does not prove hosted IAM, secrets, TLS/cookie behavior, external
email, observability, backup/restore, deployment, public traffic, or
product-market claims. Provider disclosure fails closed outside local/test.
