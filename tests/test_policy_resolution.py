import pytest

from iwiki_mcp.postgres.config import (
    ConfigError,
    HostedCodeGraphConfig,
    HostedSpecificationsConfig,
    PolicyOverride,
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


def test_specification_mode_suppressed_when_looser_than_hosted_default():
    specifications = HostedSpecificationsConfig(default_mode="optional")

    resolved = _resolve(
        specifications=specifications, project={"specification_mode": "disabled"}
    )

    assert resolved.value("specification_mode") == "optional"
    assert resolved.source("specification_mode") == "hosted_default"
    assert resolved.suppressed == ("specification_mode",)


def test_max_snapshot_age_seconds_project_wins_when_lower_than_hosted_default():
    code_graph = HostedCodeGraphConfig(max_snapshot_age_seconds=3600)

    resolved = _resolve(
        code_graph=code_graph, project={"max_snapshot_age_seconds": 60}
    )

    assert resolved.value("max_snapshot_age_seconds") == 60
    assert resolved.source("max_snapshot_age_seconds") == "project"
    assert resolved.suppressed == ()


def test_max_snapshot_age_seconds_suppressed_when_higher_than_hosted_default():
    code_graph = HostedCodeGraphConfig(max_snapshot_age_seconds=3600)

    resolved = _resolve(
        code_graph=code_graph, project={"max_snapshot_age_seconds": 7200}
    )

    assert resolved.value("max_snapshot_age_seconds") == 3600
    assert resolved.source("max_snapshot_age_seconds") == "hosted_default"
    assert resolved.suppressed == ("max_snapshot_age_seconds",)


def test_max_snapshot_age_seconds_zero_is_suppressed_against_nonzero_floor():
    """`0` disables age rejection, so it is the weakest value, not the strictest."""
    code_graph = HostedCodeGraphConfig(max_snapshot_age_seconds=3600)

    resolved = _resolve(
        code_graph=code_graph, project={"max_snapshot_age_seconds": 0}
    )

    assert resolved.value("max_snapshot_age_seconds") == 3600
    assert resolved.source("max_snapshot_age_seconds") == "hosted_default"
    assert resolved.suppressed == ("max_snapshot_age_seconds",)


def test_max_snapshot_age_seconds_zero_accepted_when_floor_is_zero():
    code_graph = HostedCodeGraphConfig(max_snapshot_age_seconds=0)

    resolved = _resolve(
        code_graph=code_graph, project={"max_snapshot_age_seconds": 0}
    )

    assert resolved.value("max_snapshot_age_seconds") == 0
    assert resolved.source("max_snapshot_age_seconds") == "project"
    assert resolved.suppressed == ()


def test_every_field_declares_its_comparison_and_parser():
    """Guard: every project-tier field must ship its own suppression test.

    Naming convention enforced here: a suppression test for field `<name>`
    is a module-level `test_*` function whose name contains both `<name>`
    and the substring `suppressed` (see the `_suppressed_when_*` /
    `_suppressed_against_*` tests above). A new project-tier field with no
    such test fails this guard instead of shipping untested.
    """
    test_names = [name for name in globals() if name.startswith("test_")]
    for field in POLICY_FIELDS:
        assert callable(field.parse)
        assert callable(field.at_least_as_strict)
        assert field.default_at[0] in {"specifications", "code_graph"}
        if field.project_tier:
            matches = [
                name for name in test_names
                if field.name in name and "suppressed" in name
            ]
            assert matches, (
                f"project-tier field {field.name!r} has no suppression test; "
                f"add test_{field.name}_suppressed_..."
            )
    assert "require_session_binding" not in PROJECT_TIER_FIELDS


def test_parse_project_policy_rejects_an_unknown_member():
    with pytest.raises(ConfigError):
        parse_project_policy({"max_batch_rows": 1})


def test_parse_project_policy_rejects_an_operator_only_field():
    with pytest.raises(ConfigError):
        parse_project_policy({"require_session_binding": True})


def test_tenant_override_applies_to_every_domain():
    specifications = HostedSpecificationsConfig(
        overrides=(PolicyOverride("team-wiki", None, {"specification_mode": "disabled"}),)
    )

    for domain in ("payments", "billing"):
        resolved = _resolve(specifications=specifications, domain=domain)
        assert resolved.value("specification_mode") == "disabled"
        assert resolved.source("specification_mode") == "hosted_override"


def test_exact_override_beats_the_tenant_override():
    specifications = HostedSpecificationsConfig(
        overrides=(
            PolicyOverride("team-wiki", None, {"specification_mode": "disabled"}),
            PolicyOverride("team-wiki", "payments", {"specification_mode": "strict"}),
        )
    )

    assert _resolve(specifications=specifications).value("specification_mode") == "strict"
    assert (
        _resolve(specifications=specifications, domain="billing").value(
            "specification_mode"
        )
        == "disabled"
    )


def test_inheritance_is_per_field_not_per_record():
    specifications = HostedSpecificationsConfig(
        default_mode="optional",
        overrides=(PolicyOverride("team-wiki", None, {"require_session_binding": True}),),
    )

    resolved = _resolve(
        specifications=specifications, project={"specification_mode": "strict"}
    )

    assert resolved.value("require_session_binding") is True
    assert resolved.source("require_session_binding") == "hosted_override"
    assert resolved.value("specification_mode") == "strict"
    assert resolved.source("specification_mode") == "project"


def test_domainless_resolution_uses_the_tenant_override():
    specifications = HostedSpecificationsConfig(
        overrides=(PolicyOverride("team-wiki", None, {"require_session_binding": True}),)
    )

    resolved = _resolve(specifications=specifications, domain=None)

    assert resolved.value("require_session_binding") is True
