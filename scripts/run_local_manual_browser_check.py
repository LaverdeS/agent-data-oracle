"""Verify the browser-only manual admission through the local proxy."""

import os

from local_preview_browser import allow_only_origin, require
from playwright.sync_api import sync_playwright


def run() -> None:
    base_url = os.environ["APP_BASE_URL"].rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--disable-features=HttpsFirstBalancedModeAutoEnable,HttpsUpgrades",
                "--no-proxy-server",
            ]
        )
        context = browser.new_context()
        blocked = allow_only_origin(
            context,
            base_url,
            allowed_origins=("http://127.0.0.1:18080",),
        )
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.goto(f"{base_url}/sign-in")
        require(
            page.get_by_text("Local browser admission is ready.").is_visible(),
            "manual proxy did not establish browser admission",
        )
        require(
            not page.locator("#preview_access_secret").count(),
            "manual proxy exposed the preview access secret field",
        )
        require(
            page.get_by_role("button", name="Email my secure link").is_enabled(),
            "manual proxy left the sign-in action disabled",
        )
        page.get_by_label("Work email").fill("founder@example.com")
        with page.expect_response(
            lambda response: "/_local/manual/sign-in-links/claim" in response.url
        ) as claimed:
            page.get_by_role("button", name="Email my secure link").click()
        require(
            claimed.value.status == 200,
            f"manual sign-in handoff was rejected ({claimed.value.status})",
        )
        require(not blocked, "manual browser attempted a non-local network request")
        context.close()
        browser.close()
    print("Manual proxy browser check passed.")


if __name__ == "__main__":
    run()
