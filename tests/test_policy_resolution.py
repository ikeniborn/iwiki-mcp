import pytest

from iwiki_mcp.postgres.config import (
    ConfigError,
    HostedCodeGraphConfig,
    HostedSpecificationsConfig,
)
from iwiki_mcp.postgres.policy import (
    POLICY_FIELDS,
    PROJECT_TIER_FIELDS,
    parse_project_policy,
    resolve_policy,
)


def _resolve(*, specifications=None, code_graph=None, domain="payments", project=None):
    return resolve_policy(
        specifications or HostedSpecificationsConfig(),
        code_graph or HostedCodeGraphConfig(),
        "team-wiki",
        domain,
        project,
    )


def test_built_in_defaults_apply_without_any_configuration():
    resolved = resolve_policy(None, None, "team-wiki", "payments", None)

    assert resolved.value("specification_mode") == "optional"
    assert resolved.source("specification_mode") == "built_in_default"
    assert resolved.value("max_snapshot_age_seconds") == 86400
    assert resolved.value("require_session_binding") is False
    assert resolved.suppressed == ()


def test_project_tier_applies_when_at_least_as_strict():
    resolved = _resolve(project={"specification_mode": "strict"})

    assert resolved.value("specification_mode") == "strict"
    assert resolved.source("specification_mode") == "project"
    assert resolved.suppressed == ()


def test_project_tier_suppressed_when_looser_than_hosted_default():
    specifications = HostedSpecificationsConfig(default_mode="optional")

    resolved = _resolve(
        specifications=specifications, project={"specification_mode": "disabled"}
    )

    assert resolved.value("specification_mode") == "optional"
    assert resolved.source("specification_mode") == "hosted_default"
    assert resolved.suppressed == ("specification_mode",)


def test_every_field_declares_its_comparison_and_parser():
    for field in POLICY_FIELDS:
        assert callable(field.parse)
        assert callable(field.at_least_as_strict)
        assert field.default_at[0] in {"specifications", "code_graph"}
    assert "require_session_binding" not in PROJECT_TIER_FIELDS


def test_parse_project_policy_rejects_an_unknown_member():
    with pytest.raises(ConfigError):
        parse_project_policy({"max_batch_rows": 1})


def test_parse_project_policy_rejects_an_operator_only_field():
    with pytest.raises(ConfigError):
        parse_project_policy({"require_session_binding": True})
