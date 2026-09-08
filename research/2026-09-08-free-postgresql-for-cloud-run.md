# Free PostgreSQL alternatives for Cloud Run

**Research date:** 2026-09-08
**Question:** Can a no-cost managed PostgreSQL service replace Cloud SQL for the
public Cloud Run prototype without sacrificing the agreed production posture?

## Decision

**Do not replace Cloud SQL with a free database tier for the public phase.** A
free external PostgreSQL database is technically usable from Cloud Run, but it
is not a stable, production-ready substitute for this repository's approved
topology. It would make the authoritative evidence store depend on a provider
that can pause/suspend it, has small hard limits, and lacks the required backup
and recovery controls. It would also introduce a second hosting vendor and a
new public network path.

This is not a claim that Neon or Supabase are bad products. Their paid tiers can
be reasonable managed-Postgres choices after a deliberate architecture,
privacy/transfer, backup/restore, cost, and operational review. The conclusion
is specifically about their **free** tiers and this bounded public service.

The existing decision is clear: PostgreSQL is the authoritative evidence store;
the initial provider/topology is Cloud SQL in Frankfurt; and production must
not rely on a trial credit or free database tier. See
[`docs/technical-grilling-handoff.md`](../docs/technical-grilling-handoff.md)
(Q11, Q12, Q20 and the operational acceptance gates). Replacing it is therefore
not a cost-only Terraform tweak; it needs a recorded decision before changing
#21's foundation.

## What Cloud SQL currently costs and what is actually free

The configured `db-f1-micro` is a shared-core PostgreSQL machine with about
0.614 GB RAM and no Cloud SQL SLA. The current Frankfurt shared-core list price
shown by Google is $0.0105/hour, about **$7.67 for 730 instance-hours**, before
storage, backups/PITR log storage, network, and any other service charges.
Google says Cloud SQL pricing also includes storage and networking, so use the
pricing calculator immediately before provisioning rather than treating that
number as a monthly cap. [Cloud SQL pricing](https://cloud.google.com/sql/pricing)
[machine series](https://docs.cloud.google.com/sql/docs/postgres/machine-series-overview)

Cloud SQL has **no permanent always-free PostgreSQL allowance**. There is one
30-day Cloud SQL trial instance per new project, with billing enabled; a new
Google Cloud customer can separately have a 90-day/$300 welcome-credit trial.
Neither is a basis for a continuing public service. [Cloud SQL trial
instance](https://docs.cloud.google.com/sql/docs/postgres/free-trial-instance)
[Google Cloud Free Program](https://docs.cloud.google.com/free/docs/free-cloud-features)

Stopping Cloud SQL suspends instance charges, but the database stops accepting
connections and storage/IP charges continue. That is useful for a short live
test followed by a deliberate pause; it is not a live public site.
[Start/stop instances](https://docs.cloud.google.com/sql/docs/postgres/start-stop-restart-instance)
Cloud SQL for PostgreSQL is available in `europe-west3` (Frankfurt).
[Region availability](https://docs.cloud.google.com/sql/docs/postgres/region-availability-overview)

## Credible free external PostgreSQL services

| Service | Relevant free-tier facts | Frankfurt and Cloud Run fit | Why it fails the public-phase bar |
| --- | --- | --- | --- |
| **Neon Free** | 0.5 GB storage, 100 CU-hours/project/month, 5 GB public egress, six-hour restore window, community support. Its compute scales to zero after five idle minutes; Free users cannot disable it. [Pricing](https://neon.com/pricing) [scale-to-zero](https://neon.com/docs/introduction/scale-to-zero) | Frankfurt (`aws eu-central-1`) is supported. A TLS PostgreSQL connection string can be kept in Secret Manager, and Cloud Run can reach a non-VPC public endpoint by default. For serverless connection volume, Neon offers a pooled URL. [Neon Frankfurt status](https://neon.com/docs/introduction/status) [TLS connection](https://neon.com/docs/connect/query-with-psql-editor) [pooling](https://neon.com/docs/connect/connection-pooling) [Cloud Run egress](https://docs.cloud.google.com/run/docs/securing/security) | A query wakes a stopped compute, so it is usable for a demo, but an idle session is terminated and must reconnect. Free capacity/egress can be exhausted; no Free-plan SLA is offered (the SLA is for Business/Scale). Its restore, support, and network controls do not meet the agreed backup/recovery and stable-service posture. [connection behavior](https://neon.com/docs/connect/connection-errors) [SLA](https://neon.com/sla) |
| **Supabase Free** | 500 MB database, shared CPU/500 MB RAM, 5 GB egress, and no automatic backups or point-in-time recovery. [Pricing](https://supabase.com/pricing) | Exact Frankfurt (`aws eu-central-1`) is offered. Connection is possible, but Free direct database access is IPv6-only; its shared pooler is the practical IPv4/serverless option, with session and transaction modes having different semantics. [Regions](https://supabase.com/docs/guides/platform/regions) [connections](https://supabase.com/docs/guides/database/connecting-to-postgres) | Free projects can be paused after one week of low activity, then need a dashboard restore; Supabase says Pro prevents that. Free backups cannot be downloaded. This is expressly unsuitable for an authoritative, continuously reachable store. [production checklist](https://supabase.com/docs/guides/deployment/going-into-prod) |

## Is the change easy?

**At the application-protocol level, mostly yes:** this app already uses
PostgreSQL/SQLAlchemy. A TLS external `DATABASE_URL` in Secret Manager, updated
connection/pooling settings, and real integration/migration tests would get a
Cloud Run service talking to either provider.

**At the deployment and operating level, no:** the current Terraform attaches
Cloud Run and the migration job to a Cloud SQL Unix socket, grants Cloud SQL
IAM roles, and creates/backs up the Cloud SQL instance. An external database
requires removing or conditionalising those resources, rotating the connection
secret, revising the provisioning wizard, and redoing deployment, migration,
failure, restore, and privacy/transfer acceptance evidence. It also creates
cross-provider public egress. Cloud Run's default source IP is dynamic; if an
external database requires IP allow-listing, static egress needs a VPC plus
Cloud NAT, which itself has ongoing cost. [Cloud Run static egress
guidance](https://docs.cloud.google.com/run/docs/configuring/static-outbound-ip)

## Practical recommendation

For a **non-public technical demonstration**, the 30-day Cloud SQL trial or
Google welcome credit can be useful if eligible; test and then stop or tear
down deliberately. The trial itself has no SLA or backups, so it does not pass
the repository's public-phase gates. For a service that stays live, keep the
small paid Cloud SQL instance and an alert/controlled-pause limit. Do not ship
the public phase on Neon Free or Supabase Free. A paid external PostgreSQL
evaluation can be proposed later as a separate architecture decision, not as a
shortcut within #21.
