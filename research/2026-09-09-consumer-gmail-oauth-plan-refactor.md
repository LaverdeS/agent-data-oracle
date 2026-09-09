# Consumer Gmail OAuth plan refactor

- **Date:** 2026-09-09
- **Status:** approved by the founder on 2026-09-09; implementation evidence remains pending
- **Recorded in:** [#17](https://github.com/LaverdeS/agent-data-oracle/issues/17#issuecomment-5606597430) and [#21](https://github.com/LaverdeS/agent-data-oracle/issues/21#issuecomment-5606596922)

## Recommendation

Allow one founder-controlled domain **only for Google OAuth branding**. Keep the application itself on its generated `run.app` address, keep the consumer Gmail sender, keep `gmail.send`, and keep Google Workspace out of scope.

This is the smallest repair. It preserves the Gmail adapter already completed in [#20](https://github.com/LaverdeS/agent-data-oracle/issues/20), the deployment work in [#21](https://github.com/LaverdeS/agent-data-oracle/issues/21), and the responsibility split recorded by the [2026-09-08 amendment to #17](https://github.com/LaverdeS/agent-data-oracle/issues/17#issuecomment-5585804841). It changes a provisioning assumption, not the product architecture.

## Why the current plan is blocked

The current plan requires all of these at once:

1. a consumer Gmail sender;
2. only the sensitive `gmail.send` scope;
3. a durable refresh token from an External app set to **In production**; and
4. no custom domain.

Items 1-3 fit together. Google classifies `gmail.send` as a sensitive, send-only scope. An External app left in **Testing** gets a refresh token that expires after seven days, so weekly manual renewal is not a durable production control. [Gmail scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes); [OAuth token lifetime](https://developers.google.com/identity/protocols/oauth2#expiration)

Item 4 conflicts with Google's current publication setup. Google says external production apps need application-domain links, and the homepage must be public, describe the app, link its privacy policy, and be on a verified domain the owner controls. [Google OAuth branding requirements](https://support.google.com/cloud/answer/15549049); [Google production-readiness policy](https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance)

The personal-use exception means this one-sender app does not need to be submitted for verification merely to remove the unverified-app warning. It does **not** turn a seven-day Testing token into a durable token or remove the External/In-production branding setup. [Google verification exceptions](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification#exceptions_to_verification_requirements)

## Proposed ticket and wizard changes

### Specification #17

Add a narrow amendment:

> No Workspace subscription is required. The application remains on `run.app` and has no custom-domain load balancer. One founder-controlled domain is allowed solely for the public OAuth homepage, privacy policy, and terms links required for durable consumer-Gmail authorization.

Keep every other boundary: only the sender grants OAuth; operators only receive email; Gmail remains a named non-Frankfurt transfer; a dedicated transactional sender is still required before a paid, expanded, or ongoing phase.

### Ticket #21

Do not split the ticket. Keep it responsible for:

- the locally implemented, not-yet-pushed atomic 100-admitted-delivery rolling-24-hour cap;
- corrected wizard stages for domain ownership/verification, public branding pages, External audience, only `gmail.send`, In-production publishing, OAuth client creation, and final sender-only authorization;
- Secret Manager storage of client ID, client secret, and refresh token;
- deployment smoke tests, real Gmail delivery, and the invalid-token failure/recovery drill.

Rewrite the wizard so it validates the domain and public-page URLs and stops safely unless the founder confirms them. Later stages verify domain ownership, publish the branding, and only then configure the sender and OAuth client. It must not suggest placeholder URLs, a seven-day Testing token, or OAuth grants by application users.

### Downstream tickets

Leave the dependency chain unchanged. #21 continues to block #22, #32, and #33. The later path remains #22 -> #29 -> #30 -> #31; #32 also waits for #22, #30, and #31; #33 also waits for #31; #32 and #33 feed #34, then #35. #35 should record domain ownership, renewal responsibility, hosting/vendor facts, current Gmail quota/cost facts, sender risk acceptance, and the later sender-migration gate. Treat #18 and #20 as complete.

[#30](https://github.com/LaverdeS/agent-data-oracle/issues/30) remains the **only** owner of phase admission, activation, batch/event limits, and cash controls. Completing #21 must not activate the usage-learning phase.

## Founder decision required

Approve or reject buying/using one founder-controlled domain for OAuth branding only.

- **If approved:** amend #17 and #21, revise the wizard, retain and verify the cap, and resume Stage 4.
- **If rejected:** keep #21 blocked and open a separate sender-architecture decision. SMTP app passwords or another provider would require a broader spec, security, provider, code, and evidence change; weekly Testing-token renewal should not be accepted as the workaround.
