"""Run or remove the repository-isolated founder-preview harness."""

import argparse
import subprocess
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


def run() -> None:
    compose("down", "--volumes", "--remove-orphans")
    result = compose(
        "run",
        "--build",
        "--rm",
        "journeys",
        check=False,
    )
    if result.returncode != 0:
        fail(
            "Local preview journey failed. The isolated app remains available "
            "for inspection; run this script with 'down' when finished."
        )
    print(
        "Local preview journeys passed. The isolated preview remains at "
        "http://127.0.0.1:18080; run this script with 'down' to remove it."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "down"))
    arguments = parser.parse_args()
    if arguments.action == "run":
        run()
    else:
        compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
