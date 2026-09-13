"""Exercise one local-preview agent key without exposing it in a command line."""

import argparse
import getpass
import json
import secrets
import urllib.error
import urllib.request
from typing import Never

BASE_URL = "http://127.0.0.1:18080"


def fail(message: str) -> Never:
    raise SystemExit(message)


def request(*, path: str, method: str, key: str, payload: object) -> tuple[int, object]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    http_request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Idempotency-Key": secrets.token_urlsafe(24),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(http_request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def active_key_journey(key: str) -> None:
    status, created = request(
        path="/api/v1/queues",
        method="POST",
        key=key,
        payload={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
    )
    if status != 201 or not isinstance(created, dict):
        fail("The local agent key did not create an evidence queue.")
    evaluation_id = created.get("evaluation_id")
    if not isinstance(evaluation_id, str):
        fail("The local agent queue response omitted its evaluation identifier.")

    status, evidence = request(
        path=f"/api/v1/queues/{evaluation_id}/evidence",
        method="GET",
        key=key,
        payload={},
    )
    if status != 200 or not isinstance(evidence, dict):
        fail("The local agent key could not retrieve the evidence contract.")

    status, review = request(
        path=f"/api/v1/queues/{evaluation_id}/reviews",
        method="POST",
        key=key,
        payload={"outcome": "not_sure"},
    )
    if status != 201 or not isinstance(review, dict):
        fail("The local agent key could not report its agent review.")
    print(f"Scoped local agent API journey passed for evaluation {evaluation_id}.")


def revoked_key_check(key: str) -> None:
    status, _ = request(
        path="/api/v1/queues",
        method="POST",
        key=key,
        payload={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
    )
    if status != 401:
        fail("The local agent key was not rejected after revocation.")
    print("Revoked local agent key was rejected.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-revoked", action="store_true")
    arguments = parser.parse_args()
    key = getpass.getpass("Paste the one-time agent key (input is hidden): ")
    if not key:
        fail("An agent key is required.")
    if arguments.expect_revoked:
        revoked_key_check(key)
    else:
        active_key_journey(key)


if __name__ == "__main__":
    main()
