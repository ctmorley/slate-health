"""Validate Docker Compose and Dockerfile configuration without requiring Docker.

These tests parse the configuration files to catch structural issues early,
even in environments where Docker is not available.
"""

import pathlib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"


@pytest.fixture(scope="module")
def compose_config() -> dict:
    """Load and parse docker-compose.yml."""
    compose_file = REPO_ROOT / "docker-compose.yml"
    assert compose_file.exists(), f"docker-compose.yml not found at {compose_file}"
    return yaml.safe_load(compose_file.read_text())


class TestDockerCompose:
    """Validate docker-compose.yml structure and configuration."""

    def test_postgres_service_exists(self, compose_config):
        assert "postgres" in compose_config["services"]

    def test_postgres_uses_pg16(self, compose_config):
        image = compose_config["services"]["postgres"]["image"]
        assert "16" in image, f"Expected PostgreSQL 16, got: {image}"

    def test_postgres_has_healthcheck(self, compose_config):
        pg = compose_config["services"]["postgres"]
        assert "healthcheck" in pg, "PostgreSQL service missing healthcheck"
        assert "pg_isready" in str(pg["healthcheck"]["test"])

    def test_backend_service_exists(self, compose_config):
        assert "backend" in compose_config["services"]

    def test_backend_depends_on_postgres_healthy(self, compose_config):
        backend = compose_config["services"]["backend"]
        depends = backend.get("depends_on", {})
        assert "postgres" in depends
        pg_dep = depends["postgres"]
        assert pg_dep.get("condition") == "service_healthy", (
            "Backend must wait for postgres to be healthy before starting"
        )

    def test_backend_has_database_url(self, compose_config):
        env = compose_config["services"]["backend"].get("environment", {})
        # environment can be a list or dict
        if isinstance(env, list):
            env_str = " ".join(env)
        else:
            env_str = " ".join(f"{k}={v}" for k, v in env.items())
        assert "SLATE_DATABASE_URL" in env_str, "Backend missing SLATE_DATABASE_URL"
        assert "asyncpg" in env_str, "Database URL should use asyncpg driver"

    def test_backend_has_healthcheck(self, compose_config):
        backend = compose_config["services"]["backend"]
        assert "healthcheck" in backend, "Backend service missing healthcheck"

    def test_backend_exposes_port_8000(self, compose_config):
        ports = compose_config["services"]["backend"].get("ports", [])
        port_strs = [str(p) for p in ports]
        assert any("8000" in p for p in port_strs), "Backend should expose port 8000"

    def test_pgdata_volume_defined(self, compose_config):
        volumes = compose_config.get("volumes", {})
        assert "pgdata" in volumes, "pgdata volume should be defined for persistence"

    # ── Temporal service tests ─────────────────────────────────────────

    def test_temporal_service_exists(self, compose_config):
        assert "temporal" in compose_config["services"], (
            "Temporal server service must be defined in docker-compose.yml"
        )

    def test_temporal_exposes_port_7233(self, compose_config):
        ports = compose_config["services"]["temporal"].get("ports", [])
        port_strs = [str(p) for p in ports]
        assert any("7233" in p for p in port_strs), "Temporal should expose port 7233"

    def test_temporal_has_healthcheck(self, compose_config):
        temporal = compose_config["services"]["temporal"]
        assert "healthcheck" in temporal, "Temporal service missing healthcheck"

    def test_temporal_depends_on_postgres_healthy(self, compose_config):
        temporal = compose_config["services"]["temporal"]
        depends = temporal.get("depends_on", {})
        assert "postgres" in depends, "Temporal must depend on postgres"
        pg_dep = depends["postgres"]
        assert pg_dep.get("condition") == "service_healthy", (
            "Temporal must wait for postgres to be healthy"
        )

    def test_temporal_worker_service_exists(self, compose_config):
        assert "temporal-worker" in compose_config["services"], (
            "Temporal worker service must be defined in docker-compose.yml"
        )

    def test_temporal_worker_depends_on_temporal_healthy(self, compose_config):
        worker = compose_config["services"]["temporal-worker"]
        depends = worker.get("depends_on", {})
        assert "temporal" in depends, "Temporal worker must depend on temporal"
        temporal_dep = depends["temporal"]
        assert temporal_dep.get("condition") == "service_healthy", (
            "Temporal worker must wait for temporal to be healthy"
        )

    def test_temporal_worker_depends_on_postgres_healthy(self, compose_config):
        worker = compose_config["services"]["temporal-worker"]
        depends = worker.get("depends_on", {})
        assert "postgres" in depends, "Temporal worker must depend on postgres"

    def test_backend_depends_on_temporal_healthy(self, compose_config):
        backend = compose_config["services"]["backend"]
        depends = backend.get("depends_on", {})
        assert "temporal" in depends, "Backend must depend on temporal"
        temporal_dep = depends["temporal"]
        assert temporal_dep.get("condition") == "service_healthy", (
            "Backend must wait for temporal to be healthy"
        )

    def test_temporal_worker_has_task_queue_env(self, compose_config):
        worker = compose_config["services"]["temporal-worker"]
        env = worker.get("environment", {})
        if isinstance(env, list):
            env_str = " ".join(env)
        else:
            env_str = " ".join(f"{k}={v}" for k, v in env.items())
        assert "SLATE_TEMPORAL_TASK_QUEUE" in env_str, (
            "Temporal worker must have SLATE_TEMPORAL_TASK_QUEUE configured"
        )


@pytest.fixture(scope="module")
def prod_compose_config() -> dict:
    """Load and parse docker-compose.prod.yml."""
    compose_file = REPO_ROOT / "docker-compose.prod.yml"
    assert compose_file.exists(), f"docker-compose.prod.yml not found at {compose_file}"
    return yaml.safe_load(compose_file.read_text())


class TestProdDockerCompose:
    """Validate docker-compose.prod.yml structure and configuration."""

    EXPECTED_SERVICES = ["postgres", "temporal", "backend", "temporal-worker", "frontend"]

    def test_all_services_exist(self, prod_compose_config):
        services = prod_compose_config["services"]
        for svc in self.EXPECTED_SERVICES:
            assert svc in services, f"Service '{svc}' missing from docker-compose.prod.yml"

    def test_resource_limits_set(self, prod_compose_config):
        services = prod_compose_config["services"]
        for svc in self.EXPECTED_SERVICES:
            deploy = services[svc].get("deploy", {})
            resources = deploy.get("resources", {})
            limits = resources.get("limits", {})
            assert limits, f"Service '{svc}' missing resource limits in deploy section"
            assert "cpus" in limits or "memory" in limits, (
                f"Service '{svc}' resource limits must specify cpus or memory"
            )

    def test_healthchecks_exist(self, prod_compose_config):
        services = prod_compose_config["services"]
        for svc in self.EXPECTED_SERVICES:
            assert "healthcheck" in services[svc], (
                f"Service '{svc}' missing healthcheck in docker-compose.prod.yml"
            )

    def test_temporal_worker_healthcheck_is_meaningful(self, prod_compose_config):
        worker = prod_compose_config["services"]["temporal-worker"]
        hc_test = str(worker["healthcheck"]["test"])
        assert "/proc/1/status" not in hc_test, (
            "temporal-worker healthcheck should not just check /proc/1/status; "
            "it must verify the worker is actually connected to Temporal"
        )
        # Should reference the temporal server to confirm connectivity
        assert "temporal" in hc_test.lower(), (
            "temporal-worker healthcheck should verify connectivity to the Temporal server"
        )


class TestDockerfile:
    """Validate Dockerfile.backend structure."""

    @pytest.fixture(scope="class")
    def dockerfile_content(self) -> str:
        path = BACKEND_DIR / "Dockerfile.backend"
        assert path.exists(), f"Dockerfile.backend not found at {path}"
        return path.read_text()

    def test_multistage_build(self, dockerfile_content):
        """Dockerfile uses multi-stage build (builder + runtime)."""
        assert dockerfile_content.count("FROM ") >= 2, "Should have at least 2 FROM stages"
        assert "AS builder" in dockerfile_content
        assert "AS runtime" in dockerfile_content

    def test_python_312(self, dockerfile_content):
        assert "python:3.12" in dockerfile_content

    def test_copies_alembic(self, dockerfile_content):
        assert "COPY alembic" in dockerfile_content
        assert "COPY alembic.ini" in dockerfile_content

    def test_copies_entrypoint(self, dockerfile_content):
        assert "entrypoint.sh" in dockerfile_content

    def test_exposes_8000(self, dockerfile_content):
        assert "EXPOSE 8000" in dockerfile_content

    def test_runs_uvicorn(self, dockerfile_content):
        assert "uvicorn" in dockerfile_content


class TestEntrypoint:
    """Validate entrypoint.sh configuration."""

    @pytest.fixture(scope="class")
    def entrypoint_content(self) -> str:
        path = BACKEND_DIR / "entrypoint.sh"
        assert path.exists(), f"entrypoint.sh not found at {path}"
        return path.read_text()

    def test_runs_migrations(self, entrypoint_content):
        assert "alembic upgrade head" in entrypoint_content

    def test_uses_exec(self, entrypoint_content):
        """Entrypoint uses exec to replace shell process with CMD."""
        assert 'exec "$@"' in entrypoint_content

    def test_set_e(self, entrypoint_content):
        """Script exits on first error."""
        assert "set -e" in entrypoint_content
