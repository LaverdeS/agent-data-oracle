"""Exercise founder browser and delegated-agent journeys against local preview."""

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError


def require(condition: bool, outcome: str) -> None:
    if not condition:
        raise RuntimeError(outcome)


def totp_code(secret: str) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def claim_sign_in_link(
    page: Page,
    *,
    base_url: str,
    recipient: str,
    preview_secret: str,
    expected_status: int,
) -> str | None:
    response = page.request.post(
        f"{base_url}/_local/sign-in-links/claim",
        headers={"Authorization": f"Bearer {preview_secret}"},
        data={"recipient": recipient},
    )
    require(
        response.status == expected_status,
        "unexpected local sign-in capture status",
    )
    if expected_status != 200:
        return None
    document = response.json()
    sign_in_url = document.get("sign_in_url") if isinstance(document, dict) else None
    require(isinstance(sign_in_url, str), "local sign-in capture omitted its link")
    return sign_in_url


def allow_only_application(context: BrowserContext, base_url: str) -> list[str]:
    expected = urlsplit(base_url)
    blocked: list[str] = []

    def guard(route: Route) -> None:
        request = route.request
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) == (
            expected.scheme,
            expected.netloc,
        ):
            route.continue_()
        else:
            blocked.append(request.url)
            route.abort()

    context.route("**/*", guard)
    return blocked


def submit_sign_in(
    page: Page, *, base_url: str, recipient: str, preview_secret: str
) -> None:
    page.goto(f"{base_url}/sign-in")
    page.get_by_label("Founder preview access code").fill(preview_secret)
    page.get_by_label("Work email").fill(recipient)
    page.get_by_role("button", name="Email my secure link").click()
    require(
        page.get_by_role("heading", name="Check your email").is_visible(),
        "sign-in request did not reach its generic response",
    )


def open_captured_sign_in(page: Page, *, base_url: str, sign_in_url: str) -> None:
    verification = urlsplit(sign_in_url)
    try:
        page.goto(f"{base_url}{verification.path}?{verification.query}")
    except PlaywrightError:
        raise RuntimeError("captured sign-in navigation failed") from None
    page.evaluate("history.replaceState(null, '', '/auth/verify')")


def run() -> None:
    base_url = os.environ["APP_BASE_URL"].rstrip("/")
    recipient = os.environ["FOUNDER_EMAIL"]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--disable-features=HttpsFirstBalancedModeAutoEnable,HttpsUpgrades",
                "--no-proxy-server",
            ]
        )
        context = browser.new_context()
        blocked = allow_only_application(context, base_url)
        page = context.new_page()
        page.set_default_timeout(10_000)
        access_response = page.request.post(f"{base_url}/_local/preview-access")
        require(
            access_response.status == 200,
            "local preview harness did not expose access over loopback",
        )
        access_document = access_response.json()
        preview_secret = (
            access_document.get("preview_access_secret")
            if isinstance(access_document, dict)
            else None
        )
        require(
            isinstance(preview_secret, str),
            "local preview harness omitted its access code",
        )
        assert isinstance(preview_secret, str)

        page.goto(base_url)
        require(
            page.get_by_text("Founder-only development preview").is_visible(),
            "preview boundary copy is absent",
        )
        require(
            page.get_by_text("local development environment", exact=False).is_visible(),
            "local provider disclosure is absent",
        )

        submit_sign_in(
            page,
            base_url=base_url,
            recipient=recipient,
            preview_secret="rejected-preview-secret",
        )
        require(
            claim_sign_in_link(
                page,
                base_url=base_url,
                recipient=recipient,
                preview_secret=preview_secret,
                expected_status=404,
            )
            is None,
            "rejected preview sign-in created a delivery",
        )

        submit_sign_in(
            page,
            base_url=base_url,
            recipient="outside-preview@example.com",
            preview_secret=preview_secret,
        )
        require(
            claim_sign_in_link(
                page,
                base_url=base_url,
                recipient="outside-preview@example.com",
                preview_secret=preview_secret,
                expected_status=404,
            )
            is None,
            "non-allowlisted preview recipient created a delivery",
        )

        submit_sign_in(
            page,
            base_url=base_url,
            recipient=recipient,
            preview_secret=preview_secret,
        )
        sign_in_url = claim_sign_in_link(
            page,
            base_url=base_url,
            recipient=recipient,
            preview_secret=preview_secret,
            expected_status=200,
        )
        require(sign_in_url is not None, "admitted preview sign-in was not captured")
        assert sign_in_url is not None
        open_captured_sign_in(page, base_url=base_url, sign_in_url=sign_in_url)
        page.get_by_role("button", name="Continue securely").click()

        replay_context = browser.new_context()
        replay_blocked = allow_only_application(replay_context, base_url)
        replay_page = replay_context.new_page()
        open_captured_sign_in(replay_page, base_url=base_url, sign_in_url=sign_in_url)
        replay_page.get_by_role("button", name="Continue securely").click()
        require(
            replay_page.get_by_role(
                "heading", name="This sign-in link is unavailable"
            ).is_visible(),
            "captured sign-in link could be redeemed twice",
        )
        replay_context.close()
        blocked.extend(replay_blocked)

        page.get_by_label("I am reviewing as a business operator").check()
        page.get_by_label("These products are sold into the United States").check()
        page.get_by_role("button", name="Record declaration").click()
        require(
            page.get_by_role("heading", name="Your evidence queues").is_visible(),
            "founder did not reach the authenticated shell",
        )

        page.get_by_role("link", name="Founder controls").click()
        require(
            page.get_by_role("heading", name="Set up your second factor").is_visible(),
            "founder second-factor enrollment was not required",
        )
        totp_secret = page.locator("[data-totp-secret]").get_attribute(
            "data-totp-secret"
        )
        require(totp_secret is not None, "founder enrollment omitted its TOTP secret")
        page.get_by_label("Six-digit authenticator code").fill(totp_code(totp_secret))
        page.get_by_role("button", name="Verify and create recovery codes").click()
        require(
            page.get_by_role("heading", name="Save your recovery codes").is_visible(),
            "founder second-factor enrollment failed",
        )
        page.get_by_role("link", name="Continue to founder controls").click()

        page.goto(f"{base_url}/queues/new")
        page.get_by_label("Type").first.select_option("model")
        page.get_by_label("Exact submitted value").first.fill("HANS0002")
        page.get_by_label("I am authorized to submit these identifier values").check()
        page.get_by_role("button", name="Create evidence queue").click()
        queue_url = page.url
        require(
            page.get_by_role("heading", name="Founder review pending").is_visible(),
            "candidate queue bypassed founder audit",
        )

        page.goto(f"{base_url}/founder")
        page.get_by_role("link", name="Inspect queue", exact=False).click()
        require(
            page.get_by_text("model: HANS0002", exact=True).is_visible(),
            "audit omitted match evidence",
        )
        page.get_by_role(
            "button", name="Approve and release this exact evaluation"
        ).click()
        page.goto(queue_url)
        require(
            page.get_by_role(
                "heading", name="Possible recall-to-listing action records"
            ).is_visible(),
            "approved evidence queue was not released",
        )
        evidence_path = page.get_by_role(
            "link", name="Official CPSC notice"
        ).get_attribute("href")
        require(evidence_path is not None, "released candidate omitted source action")
        evidence_response = page.request.get(
            f"{base_url}{evidence_path}", max_redirects=0
        )
        require(
            evidence_response.status == 303,
            "source evidence action did not redirect",
        )
        require(
            evidence_response.headers.get("location", "").startswith(
                "https://www.cpsc.gov/"
            ),
            "source evidence action did not retain its official authority",
        )
        page.get_by_label("Your review outcome").select_option("not_sure")
        page.get_by_role("button", name="Record acknowledgement").click()
        require(
            page.get_by_text("Current human acknowledgement: not sure").is_visible(),
            "human review acknowledgement was not recorded",
        )

        page.goto(f"{base_url}/agent-keys")
        for scope in (
            "queues:submit",
            "queues:read",
            "evidence:read",
            "reviews:report-agent",
        ):
            page.get_by_label(scope).check()
        page.get_by_role("button", name="Create agent key").click()
        agent_secret = page.locator("#agent-key-secret").text_content()
        require(agent_secret is not None, "agent key secret was not shown once")
        authorization = {"Authorization": f"Bearer {agent_secret}"}
        create_queue_response = page.request.post(
            f"{base_url}/api/v1/queues",
            headers={**authorization, "Idempotency-Key": "local-agent-journey-0001"},
            data={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
        )
        require(
            create_queue_response.status == 201,
            "scoped agent could not submit a queue",
        )
        created_queue_payload = create_queue_response.json()
        evaluation_id = created_queue_payload.get("evaluation_id")
        require(isinstance(evaluation_id, str), "agent queue omitted evaluation id")
        require(
            created_queue_payload.get("contract_version") == "v1",
            "agent queue omitted the versioned evidence contract",
        )
        created_evidence = created_queue_payload.get("evidence")
        require(
            isinstance(created_evidence, dict)
            and created_evidence.get("outcome") == "no_candidates"
            and bool(created_evidence.get("limitations")),
            "agent queue omitted mandatory no-candidate evidence",
        )
        queue_api = f"{base_url}/api/v1/queues/{evaluation_id}"
        retrieve_queue_response = page.request.get(queue_api, headers=authorization)
        require(
            retrieve_queue_response.status == 200,
            "scoped agent could not retrieve its queue",
        )
        retrieved_queue_payload = retrieve_queue_response.json()
        require(
            retrieved_queue_payload.get("contract_version") == "v1"
            and retrieved_queue_payload.get("evaluation_id") == evaluation_id
            and retrieved_queue_payload.get("evidence") == created_evidence,
            "agent retrieval changed the authoritative evidence contract",
        )
        retrieve_evidence_response = page.request.get(
            f"{queue_api}/evidence", headers=authorization
        )
        require(
            retrieve_evidence_response.status == 200,
            "scoped agent could not retrieve evidence",
        )
        require(
            retrieve_evidence_response.json().get("evidence") == created_evidence,
            "agent evidence retrieval changed the authoritative contract",
        )
        report_review_response = page.request.post(
            f"{queue_api}/reviews",
            headers=authorization,
            data={"outcome": "not_sure"},
        )
        require(
            report_review_response.status == 201
            and report_review_response.json()
            == {
                "outcome": "not_sure",
                "report_type": "agent_review",
                "status": "recorded",
            },
            "scoped agent could not report its review",
        )
        page.get_by_role("button", name="Revoke").click()
        require(
            page.request.get(queue_api, headers=authorization).status == 401,
            "revoked agent key remained usable",
        )

        require(not blocked, "browser attempted a non-local network request")
        context.close()
        browser.close()

    print("Founder browser and delegated-agent journeys passed.")


if __name__ == "__main__":
    run()
