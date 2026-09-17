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


def test_source_refresh_job_exposes_daily_and_weekly_modes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_data_oracle", "job", "cpsc-refresh", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "daily" in result.stdout
    assert "weekly" in result.stdout


def test_data_cleanup_and_incident_hold_jobs_are_exposed() -> None:
    cleanup = subprocess.run(
        [sys.executable, "-m", "agent_data_oracle", "job", "data-cleanup", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    hold = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_data_oracle",
            "job",
            "data-retention-hold",
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert cleanup.returncode == 0
    assert "--now" in cleanup.stdout
    assert hold.returncode == 0
    assert "--reason-category" in hold.stdout
