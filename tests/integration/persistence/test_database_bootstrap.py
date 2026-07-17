from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table, inspect, text
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_CONFIG = REPO_ROOT / "migrations" / "alembic.ini"
LOCAL_DATABASE_URL = "postgresql+psycopg://postgres:test@127.0.0.1:55432/crm_test"


def test_deployment_secret_template_covers_all_required_secrets() -> None:
    deploy_config = yaml.safe_load(
        (REPO_ROOT / "dashboard/config/deploy.yml").read_text()
    )
    required_secrets = set(deploy_config["env"]["secret"])
    assignments = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (REPO_ROOT / "dashboard/.kamal/secrets.example")
        .read_text()
        .splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert {"CRM_WRITE_TOKEN", "CRM_CSRF_TOKEN", "DATABASE_URL"} <= required_secrets
    assert "CRM_PRINCIPAL_PASSWORD" in required_secrets
    assert required_secrets <= assignments.keys()
    assert assignments["DATABASE_URL"].startswith("postgresql+psycopg://")
    clear_environment = deploy_config["env"]["clear"]
    assert {
        "CRM_PRINCIPAL_USERNAME",
        "CRM_PRINCIPAL_WORKSPACE_ID",
        "CRM_PRINCIPAL_IS_ADMIN",
    } <= clear_environment.keys()
    assert "CRM_PRINCIPAL_PASSWORD" not in clear_environment


def test_unnamed_check_constraint_gets_a_deterministic_name() -> None:
    from src.crm.persistence.base import NAMING_CONVENTION

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    account = Table("account", metadata, Column("balance", Integer))
    constraint = CheckConstraint(account.c.balance >= 0)

    account.append_constraint(constraint)

    assert constraint.name == "ck_account_balance"


def test_persistence_imports_do_not_create_an_engine_or_connect() -> None:
    script = """
import importlib.util
from pathlib import Path

import sqlalchemy
from sqlalchemy.engine import Engine


def unexpected(*args, **kwargs):
    raise AssertionError("database activity during import")

sqlalchemy.create_engine = unexpected
Engine.connect = unexpected

import dashboard.app.db
import src.crm.persistence.base

spec = importlib.util.spec_from_file_location("imported_alembic_env", Path("migrations/env.py"))
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not-a-url",
        "sqlite:///local.db",
        "postgresql://user:password@localhost/database",
        "postgresql+psycopg2://user:password@localhost/database",
    ],
)
def test_database_settings_reject_missing_or_unsupported_urls_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    from dashboard.app.db import DatabaseSettings

    if value is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", value)

    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings()

    error = str(exc_info.value)
    assert "DATABASE_URL must be a postgresql+psycopg URL" in error
    if value:
        assert value not in error
        assert "password" not in error


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("postgresql+psycopg://", id="bare-driver"),
        pytest.param("postgresql+psycopg:///crm_test", id="missing-host"),
        pytest.param("postgresql+psycopg://localhost", id="missing-database"),
        pytest.param(
            "postgresql+psycopg://secret-user:secret-password@localhost",
            id="missing-database-with-credentials",
        ),
        pytest.param(
            "postgresql+psycopg://localhost:not-a-port/crm_test",
            id="nonnumeric-port",
        ),
        pytest.param("postgresql+psycopg://localhost:0/crm_test", id="port-zero"),
        pytest.param(
            "postgresql+psycopg://localhost:65536/crm_test", id="port-too-large"
        ),
        pytest.param("postgresql+psycopg://bad host/crm_test", id="host-whitespace"),
        pytest.param("postgresql+psycopg://bad\thost/crm_test", id="host-control"),
        pytest.param(
            "postgresql+psycopg://bad%20host/crm_test", id="encoded-host-whitespace"
        ),
        pytest.param(
            "postgresql+psycopg://999.999.999.999/crm_test", id="out-of-range-ipv4"
        ),
        pytest.param(
            "postgresql+psycopg://999.999.999.999./crm_test",
            id="out-of-range-ipv4-with-root-dot",
        ),
        pytest.param("postgresql+psycopg://[gggg::1]/crm_test", id="malformed-ipv6"),
        pytest.param("postgresql+psycopg://bad..host/crm_test", id="empty-dns-label"),
        pytest.param("postgresql+psycopg://%/crm_test", id="host-percent-sign"),
        pytest.param(
            "postgresql+psycopg://-bad.example/crm_test", id="leading-label-hyphen"
        ),
        pytest.param(
            "postgresql+psycopg://bad-.example/crm_test", id="trailing-label-hyphen"
        ),
        pytest.param(
            f"postgresql+psycopg://{'a' * 64}.example/crm_test",
            id="dns-label-over-63-characters",
        ),
        pytest.param(
            f"postgresql+psycopg://{'.'.join(['a' * 63] * 4)}/crm_test",
            id="dns-name-over-253-characters",
        ),
        pytest.param(
            "postgresql+psycopg://localhost/crm test", id="database-whitespace"
        ),
        pytest.param("postgresql+psycopg://localhost/crm\ntest", id="database-control"),
        pytest.param(
            "postgresql+psycopg://localhost/crm%00test", id="encoded-database-control"
        ),
    ],
)
def test_database_settings_reject_semantically_incomplete_urls_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    from dashboard.app.db import DatabaseSettings

    monkeypatch.setenv("DATABASE_URL", value)

    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings()

    error = str(exc_info.value)
    assert "DATABASE_URL must be a postgresql+psycopg URL" in error
    rendered_errors = (error, repr(exc_info.value.errors()), exc_info.value.json())
    for rendered_error in rendered_errors:
        assert value not in rendered_error
        assert "secret-user" not in rendered_error
        assert "secret-password" not in rendered_error


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("host=attacker.example", id="host"),
        pytest.param("hostaddr=203.0.113.10", id="hostaddr"),
        pytest.param("port=6543", id="port"),
        pytest.param("dbname=attacker", id="dbname"),
        pytest.param("database=attacker", id="database"),
        pytest.param("user=attacker", id="user"),
        pytest.param("password=query-secret", id="password"),
        pytest.param("service=attacker", id="service"),
        pytest.param("servicefile=/tmp/attacker", id="servicefile"),
        pytest.param("%68ost=attacker.example", id="percent-encoded-host-key"),
        pytest.param("sslmode=require&HOST=attacker.example", id="mixed-case-host"),
        pytest.param("host=first.example&host=second.example", id="repeated-host"),
    ],
)
def test_database_settings_reject_libpq_identity_query_overrides_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    from dashboard.app.db import DatabaseSettings

    value = (
        f"postgresql+psycopg://secret-user:secret-password@localhost/crm_test?{query}"
    )
    monkeypatch.setenv("DATABASE_URL", value)

    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings()

    rendered_errors = (
        str(exc_info.value),
        repr(exc_info.value.errors()),
        exc_info.value.json(),
    )
    for rendered_error in rendered_errors:
        assert "DATABASE_URL must be a postgresql+psycopg URL" in rendered_error
        assert value not in rendered_error
        assert "secret-user" not in rendered_error
        assert "secret-password" not in rendered_error
        assert query not in rendered_error


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("postgresql+psycopg://localhost/crm_test", id="localhost"),
        pytest.param("postgresql+psycopg://127.0.0.1:5432/crm_test", id="ipv4"),
        pytest.param("postgresql+psycopg://[2001:db8::1]:5432/crm_test", id="ipv6"),
        pytest.param("postgresql+psycopg://db.example.com/crm_test", id="ordinary-dns"),
        pytest.param(
            "postgresql+psycopg://xn--mnchen-3ya.example/crm_test", id="punycode-dns"
        ),
        pytest.param(
            "postgresql+psycopg://münchen.example/crm_test", id="unicode-idna-dns"
        ),
        pytest.param(
            "postgresql+psycopg://db.example.com./crm_test", id="trailing-root-dot"
        ),
        pytest.param(
            "postgresql+psycopg://postgres_service/crm_test",
            id="deployment-hostname-with-underscore",
        ),
        pytest.param(
            "postgresql+psycopg://localhost/crm_test?sslmode=require",
            id="sslmode",
        ),
        pytest.param(
            "postgresql+psycopg://localhost/crm_test?connect_timeout=5",
            id="connect-timeout",
        ),
        pytest.param(
            "postgresql+psycopg://localhost/crm_test?application_name=crm",
            id="application-name",
        ),
    ],
)
def test_database_settings_accept_supported_network_hosts(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    from dashboard.app.db import DatabaseSettings

    monkeypatch.setenv("DATABASE_URL", value)

    settings = DatabaseSettings()
    assert isinstance(settings.database_url, SecretStr)
    assert settings.database_url.get_secret_value() == value
    for serialized in (
        repr(settings),
        repr(settings.model_dump()),
        settings.model_dump_json(),
    ):
        assert value not in serialized


def test_database_settings_are_loaded_lazily_and_cache_can_be_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dashboard.app.db import get_database_settings

    get_database_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", LOCAL_DATABASE_URL)
    first = get_database_settings()
    monkeypatch.delenv("DATABASE_URL")

    assert get_database_settings() is first
    get_database_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_database_settings()


def test_alembic_config_is_deprecation_warning_free() -> None:
    script = """
import warnings
from alembic.config import Config

with warnings.catch_warnings():
    warnings.simplefilter("error", DeprecationWarning)
    Config("migrations/alembic.ini").get_prepend_sys_paths_list()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_alembic_bootstrap_and_session_factory_work_from_any_cwd() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url != LOCAL_DATABASE_URL:
        pytest.skip("requires the documented disposable local PostgreSQL URL")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    commands = [
        (
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_CONFIG),
                "upgrade",
                "head",
            ],
            REPO_ROOT,
        ),
        (
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_CONFIG),
                "upgrade",
                "head",
            ],
            REPO_ROOT.parent,
        ),
    ]
    for command, cwd in commands:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert database_url not in result.stdout
        assert database_url not in result.stderr
        assert "postgres:test" not in result.stdout
        assert "postgres:test" not in result.stderr

    from dashboard.app.db import create_database_engine, create_session_factory

    engine = create_database_engine()
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()
