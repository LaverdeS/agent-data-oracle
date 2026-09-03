import subprocess
import sys


def test_package_exposes_web_migration_and_named_job_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_data_oracle", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "web" in result.stdout
    assert "migrate" in result.stdout
    assert "job" in result.stdout
