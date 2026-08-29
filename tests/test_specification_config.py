from pathlib import Path

import pytest

from iwiki_mcp import base


def _runtime_env(*, wiki_base: str = "") -> dict[str, str]:
    return {
        "IWIKI_BASE_DIR": wiki_base,
        "IWIKI_DB_PASSWORD": "database-secret",
        "IWIKI_EMBED_MODEL": "example-embedding",
        "IWIKI_EMBED_DIMENSIONS": "1024",
        "IWIKI_RERANK_MODEL": "example-reranker",
    }


def _project_config(storage_type: str, specifications: str = "") -> str:
    storage = (
        '[storage]\ntype = "git"\n'
        if storage_type == "git"
        else (
            '[storage]\ntype = "postgres"\nhost = "db.example.net"\n'
            'port = 5432\ndatabase = "iwiki"\nuser = "iwiki_local"\n'
            'sslmode = "verify-full"\niwiki_id = "team-wiki"\n'
        )
    )
    return (
        'read = ["payments"]\nwrite = ["payments"]\nprimary = "payments"\n'
        f"{specifications}{storage}"
    )


def _resolve_local_binding(
    tmp_path: Path, storage_type: str, specifications: str = ""
):
    project = tmp_path / storage_type
    project.mkdir()
    wiki_base = tmp_path / "wiki"
    (wiki_base / "payments").mkdir(parents=True, exist_ok=True)
    (project / ".iwiki.toml").write_text(
        _project_config(storage_type, specifications), encoding="utf-8"
    )
    return base.resolve_storage_binding(
        str(project), environ=_runtime_env(wiki_base=str(wiki_base))
    )


@pytest.mark.parametrize("storage_type", ["git", "postgres"])
@pytest.mark.parametrize("specifications", ["", "[specifications]\n"])
def test_local_specification_mode_defaults_to_optional(
    tmp_path, storage_type, specifications
):
    binding = _resolve_local_binding(tmp_path, storage_type, specifications)

    assert binding.specification_mode == "optional"


@pytest.mark.parametrize("storage_type", ["git", "postgres"])
@pytest.mark.parametrize("mode", ["disabled", "optional", "strict"])
def test_local_specification_mode_accepts_exact_supported_values(
    tmp_path, storage_type, mode
):
    binding = _resolve_local_binding(
        tmp_path, storage_type, f'[specifications]\nmode = "{mode}"\n'
    )

    assert binding.specification_mode == mode


@pytest.mark.parametrize("storage_type", ["git", "postgres"])
@pytest.mark.parametrize(
    "specifications",
    [
        'specifications = "invalid"\n',
        "specifications = true\n",
        "specifications = []\n",
        "[specifications]\nunknown = true\n",
        "[specifications]\nmode = true\n",
        '[specifications]\nmode = "required"\n',
    ],
)
def test_local_specification_config_rejects_invalid_shapes_and_values(
    tmp_path, storage_type, specifications
):
    with pytest.raises(base.BaseError, match="specification"):
        _resolve_local_binding(tmp_path, storage_type, specifications)


@pytest.mark.parametrize("storage_type", ["git", "postgres"])
def test_local_specification_config_error_does_not_disclose_payload(
    tmp_path, storage_type
):
    secret = "must-not-be-shown"

    with pytest.raises(base.BaseError) as caught:
        _resolve_local_binding(
            tmp_path,
            storage_type,
            f'[specifications]\nmode = "{secret}"\n',
        )

    diagnostic = str(caught.value)
    assert "specification" in diagnostic
    assert secret not in diagnostic
