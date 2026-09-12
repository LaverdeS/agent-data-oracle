# Founder-only Cloud Run preview checklist

**Founder-approved plan, 2026-09-11. Implementation and deployment evidence are
pending in [#21](https://github.com/LaverdeS/agent-data-oracle/issues/21).**
This preview is for founder testing before continued product development. It
admits no public operators and collects no usage-learning validation traffic.
The superseded `scripts/provision-gcp.sh` paid-domain wizard has been retired:
**do not retrieve an older revision and resume Stage 4 or its production-OAuth
steps**. This document is the current human-operated checklist.

## 1. Preserve completed work and verify the foundation

Keep the founder's completed Stages 1–3 and ignored `.provisioning.env`. Never
print or commit that file. Do not recreate projects, billing, GitHub protections
or Terraform state without inspecting what already exists. The GCP project and
consumer-Gmail sender belong to separate founder-controlled accounts; redact
both addresses in all evidence.

The September 11 read-only GitHub check found remote `main` at `ca785e3` and no
deployment-workflow runs. Local commits `efaee9d` and `9082c1d` remain unpushed.
The latter's domain route is superseded; edit forward in a later implementation
session. GCP resource inspection failed on certificate validation, so neither
resource absence nor running/parked state has been verified. Resolve that read
failure without disabling certificate verification before relying on GCP state.

Before billable provisioning, record the actual resource inventory and current
Frankfurt costs: request-based Cloud Run, zonal `db-f1-micro` Cloud SQL with
10 GiB SSD, backups/PITR and IP allocation, plus secrets, artifacts, build and
other retained resources. Verify actual settings rather than assuming defaults.
Choose a preview spending limit, review interval and pause owner; the deferred
usage-learning phase's clock and cash ledger must not be activated to do this.
Cloud Run's free allowance is not a promise of free hosting. Exact Frankfurt
prices remain unverified; do not use an unrelated region's quote.
[Cloud Run pricing](https://cloud.google.com/run/pricing),
[Cloud SQL pricing](https://cloud.google.com/sql/pricing).

## 2. Implement and test founder admission before deploying

#21 provides both a founder-held `PREVIEW_ACCESS_SECRET` gate before mail
admission and an explicit `PREVIEW_RECIPIENT_EMAILS` founder allowlist. An
allowlist alone allows strangers who know a founder address to request mail to
that address. `FOUNDER_EMAILS` assigns privileges separately; every preview
recipient must also be present there.

Missing configuration must fail closed. Rejected requests retain the generic
response and create no deliverable token, operator or delivery-cap reservation.
Test magic-link redemption, existing sessions, removal from the allowlist and
founder-owned API keys, including direct access that bypasses the UI. Keep email
verification, founder TOTP, CSRF, secure cookies, expiry, ownership and scopes.
Use a separate preview access mechanism without weakening ordinary authentication.
All preview access secrets belong in regional Secret Manager, not URLs or logs.

Preserve the atomic 100-admitted-delivery rolling-24-hour cap, per-address/network
limits and bounded retries. Verify that founder tests do not start a phase,
emit validation events or consume real-batch/first-20-real-queue audit allowances.
Keep immutable test evidence and diagnostic/security records; test isolation
must not bypass audit, source freshness, integrity or pause safeguards.

## 3. Temporary Gmail authorization — human-operated

No domain purchase, public branding pages, Workspace subscription or production
OAuth publication is a preview prerequisite. Use only the separate controlled
consumer-Gmail sender, with its two-step verification and recovery checked.

The human configures the Google Auth Platform audience as **External**, leaves
publishing status in **Testing**, adds only the sender as a test user and requests
only `https://www.googleapis.com/auth/gmail.send`. Only the sender grants Google
access; magic-link recipients do not. A temporary test OAuth client and the
sender grant are human-operated; this checklist authorizes no automatic account
or credential creation.

A Gmail-scoped Testing refresh token expires after **seven days** and may fail
earlier if revoked. Record the authorization date and human renewal owner, never
the token. Reauthorize for subsequent founder smoke sessions as needed; do not
call this durable or public-ready. If setup cannot proceed in Testing, record
the actual blocker rather than substituting the old domain/production route.
[Google token-expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration),
[Gmail scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes).

Enter the client ID, client secret and refresh token directly into the regional
Secret Manager containers alongside `database-url`, `auth-secret`,
`founder-emails`, `preview-access-secret` and `preview-recipient-emails`. The
deployed environment maps the last two to `PREVIEW_ACCESS_SECRET` and
`PREVIEW_RECIPIENT_EMAILS`. Use the founder's password manager for necessary
secure handling. Never place credentials in `.provisioning.env`,
GitHub, shell history, screenshots, issues, logs, Terraform inputs/state or images.
Gmail processing remains a documented non-Frankfurt exception.

Ordinary local development and CI use the existing capture provider. Planned
Cloud Run testing sessions use Gmail; repeat delivery/failure evidence after
material authentication, provider or deployment changes. Hosted capture would
need a separate safe implementation. Do not set Cloud Run `APP_ENV` to local/test
to bypass Gmail because that also changes security behavior.

## 4. Deploy only after the preview implementation is ready

Retain regional Cloud Run/SQL/Artifact Registry/Secret Manager, zero minimum and
at most two web instances, bounded SQL connections, distinct least-privilege
identities and Google-authenticated encrypted SQL connectivity. Keep GitHub WIF
and the existing manual deployment approval; existing project/environment names
containing `production` do not authorize public use or require recreation.

Use a digest-pinned image, explicit migration-before-traffic, backward-compatible
migrations and a known prior revision for recovery. Use the generated HTTPS
`run.app` origin with tested canonical magic links. An obscure URL is not access
control. A bootstrap revision must not permit sign-in before the correct origin
and founder boundary are established.

The human performs interactive provisioning and handles secrets. This checklist
is sufficient; the superseded 19-stage wizard was removed without reading or
changing the ignored `.provisioning.env` that preserves completed Stages 1–3.

## 5. Record founder-only acceptance and cost posture

Record dated, redacted evidence in #21 for:

- `/live`, `/ready`, founder admission and rejection, secure browser/magic-link
  completion, founder controls and relevant API admission checks.
- Actual Gmail arrival at an approved founder inbox; generic responses alone do
  not prove delivery.
- A controlled invalid-refresh-token secret version/revision, a real attempted
  provider call, generic failure response and redacted failure logs. Use a fresh
  revision so a cached access token cannot conceal the invalid-token condition.
- Immediate recovery to a known valid secret version/revision and successful
  delivery. Never leave the deliberate failure revision serving.
- No phase activation or validation-event/counter contamination from test data.
- Actual running or parked SQL state, recurring cost estimate, preview budget
  owner and restart procedure. A stopped instance cannot serve the application;
  storage and IP charges continue. Account for retained backups and other
  resources instead of treating parking as zero cost.
  [Google SQL stopping behavior](https://docs.cloud.google.com/sql/docs/postgres/start-stop-restart-instance#stop_an_instance).

Load the allowlisted founder inbox and preview secret without placing either in
shell history, export them for the smoke process, then run:

```console
scripts/smoke-deployed-shell.sh https://SERVICE.run.app
```

The revised script submits one rejected request without the founder gate and one
admitted request with it, while keeping the secret off the curl command line. Its
generic responses still do not prove delivery: confirm exactly one new message
arrived, complete that link, and perform the separately documented provider
failure/recovery drill. Unset the smoke variables after the session.

Keep #21 open and do not push until the newly approved evidence is complete.
This implementation performs no deployment, OAuth grant or closure.

## 6. Continue product development; defer publication

#22 source refresh no longer waits for deployment. #29 evidence refresh and #31
core privacy can progress without #30 activation. #30 and #32–#35 remain deferred
public/usage-learning work; their full production certification is not a preview
or ordinary feature-development gate.

After the preview is usable, [#37](https://github.com/LaverdeS/agent-data-oracle/issues/37)
asks the founder to run a grilling session about intelligence features, product
fit, expectations and technology alternatives, then generate aligned specs and
tickets. No intelligence spec or stack is chosen now.

Future publication needs a fresh founder decision, current hosting/domain and
durable-sender research, public security/privacy/operational evidence and separate
activation authorization. Neither a completed preview nor feature development
starts that phase. See [#17](https://github.com/LaverdeS/agent-data-oracle/issues/17)
for scope and [#35](https://github.com/LaverdeS/agent-data-oracle/issues/35) for the
deferred public decision.
