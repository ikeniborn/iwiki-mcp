from pathlib import Path

import pytest


def _runtime_env(**overrides):
    values = {
        "IWIKI_DB_PASSWORD": "database-secret",
        "IWIKI_EMBED_MODEL": "lemonade-embeddings-bge-m3-q8",
        "IWIKI_EMBED_DIMENSIONS": "1024",
        "IWIKI_RERANK_MODEL": "lemonade-reranker-bge-reranker-v2-m3",
    }
    values.update(overrides)
    return values


def _server_toml(storage_type="postgres", *, iwiki_id=""):
    identity = f'iwiki_id = "{iwiki_id}"\n' if iwiki_id else ""
    return (
        "[storage]\n"
        f'type = "{storage_type}"\n'
        'host = "127.0.0.1"\n'
        "port = 5432\n"
        'database = "iwiki"\n'
        'user = "iwiki_server"\n'
        'sslmode = "verify-full"\n'
        f"{identity}"
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 8080\n"
        'allowed_origins = ["https://iwiki.ikeniborn.ru"]\n'
        "pool_min_size = 1\n"
        "pool_max_size = 10\n"
        "statement_timeout_ms = 30000\n"
        "lock_timeout_ms = 5000\n"
    )


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "server.toml"
    path.write_text(text)
    return path


def test_postgres_test_dsn_repr_redacts_credentials():
    from tests.postgres.conftest import validated_test_dsn

    dsn = validated_test_dsn(
        "postgresql://test-user:test-secret@example.test/example_test"
    )

    assert repr(dsn) == "<redacted PostgreSQL test DSN>"
    assert "test-secret" not in repr(dsn)


def test_postgres_test_dsn_rejects_non_test_database_before_connection():
    from tests.postgres.conftest import validated_test_dsn

    with pytest.raises(ValueError, match="must end in _test"):
        validated_test_dsn(
            "postgresql://test-user:test-secret@example.test/production"
        )


def _server_toml_with_origins(origins):
    values = ", ".join(f'"{origin}"' for origin in origins)
    return _server_toml().replace(
        'allowed_origins = ["https://iwiki.ikeniborn.ru"]',
        f"allowed_origins = [{values}]",
    )


def test_load_server_config_parses_hosted_postgres_settings(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    config = load_server_config(
        _write_config(tmp_path, _server_toml()), environ=_runtime_env()
    )

    assert config.storage.host == "127.0.0.1"
    assert config.storage.port == 5432
    assert config.storage.database == "iwiki"
    assert config.storage.user == "iwiki_server"
    assert config.storage.sslmode == "verify-full"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8080
    assert config.server.allowed_origins == ("https://iwiki.ikeniborn.ru",)
    assert config.server.pool_min_size == 1
    assert config.server.pool_max_size == 10
    assert config.server.statement_timeout_ms == 30000
    assert config.server.lock_timeout_ms == 5000
    assert config.models.embed_model == "lemonade-embeddings-bge-m3-q8"
    assert config.models.embed_dimensions == 1024
    assert config.models.rerank_model == "lemonade-reranker-bge-reranker-v2-m3"


def test_runtime_models_accept_another_model_and_dimension():
    from iwiki_mcp.postgres.config import load_model_config

    config = load_model_config(
        _runtime_env(
            IWIKI_EMBED_MODEL="example-embedding-v2",
            IWIKI_EMBED_DIMENSIONS="768",
            IWIKI_RERANK_MODEL="",
        )
    )

    assert config.embed_model == "example-embedding-v2"
    assert config.embed_dimensions == 768
    assert config.rerank_model == ""


@pytest.mark.parametrize("storage_type", [None, "git"])
def test_load_server_config_rejects_non_postgres_storage(tmp_path, storage_type):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml("git") if storage_type else _server_toml().split("[server]")[1]
    if storage_type is None:
        text = "[server]" + text

    with pytest.raises(ConfigError, match="hosted server requires postgres storage"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


def test_load_server_config_rejects_fixed_iwiki_id(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    with pytest.raises(ConfigError, match="iwiki_id"):
        load_server_config(
            _write_config(tmp_path, _server_toml(iwiki_id="personal")),
            environ=_runtime_env(),
        )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('host = "127.0.0.1"\nport = 8080', 'host = "0.0.0.0"\nport = 8080', "loopback"),
        ("pool_min_size = 1", "pool_min_size = 0", "pool_min_size"),
        ("pool_max_size = 10", "pool_max_size = 101", "pool_max_size"),
        ("statement_timeout_ms = 30000", "statement_timeout_ms = 0", "statement_timeout_ms"),
        ("lock_timeout_ms = 5000", "lock_timeout_ms = 300001", "lock_timeout_ms"),
    ],
)
def test_load_server_config_rejects_unsafe_listener_and_bounds(
    tmp_path, old, new, message
):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml().replace(old, new)

    with pytest.raises(ConfigError, match=message):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


@pytest.mark.parametrize(
    "environ",
    [
        _runtime_env(IWIKI_DB_PASSWORD=""),
        _runtime_env(IWIKI_EMBED_MODEL=""),
        _runtime_env(IWIKI_EMBED_DIMENSIONS=""),
        _runtime_env(IWIKI_EMBED_DIMENSIONS="not-a-number"),
        _runtime_env(IWIKI_EMBED_DIMENSIONS="0"),
    ],
)
def test_config_errors_do_not_disclose_password_or_secret_variable(tmp_path, environ):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    secret = "database-secret"

    with pytest.raises(ConfigError) as caught:
        load_server_config(_write_config(tmp_path, _server_toml()), environ=environ)

    diagnostic = str(caught.value)
    assert secret not in diagnostic
    assert "IWIKI_DB_PASSWORD" not in diagnostic


def test_server_config_repr_redacts_database_password(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    secret = "database-secret"
    config = load_server_config(
        _write_config(tmp_path, _server_toml()), environ=_runtime_env()
    )

    assert secret not in repr(config)
    assert "IWIKI_DB_PASSWORD" not in repr(config)


def test_database_password_preserves_runtime_value(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    password = "  password-with-significant-spaces  "
    config = load_server_config(
        _write_config(tmp_path, _server_toml()),
        environ=_runtime_env(IWIKI_DB_PASSWORD=password),
    )

    assert config.storage.password == password


@pytest.mark.parametrize(
    "field",
    [
        'password = "must-not-be-used"',
        'embed_model = "must-not-be-used"',
        "embed_dimensions = 12",
        'rerank_model = "must-not-be-used"',
        'llm_base_url = "https://must-not-be-used"',
        'llm_key = "must-not-be-used"',
    ],
)
def test_load_server_config_rejects_runtime_settings_in_toml(tmp_path, field):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml().replace('sslmode = "verify-full"', f'sslmode = "verify-full"\n{field}')

    with pytest.raises(ConfigError, match="runtime environment"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


@pytest.mark.parametrize(
    "field",
    [
        'iwiki_id = "must-not-be-used"',
        'password = "must-not-be-used"',
        'embed_model = "must-not-be-used"',
        "embed_dimensions = 12",
        'rerank_model = "must-not-be-used"',
        'llm_base_url = "https://must-not-be-used"',
        'llm_key = "must-not-be-used"',
        "unexpected = true",
    ],
)
def test_load_server_config_rejects_non_server_keys_in_server_table(
    tmp_path, field
):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml() + field + "\n"

    with pytest.raises(ConfigError, match="not allowed"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


def test_load_server_config_rejects_unknown_storage_key_without_naming_it(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml().replace(
        'sslmode = "verify-full"',
        'sslmode = "verify-full"\nIWIKI_LLM_KEY = "must-not-be-used"',
    )

    with pytest.raises(ConfigError, match="not allowed") as caught:
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())

    assert "IWIKI_LLM_KEY" not in str(caught.value)


@pytest.mark.parametrize(
    "field",
    [
        'password = "must-not-be-used"',
        'llm_key = "must-not-be-used"',
        'embed_model = "must-not-be-used"',
        "embed_dimensions = 12",
        'rerank_model = "must-not-be-used"',
        "unexpected = true",
    ],
)
def test_load_server_config_rejects_non_table_top_level_keys(tmp_path, field):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = field + "\n" + _server_toml()

    with pytest.raises(ConfigError, match="not allowed") as caught:
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())

    assert "must-not-be-used" not in str(caught.value)


@pytest.mark.parametrize("table_name", ["storage", "server"])
def test_load_server_config_rejects_scalar_top_level_tables(tmp_path, table_name):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    storage_text, server_text = _server_toml().split("[server]", 1)
    if table_name == "storage":
        text = 'storage = "invalid"\n[server]' + server_text
    else:
        text = 'server = "invalid"\n' + storage_text

    with pytest.raises(ConfigError, match="tables"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://user:password@example.com",
        "example.com",
        "https://example.com/path",
        "https://example.com/",
        "https://example.com?query=value",
        "https://example.com#fragment",
        "ftp://example.com",
        "http://example.com",
        "http://127.0.0.2",
        "https://[::1",
        "https://example.com:0",
        "https://example.com:65536",
    ],
)
def test_load_server_config_rejects_invalid_allowed_origin(tmp_path, origin):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    with pytest.raises(ConfigError, match="allowed_origins"):
        load_server_config(
            _write_config(tmp_path, _server_toml_with_origins([origin])),
            environ=_runtime_env(),
        )


def test_load_server_config_normalizes_allowed_origins(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    text = _server_toml_with_origins(
        [
            "HTTPS://Example.COM:443",
            "http://LOCALHOST:80",
            "http://127.0.0.1:8080",
            "http://[::1]:8080",
        ]
    )

    config = load_server_config(_write_config(tmp_path, text), environ=_runtime_env())

    assert config.server.allowed_origins == (
        "https://example.com",
        "http://localhost",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
    )


def test_load_server_config_rejects_duplicate_normalized_origins(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml_with_origins(
        ["https://EXAMPLE.com", "https://example.com:443"]
    )

    with pytest.raises(ConfigError, match="allowed_origins"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())
