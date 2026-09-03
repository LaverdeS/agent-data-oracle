import json
import subprocess
import sys

import pytest


@pytest.mark.integration
def test_named_database_check_job_uses_postgresql(postgres_url: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_data_oracle",
            "job",
            "database-check",
            "--database-url",
            postgres_url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    assert event["level"] == "INFO"
    assert event["message"] == "database_check_completed"
