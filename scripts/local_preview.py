"""Run or remove the repository-isolated founder-preview harness."""

import argparse
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.preview.yaml"


def compose(
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", "compose", "--file", str(COMPOSE_FILE), *arguments],
        cwd=ROOT,
        check=check,
    )


def fail(message: str) -> NoReturn:
    raise SystemExit(message)


def check_manual_preview() -> None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18080/", timeout=5) as page:
            body = page.read()
    except urllib.error.URLError as error:
        fail(f"Local preview host browser check failed: {error.reason}")
    if page.status != 200 or b"Founder-only development preview" not in body:
        fail("Local preview host browser check did not reach the founder UI.")


def start_preview() -> None:
    compose("down", "--volumes", "--remove-orphans")
    compose("up", "--build", "--wait", "app", "manual")
    check_manual_preview()


def run() -> None:
    start_preview()
    result = compose(
        "run",
        "--build",
        "--rm",
        "--no-deps",
        "journeys",
        check=False,
    )
    if result.returncode != 0:
        fail(
            "Local preview journey failed. The isolated app remains "
            "available for inspection; run this script with 'down' when finished."
        )
    manual_result = compose(
        "run",
        "--build",
        "--rm",
        "--no-deps",
        "manual-check",
        check=False,
    )
    if manual_result.returncode != 0:
        fail(
            "Local preview manual browser check failed. The isolated app remains "
            "for inspection; run this script with 'down' when finished."
        )
    print(
        "Local preview journeys passed. The isolated preview remains at "
        "http://127.0.0.1:18080; run this script with 'down' to remove it."
    )


def manual() -> None:
    start_preview()
    print(
        "Local founder preview is ready at http://127.0.0.1:18080; "
        "run this script with 'down' to remove it."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "manual", "down"))
    arguments = parser.parse_args()
    if arguments.action == "run":
        run()
    elif arguments.action == "manual":
        manual()
    else:
        compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
