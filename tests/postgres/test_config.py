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


def test_hosted_specifications_default_to_optional(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    config = load_server_config(
        _write_config(tmp_path, _server_toml()), environ=_runtime_env()
    )

    assert config.specifications.mode_for("team-wiki", "payments") == "optional"
    assert config.specifications.allow_project_mode is True


def test_hosted_specifications_can_disable_project_mode(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    text = "[specifications]\nallow_project_mode = false\n" + _server_toml()

    config = load_server_config(
        _write_config(tmp_path, text), environ=_runtime_env()
    )

    assert config.specifications.allow_project_mode is False


def test_hosted_specification_exact_pair_overrides_default(tmp_path):
    import dataclasses

    from iwiki_mcp.postgres.config import load_server_config

    text = """
[specifications]
default_mode = "optional"
[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"
""" + _server_toml()

    config = load_server_config(
        _write_config(tmp_path, text), environ=_runtime_env()
    )

    assert config.specifications.mode_for("team-wiki", "payments") == "strict"
    assert config.specifications.mode_for("team-wiki", "other") == "optional"
    assert config.specifications.mode_for("other-wiki", "payments") == "optional"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.specifications.overrides[0].mode = "disabled"


def test_hosted_specifications_accept_supported_default_modes(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    for mode in ("disabled", "optional", "strict"):
        text = f'[specifications]\ndefault_mode = "{mode}"\n' + _server_toml()
        config = load_server_config(
            _write_config(tmp_path, text), environ=_runtime_env()
        )
        assert config.specifications.mode_for("team-wiki", "payments") == mode


@pytest.mark.parametrize("second_mode", ["strict", "disabled"])
def test_hosted_specifications_reject_duplicate_exact_pairs(tmp_path, second_mode):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = f"""
[specifications]
[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"
[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "{second_mode}"
""" + _server_toml()

    with pytest.raises(ConfigError, match="duplicate"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


@pytest.mark.parametrize(
    "override",
    [
        'domain = "payments"\nmode = "strict"',
        'iwiki_id = "team-wiki"\nmode = "strict"',
        'iwiki_id = "team-wiki"\ndomain = "payments"',
        'iwiki_id = ""\ndomain = "payments"\nmode = "strict"',
        'iwiki_id = "team-wiki"\ndomain = " payments"\nmode = "strict"',
        'iwiki_id = "team-wiki"\ndomain = "payments"\nmode = "required"',
        (
            'iwiki_id = "team-wiki"\ndomain = "payments"\nmode = "strict"\n'
            'unknown = "must-not-be-shown"'
        ),
    ],
)
def test_hosted_specifications_reject_incomplete_or_invalid_overrides(
    tmp_path, override
):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = f"[specifications]\n[[specifications.overrides]]\n{override}\n" + _server_toml()

    with pytest.raises(ConfigError, match="specification") as caught:
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())

    assert "must-not-be-shown" not in str(caught.value)


@pytest.mark.parametrize(
    "specifications",
    [
        'specifications = "invalid"\n',
        "[specifications]\nunknown = true\n",
        "[specifications]\ndefault_mode = true\n",
        '[specifications]\nallow_project_mode = "false"\n',
        '[specifications]\ndefault_mode = "required"\n',
        '[specifications]\noverrides = "invalid"\n',
    ],
)
def test_hosted_specifications_reject_invalid_shapes_and_values(
    tmp_path, specifications
):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    with pytest.raises(ConfigError, match="specification"):
        load_server_config(
            _write_config(tmp_path, specifications + _server_toml()),
            environ=_runtime_env(),
        )


def test_hosted_specification_error_does_not_disclose_payload(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    secret = "must-not-be-shown"
    text = f'[specifications]\ndefault_mode = "{secret}"\n' + _server_toml()

    with pytest.raises(ConfigError) as caught:
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())

    diagnostic = str(caught.value)
    assert "specification" in diagnostic
    assert secret not in diagnostic


@pytest.mark.parametrize("default_mode", ["required", True, None])
def test_hosted_specifications_direct_constructor_rejects_invalid_default_mode(
    default_mode,
):
    from iwiki_mcp.postgres.config import ConfigError, HostedSpecificationsConfig

    with pytest.raises(ConfigError, match="specification mode"):
        HostedSpecificationsConfig(default_mode=default_mode)


@pytest.mark.parametrize("allow_project_mode", ["false", 0, 1, None])
def test_hosted_specifications_direct_constructor_rejects_invalid_project_switch(
    allow_project_mode,
):
    from iwiki_mcp.postgres.config import ConfigError, HostedSpecificationsConfig

    with pytest.raises(ConfigError, match="allow_project_mode"):
        HostedSpecificationsConfig(allow_project_mode=allow_project_mode)


@pytest.mark.parametrize(
    "values",
    [
        {"iwiki_id": "", "domain": "payments", "mode": "strict"},
        {"iwiki_id": True, "domain": "payments", "mode": "strict"},
        {"iwiki_id": "team-wiki", "domain": " payments", "mode": "strict"},
        {"iwiki_id": "team-wiki", "domain": True, "mode": "strict"},
        {"iwiki_id": "team-wiki", "domain": "payments", "mode": "required"},
        {"iwiki_id": "team-wiki", "domain": "payments", "mode": True},
    ],
)
def test_specification_override_direct_constructor_rejects_invalid_fields(values):
    from iwiki_mcp.postgres.config import ConfigError, SpecificationOverride

    with pytest.raises(ConfigError, match="specification override|specification mode"):
        SpecificationOverride(**values)


def test_specification_override_direct_constructor_normalizes_iwiki_id():
    from iwiki_mcp.postgres.config import SpecificationOverride

    override = SpecificationOverride(
        iwiki_id="  team-wiki  ", domain="payments", mode="strict"
    )

    assert override.iwiki_id == "team-wiki"


def test_hosted_specifications_direct_constructor_copies_override_list():
    import dataclasses

    from iwiki_mcp.postgres.config import (
        HostedSpecificationsConfig,
        SpecificationOverride,
    )

    payments = SpecificationOverride(
        iwiki_id="team-wiki", domain="payments", mode="strict"
    )
    source = [payments]

    config = HostedSpecificationsConfig(overrides=source)
    source.append(
        SpecificationOverride(
            iwiki_id="team-wiki", domain="other", mode="disabled"
        )
    )

    assert config.overrides == (payments,)
    assert config.mode_for("team-wiki", "payments") == "strict"
    assert config.mode_for("team-wiki", "other") == "optional"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.default_mode = "disabled"


@pytest.mark.parametrize("overrides", [["invalid"], ({"mode": "strict"},)])
def test_hosted_specifications_direct_constructor_rejects_non_override_entries(
    overrides,
):
    from iwiki_mcp.postgres.config import ConfigError, HostedSpecificationsConfig

    with pytest.raises(ConfigError, match="specification override"):
        HostedSpecificationsConfig(overrides=overrides)


def test_hosted_specifications_direct_constructor_rejects_duplicate_pair():
    from iwiki_mcp.postgres.config import (
        ConfigError,
        HostedSpecificationsConfig,
        SpecificationOverride,
    )

    override = SpecificationOverride(
        iwiki_id="team-wiki", domain="payments", mode="strict"
    )

    with pytest.raises(ConfigError, match="duplicate"):
        HostedSpecificationsConfig(overrides=[override, override])


def test_hosted_code_graph_limits_have_safe_defaults(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    config = load_server_config(
        _write_config(tmp_path, _server_toml() + "\n[code_graph]\n"),
        environ=_runtime_env(),
    )

    assert config.code_graph.max_snapshot_age_seconds == 86400
    assert config.code_graph.max_batch_rows == 1000
    assert config.code_graph.max_batch_bytes == 1_000_000
    assert config.code_graph.publication_session_ttl_seconds == 900
    assert config.code_graph.staging_retention_seconds == 86400
    assert config.code_graph.staging_cleanup_limit == 100
    assert config.code_graph.require_session_binding is False


def test_hosted_code_graph_limits_accept_valid_values(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    text = _server_toml() + """
[code_graph]
max_snapshot_age_seconds = 0
max_batch_rows = 5000
max_batch_bytes = 5000000
publication_session_ttl_seconds = 3600
staging_retention_seconds = 604800
staging_cleanup_limit = 1000
require_session_binding = true
"""

    config = load_server_config(
        _write_config(tmp_path, text),
        environ=_runtime_env(),
    )

    assert config.code_graph.max_snapshot_age_seconds == 0
    assert config.code_graph.max_batch_rows == 5000
    assert config.code_graph.max_batch_bytes == 5_000_000
    assert config.code_graph.publication_session_ttl_seconds == 3600
    assert config.code_graph.staging_retention_seconds == 604800
    assert config.code_graph.staging_cleanup_limit == 1000
    assert config.code_graph.require_session_binding is True


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("max_snapshot_age_seconds = -1", "max_snapshot_age_seconds"),
        ("max_batch_rows = 5001", "max_batch_rows"),
        ("max_batch_bytes = 5000001", "max_batch_bytes"),
        ("publication_session_ttl_seconds = 3601", "publication_session_ttl_seconds"),
        ("staging_retention_seconds = 604801", "staging_retention_seconds"),
        ("staging_cleanup_limit = 1001", "staging_cleanup_limit"),
        ("require_session_binding = 1", "require_session_binding"),
        ('password = "must-not-be-used"', "not allowed"),
        ('dsn = "postgresql://must-not-be-used"', "not allowed"),
        ('token = "must-not-be-used"', "not allowed"),
        ('url = "https://must-not-be-used"', "not allowed"),
        ("unexpected = true", "not allowed"),
    ],
)
def test_hosted_code_graph_rejects_unsafe_bounds_and_fields(
    tmp_path, field, message
):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = _server_toml() + f"\n[code_graph]\n{field}\n"

    with pytest.raises(ConfigError, match=message) as caught:
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())

    assert "must-not-be-used" not in str(caught.value)


def test_hosted_code_graph_requires_a_table(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = "code_graph = true\n" + _server_toml()

    with pytest.raises(ConfigError, match="tables"):
        load_server_config(_write_config(tmp_path, text), environ=_runtime_env())


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
