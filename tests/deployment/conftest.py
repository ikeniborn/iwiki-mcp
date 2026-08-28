from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest


REPOSITORY = Path(__file__).parents[2]


def run_checked(args, *, timeout=120, **kwargs):
    return subprocess.run(
        args,
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
        **kwargs,
    )


@pytest.fixture(scope="session")
def compose_command():
    candidates = (["docker", "compose"], ["docker-compose"])
    failures = []
    for candidate in candidates:
        if shutil.which(candidate[0]) is None:
            failures.append(f"{candidate[0]} missing")
            continue
        try:
            result = run_checked([*candidate, "version"], timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{' '.join(candidate)}: {type(exc).__name__}")
            continue
        if result.returncode == 0:
            return candidate
    pytest.skip("functional Docker Compose unavailable: " + "; ".join(failures))


@pytest.fixture(scope="session")
def rendered_compose(compose_command):
    try:
        result = run_checked(
            [
                *compose_command,
                "--env-file",
                "tests/deployment/fixtures/render.env",
                "config",
                "--format",
                "json",
            ],
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Compose rendering failed with exit code {exc.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("Compose command did not return valid JSON")


@pytest.fixture(scope="session")
def docker_command():
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI unavailable")
    try:
        run_checked(["docker", "info"], timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"Docker daemon unavailable: {type(exc).__name__}")
    return ["docker"]


@pytest.fixture(scope="session")
def acceptance_image(docker_command, compose_command):
    tag = f"iwiki-mcp:acceptance-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        run_checked(
            [
                *compose_command,
                "--env-file",
                "tests/deployment/fixtures/render.env",
                "build",
                "iwiki",
            ],
            timeout=300,
        )
        run_checked(
            [*docker_command, "tag", "iwiki-mcp-iwiki:latest", tag],
            timeout=20,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"acceptance image build failed with exit code {exc.returncode}")
    yield tag
    subprocess.run(
        [*docker_command, "image", "rm", "-f", tag],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# Reuse the repository's disposable PostgreSQL fixtures without adding a
# database service to the application Compose project.
from tests.postgres.conftest import (  # noqa: E402,F401
    clean_postgres,
    hosted_runtime,
    postgres_dsn,
    store_factory,
)
