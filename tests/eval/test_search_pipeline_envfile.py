from types import SimpleNamespace

import pytest

from eval.search_pipeline.envfile import (
    apply_env_file,
    load_env_file,
    safe_config_fingerprint,
    validate_env_file_path,
)
from iwiki_mcp.engine.config import Config


def test_load_env_file_accepts_comments_export_and_quoted_values(tmp_path):
    env = tmp_path / ".benchmark.env"
    env.write_text(
        "# local only\n"
        "export IWIKI_LLM_KEY='secret key'\n"
        'IWIKI_LLM_BASE_URL="https://secret.example/v1"\n'
        "IWIKI_RERANK_MODEL=rerank-model\n",
        encoding="utf-8",
    )

    assert load_env_file(env) == {
        "IWIKI_LLM_KEY": "secret key",
        "IWIKI_LLM_BASE_URL": "https://secret.example/v1",
        "IWIKI_RERANK_MODEL": "rerank-model",
    }


def test_load_env_file_preserves_unquoted_spaces_before_inline_comment(tmp_path):
    env = tmp_path / ".benchmark.env"
    env.write_text(
        "PLAIN=value with spaces # local comment\n"
        "TRAILING=value with trailing spaces   \n"
        "QUOTED='quoted # value' # local comment\n",
        encoding="utf-8",
    )

    assert load_env_file(env) == {
        "PLAIN": "value with spaces",
        "TRAILING": "value with trailing spaces",
        "QUOTED": "quoted # value",
    }


def test_apply_env_file_restores_previous_values_and_removes_new_values(
    tmp_path, monkeypatch
):
    env = tmp_path / ".benchmark.env"
    env.write_text(
        "IWIKI_LLM_KEY=file-secret\n"
        "IWIKI_RERANK_MODEL=rerank-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("IWIKI_LLM_KEY", "shell-secret")
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)

    with apply_env_file(env):
        assert __import__("os").environ["IWIKI_LLM_KEY"] == "file-secret"
        assert __import__("os").environ["IWIKI_RERANK_MODEL"] == "rerank-model"

    assert __import__("os").environ["IWIKI_LLM_KEY"] == "shell-secret"
    assert "IWIKI_RERANK_MODEL" not in __import__("os").environ


def test_validate_env_file_rejects_output_tree_without_leaking_values(
    tmp_path, capsys
):
    out = tmp_path / "evidence"
    out.mkdir()
    env = out / ".benchmark.env"
    env.write_text("IWIKI_LLM_KEY=secret\n", encoding="utf-8")

    result = validate_env_file_path(env, out)

    captured = capsys.readouterr()
    assert result["ok"] is False
    assert "inside output directory" in result["errors"][0]
    assert "secret" not in repr(result)
    assert "secret" not in captured.out
    assert "secret" not in captured.err


def test_validate_env_file_rejects_symlink_location_inside_output_tree(tmp_path):
    out = tmp_path / "evidence"
    out.mkdir()
    outside = tmp_path / ".benchmark.env"
    outside.write_text("IWIKI_LLM_KEY=secret\n", encoding="utf-8")
    env = out / ".benchmark.env"
    try:
        env.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unsupported: {exc}")

    result = validate_env_file_path(env, out)

    assert result["ok"] is False
    assert "inside output directory" in result["errors"][0]


def test_validate_env_file_warns_when_file_appears_git_tracked(
    tmp_path, monkeypatch
):
    env = tmp_path / ".benchmark.env"
    env.write_text("IWIKI_LLM_KEY=secret\n", encoding="utf-8")

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        if args[0][:5] == [
            "git",
            "-C",
            str(env.parent),
            "rev-parse",
            "--show-toplevel",
        ]:
            return SimpleNamespace(stdout=str(tmp_path))
        return None

    monkeypatch.setattr("eval.search_pipeline.envfile.subprocess.run", fake_run)

    result = validate_env_file_path(env, tmp_path / "out")

    assert result["ok"] is True
    assert result["warnings"] == ["env file appears tracked by git"]
    assert calls[0][:3] == ["git", "-C", str(env.parent)]


def test_safe_config_fingerprint_redacts_key_base_url_and_secret_fields():
    cfg = Config(
        base_url="https://secret.example/v1",
        api_key="secret",
        embed_model="embed-model",
        dimensions=2,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.2,
        graph_depth=2,
        ignore=None,
        rerank_model="rerank-model",
        chat_model="chat-model",
    )

    fingerprint = safe_config_fingerprint(cfg)

    assert fingerprint["embed_model"] == "embed-model"
    assert fingerprint["chat_model"] == "chat-model"
    assert fingerprint["rerank_model"] == "rerank-model"
    assert fingerprint["rerank_enabled"] is True
    assert "secret" not in repr(fingerprint)
    assert "base_url" not in fingerprint
    assert "api_key" not in fingerprint
