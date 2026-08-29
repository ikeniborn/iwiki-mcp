from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import subprocess

import pytest

from iwiki_mcp import indexer, server
from iwiki_mcp.specification_store import (
    GitSpecificationStore,
    PreparedProjectionReplace,
    decode_jsonl,
)


VALID_SPECIFICATION = '''# Account behavior

## Open account

```iwiki-gwt
id = "open-account"
title = "Open account"
given = [{ role = "state", name = "Account is pending" }]
when = { role = "command", name = "OpenAccount" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", symbol = "Account.open" },
  { relation = "verifies", file = "tests/test_account.py" }
]
```
'''


def _git(base, *args):
    return subprocess.run(
        ["git", *args], cwd=base, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _seed(tmp_path, monkeypatch, mode):
    wiki = tmp_path / "wiki"
    domain = wiki / "payments"
    domain.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".iwiki.toml").write_text(
        'read = ["payments"]\nwrite = ["payments"]\nprimary = "payments"\n'
        f'[specifications]\nmode = "{mode}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", str(wiki))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(project))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(
        indexer, "embed_texts", lambda _cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    _git(wiki, "init", "-q")
    _git(wiki, "config", "user.email", "test@example.invalid")
    _git(wiki, "config", "user.name", "test")
    (domain / "seed.md").write_text("# Seed\n\n## Notes\nseed\n", encoding="utf-8")
    _git(wiki, "add", "-A")
    _git(wiki, "commit", "-q", "-m", "seed")
    return wiki, domain


def test_optional_valid_specification_writes_projection_in_same_commit(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "optional")

    result = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )

    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    committed = _git(wiki, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert result["page"] == "payments/specification/account.md"
    assert result["specifications"] == {
        "mode": "optional",
        "state": "ready",
        "scenarios": 1,
        "bindings": 2,
        "findings": [],
    }
    assert projection.scenario_count == 1
    assert projection.binding_count == 2
    assert committed == [
        "payments/index.jsonl",
        "payments/log.jsonl",
        "payments/specification/account.md",
        "payments/specifications.jsonl",
    ]


def test_strict_invalid_target_rejects_before_visible_change(tmp_path, monkeypatch):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    before = _git(wiki, "rev-parse", "HEAD")

    result = server.wiki_write_page(
        "payments",
        "broken",
        "# Broken\n\n## Scenario\nNo block.\n",
        type="specification",
    )

    assert result["error"] == "specification validation failed"
    assert result["specifications"]["mode"] == "strict"
    assert result["specifications"]["state"] == "failed"
    assert result["specifications"]["findings"][0]["type"] == "missing_scenario"
    assert not (domain / "specification" / "broken.md").exists()
    assert not (domain / "specifications.jsonl").exists()
    assert _git(wiki, "rev-parse", "HEAD") == before


def test_ordinary_page_bypasses_specification_factories_in_strict_mode(
    tmp_path, monkeypatch
):
    _, domain = _seed(tmp_path, monkeypatch, "strict")

    monkeypatch.setattr(
        server,
        "_assemble_specification_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary page reached specification projection")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_git_specification_store_factory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary page opened specification storage")
        ),
        raising=False,
    )

    result = server.wiki_write_page(
        "payments", "notes", "# Notes\n\n## Body\nordinary\n"
    )

    assert result["page"] == "payments/concept/notes.md"
    assert "specifications" not in result
    assert not (domain / "specifications.jsonl").exists()


def test_wiki_index_rebuilds_optional_projection_from_sorted_snapshot(
    tmp_path, monkeypatch
):
    _, domain = _seed(tmp_path, monkeypatch, "optional")
    spec_dir = domain / "specification"
    spec_dir.mkdir()
    first = VALID_SPECIFICATION.replace("open-account", "z-account")
    second = VALID_SPECIFICATION.replace("open-account", "a-account")
    (spec_dir / "z.md").write_text(
        "---\ntype: specification\n---\n" + first, encoding="utf-8"
    )
    (spec_dir / "a.md").write_text(
        "---\ntype: specification\n---\n" + second, encoding="utf-8"
    )

    result = server.wiki_index("payments")

    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    assert result["specifications"]["state"] == "ready"
    assert [item.scenario_id for item in projection.scenarios] == [
        "a-account",
        "z-account",
    ]


def test_strict_index_failure_restores_page_projection_index_log_and_head(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    tracked = [
        domain / "specification" / "account.md",
        domain / "index.jsonl",
        domain / "log.jsonl",
        domain / "specifications.jsonl",
    ]
    before = {path: path.read_bytes() for path in tracked}
    head = _git(wiki, "rev-parse", "HEAD")
    real_index = indexer.index_domain

    def fail_after_index(*args, **kwargs):
        real_index(*args, **kwargs)
        raise RuntimeError("private path /secret must not escape")

    monkeypatch.setattr(indexer, "index_domain", fail_after_index)

    result = server.wiki_update_page(
        "payments", "specification/account", "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )

    assert result["error"] == "specification transaction failed"
    assert result["rolled_back"] is True
    assert "/secret" not in str(result)
    assert {path: path.read_bytes() for path in tracked} == before
    assert _git(wiki, "rev-parse", "HEAD") == head
    assert _git(wiki, "diff", "--cached", "--name-only") == ""
    assert not list(domain.glob(".specifications-*.tmp"))


def test_optional_projection_publish_failure_commits_markdown_and_keeps_projection(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "optional")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    projection_path = domain / "specifications.jsonl"
    projection_before = projection_path.read_bytes()
    head_before = _git(wiki, "rev-parse", "HEAD")

    def fail_publish(self):
        self.abort()
        raise RuntimeError("credential https://secret.example.invalid")

    monkeypatch.setattr(PreparedProjectionReplace, "publish", fail_publish)

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )

    assert "error" not in result
    assert result["specifications"]["state"] == "stale"
    assert result["specifications"]["warning"] == (
        "specification projection is stale"
    )
    assert "secret" not in str(result)
    assert projection_path.read_bytes() == projection_before
    assert "ConfirmAccount" in (
        domain / "specification" / "account.md"
    ).read_text(encoding="utf-8")
    assert _git(wiki, "rev-parse", "HEAD") != head_before


def test_strict_parser_failure_is_sanitized_before_visible_change(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    head_before = _git(wiki, "rev-parse", "HEAD")
    monkeypatch.setattr(
        server,
        "_assemble_specification_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("https://token@example.invalid/private")
        ),
    )

    result = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )

    assert result["error"] == "specification projection preparation failed"
    assert "token" not in str(result)
    assert not (domain / "specification" / "account.md").exists()
    assert not (domain / "specifications.jsonl").exists()
    assert _git(wiki, "rev-parse", "HEAD") == head_before


def test_strict_local_commit_failure_restores_all_files(tmp_path, monkeypatch):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    tracked = [
        domain / "specification" / "account.md",
        domain / "index.jsonl",
        domain / "log.jsonl",
        domain / "specifications.jsonl",
    ]
    before = {path: path.read_bytes() for path in tracked}
    head = _git(wiki, "rev-parse", "HEAD")
    monkeypatch.setattr(
        server.sync,
        "commit_locked",
        lambda *_args, **_kwargs: {
            "committed": False,
            "warning": "fatal https://secret.example.invalid",
        },
    )

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )

    assert result["error"] == "specification transaction failed"
    assert result["rolled_back"] is True
    assert "secret" not in str(result)
    assert {path: path.read_bytes() for path in tracked} == before
    assert _git(wiki, "rev-parse", "HEAD") == head
    assert _git(wiki, "diff", "--cached", "--name-only") == ""
    assert not list(domain.glob(".specifications-*.tmp"))


@pytest.mark.parametrize(
    "fault",
    ["projection_prepare", "page_write", "projection_publish", "stage"],
)
def test_strict_new_page_faults_leave_no_visible_artifacts(
    tmp_path, monkeypatch, fault
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    page = domain / "specification" / "account.md"
    tracked = (
        page,
        domain / "index.jsonl",
        domain / "log.jsonl",
        domain / "specifications.jsonl",
    )
    head_before = _git(wiki, "rev-parse", "HEAD")

    if fault == "projection_prepare":
        monkeypatch.setattr(
            server.GitSpecificationStore,
            "prepare",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("private preparation failure")
            ),
        )
    elif fault == "page_write":
        real_open = open

        def fail_page_write(file, mode="r", *args, **kwargs):
            if str(file) == str(page) and "w" in mode:
                raise RuntimeError("private page write failure")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fail_page_write)
    elif fault == "projection_publish":
        def fail_projection_publish(prepared):
            prepared.abort()
            raise RuntimeError("private projection publication failure")

        monkeypatch.setattr(
            PreparedProjectionReplace, "publish", fail_projection_publish
        )
    else:
        real_run = server.sync._run

        def fail_stage(base_dir, *args):
            if args and args[0] == "add":
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="private staging failure"
                )
            return real_run(base_dir, *args)

        monkeypatch.setattr(server.sync, "_run", fail_stage)

    result = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )

    expected_error = (
        "specification projection preparation failed"
        if fault == "projection_prepare"
        else "specification transaction failed"
    )
    assert result["error"] == expected_error
    assert "private" not in str(result)
    assert not any(path.exists() for path in tracked)
    assert _git(wiki, "rev-parse", "HEAD") == head_before
    assert _git(wiki, "diff", "--cached", "--name-only") == ""
    assert not list(domain.glob(".specifications-*.tmp"))


def test_optional_all_page_mutations_refresh_projection(tmp_path, monkeypatch):
    _, domain = _seed(tmp_path, monkeypatch, "optional")
    written = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert written["specifications"]["state"] == "ready"

    updated = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )
    inserted = server.wiki_insert_section(
        "payments", "specification/account", "Notes", "Projected safely."
    )
    moved = server.wiki_move_section(
        "payments",
        "specification/account",
        "Notes",
        before_heading="Open account",
    )
    deleted_section = server.wiki_delete_section(
        "payments", "specification/account", "Notes"
    )
    deleted_page = server.wiki_delete_page(
        "payments", "specification/account"
    )

    for result in (updated, inserted, moved, deleted_section, deleted_page):
        assert result["specifications"]["state"] == "ready"
    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    assert projection.scenario_count == 0
    assert projection.binding_count == 0


def test_disabled_specification_page_uses_existing_markdown_path_only(
    tmp_path, monkeypatch
):
    _, domain = _seed(tmp_path, monkeypatch, "disabled")
    monkeypatch.setattr(
        server,
        "_assemble_specification_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled mode parsed specification")
        ),
    )
    monkeypatch.setattr(
        server,
        "_git_specification_store_factory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled mode opened projection")
        ),
    )

    result = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    indexed = server.wiki_index("payments")

    assert result["page"] == "payments/specification/account.md"
    assert "specifications" not in result
    assert "specifications" not in indexed
    assert not (domain / "specifications.jsonl").exists()


def test_optional_missing_scenario_is_advisory_and_excluded(tmp_path, monkeypatch):
    _, domain = _seed(tmp_path, monkeypatch, "optional")

    result = server.wiki_write_page(
        "payments",
        "broken",
        "# Broken\n\n## Scenario\nNo block.\n",
        type="specification",
    )

    assert "error" not in result
    assert result["specifications"]["state"] == "ready"
    assert result["specifications"]["scenarios"] == 0
    assert result["specifications"]["findings"][0]["type"] == "missing_scenario"
    assert (domain / "specification" / "broken.md").is_file()


def test_projection_assembly_runs_inside_single_server_base_lock(
    tmp_path, monkeypatch
):
    _, _domain = _seed(tmp_path, monkeypatch, "strict")
    held = {"value": False, "count": 0}
    real_lock = server.base_lock
    real_assemble = server._assemble_specification_projection

    @contextmanager
    def tracking_lock(*args, **kwargs):
        held["count"] += 1
        with real_lock(*args, **kwargs):
            held["value"] = True
            try:
                yield
            finally:
                held["value"] = False

    def checked_assemble(*args, **kwargs):
        assert held["value"] is True
        return real_assemble(*args, **kwargs)

    monkeypatch.setattr(server, "base_lock", tracking_lock)
    monkeypatch.setattr(server, "_assemble_specification_projection", checked_assemble)

    result = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )

    assert "error" not in result
    assert held["count"] == 1


def test_unrelated_same_domain_change_is_not_committed_by_spec_transaction(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    page = domain / "specification" / "account.md"
    projection = domain / "specifications.jsonl"
    page_before = page.read_bytes()
    projection_before = projection.read_bytes()
    head_before = _git(wiki, "rev-parse", "HEAD")
    unrelated = domain / "unrelated.md"
    unrelated.write_text("# Concurrent\n\n## Notes\nkeep me uncommitted\n")

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )

    assert result["error"] == "specification transaction failed"
    assert page.read_bytes() == page_before
    assert projection.read_bytes() == projection_before
    assert unrelated.is_file()
    assert _git(wiki, "rev-parse", "HEAD") == head_before
    assert "payments/unrelated.md" in _git(
        wiki, "status", "--porcelain", "-uall"
    )


def test_concurrent_markdown_change_at_lock_boundary_fails_without_partial_commit(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    page = domain / "specification" / "account.md"
    projection = domain / "specifications.jsonl"
    page_before = page.read_bytes()
    projection_before = projection.read_bytes()
    head_before = _git(wiki, "rev-parse", "HEAD")
    real_lock = server.base_lock
    raced = {"done": False}

    @contextmanager
    def racing_lock(*args, **kwargs):
        if not raced["done"]:
            raced["done"] = True
            (domain / "raced.md").write_text(
                "# Race\n\n## Notes\narrived before lock\n", encoding="utf-8"
            )
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(server, "base_lock", racing_lock)

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )

    assert result["error"] == "specification transaction failed"
    assert page.read_bytes() == page_before
    assert projection.read_bytes() == projection_before
    assert (domain / "raced.md").is_file()
    assert _git(wiki, "rev-parse", "HEAD") == head_before


def test_specification_heading_rename_updates_projection_atomically(
    tmp_path, monkeypatch
):
    _, domain = _seed(tmp_path, monkeypatch, "strict")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1],
        new_heading="Confirm account",
    )

    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    assert "error" not in result
    assert result["specifications"]["state"] == "ready"
    assert projection.scenarios[0].heading == "Confirm account"


def test_specification_heading_rename_projects_final_backlink_revision(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    backlink = VALID_SPECIFICATION.replace(
        "## Open account\n\n",
        "## Review account\n\n"
        "[Account](specification/account.md#open-account)\n\n",
    ).replace('id = "open-account"', 'id = "review-account"')
    linked = server.wiki_write_page(
        "payments", "review", backlink, type="specification"
    )
    assert "error" not in linked

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1],
        new_heading="Confirm account",
    )

    backlink_path = domain / "specification" / "review.md"
    backlink_bytes = backlink_path.read_bytes()
    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    record = next(
        item for item in projection.scenarios
        if item.scenario_id == "review-account"
    )
    assert "error" not in result
    assert result["specifications"]["state"] == "ready"
    assert b"#confirm-account" in backlink_bytes
    assert record.page_revision == f"sha256:{sha256(backlink_bytes).hexdigest()}"
    assert GitSpecificationStore(str(wiki), "strict").status(
        "payments"
    ).state == "ready"
    assert _git(wiki, "diff", "--cached", "--name-only") == ""


def test_ordinary_heading_rename_reprojects_specification_backlink(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    ordinary = server.wiki_write_page(
        "payments", "guide", "# Guide\n\n## Old heading\nBody.\n"
    )
    assert "error" not in ordinary
    backlink = VALID_SPECIFICATION.replace(
        "## Open account\n\n",
        "## Review guide\n\n"
        "[Guide](concept/guide.md#old-heading)\n\n",
    ).replace('id = "open-account"', 'id = "review-guide"')
    linked = server.wiki_write_page(
        "payments", "review", backlink, type="specification"
    )
    assert "error" not in linked

    result = server.wiki_update_page(
        "payments",
        "concept/guide",
        "Old heading",
        "Body.",
        new_heading="New heading",
    )

    backlink_path = domain / "specification" / "review.md"
    backlink_bytes = backlink_path.read_bytes()
    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    record = projection.scenarios[0]
    assert "error" not in result
    assert result["specifications"]["state"] == "ready"
    assert b"#new-heading" in backlink_bytes
    assert record.page_revision == f"sha256:{sha256(backlink_bytes).hexdigest()}"
    assert GitSpecificationStore(str(wiki), "strict").status(
        "payments"
    ).state == "ready"


def test_optional_stale_projection_survives_fresh_store_until_rebuild(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "optional")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    projection_path = domain / "specifications.jsonl"
    projection_before = projection_path.read_bytes()
    real_publish = PreparedProjectionReplace.publish

    def fail_publish(prepared):
        prepared.abort()
        raise RuntimeError("private projection failure")

    monkeypatch.setattr(PreparedProjectionReplace, "publish", fail_publish)
    changed = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1].replace(
            "OpenAccount", "ConfirmAccount"
        ),
    )

    fresh_status = GitSpecificationStore(
        str(wiki), "optional"
    ).status("payments")
    assert changed["specifications"]["state"] == "stale"
    assert projection_path.read_bytes() == projection_before
    assert fresh_status.state == "stale"
    assert fresh_status.reason == "out_of_band_change"

    monkeypatch.setattr(PreparedProjectionReplace, "publish", real_publish)
    rebuilt = server.wiki_index("payments")
    rebuilt_status = GitSpecificationStore(
        str(wiki), "optional"
    ).status("payments")
    assert rebuilt["specifications"]["state"] == "ready"
    assert rebuilt_status.state == "ready"


def test_ordinary_only_mutation_does_not_stale_ready_projection(
    tmp_path, monkeypatch
):
    wiki, _ = _seed(tmp_path, monkeypatch, "optional")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created

    ordinary = server.wiki_write_page(
        "payments", "notes", "# Notes\n\n## Body\nOrdinary change.\n"
    )

    assert "error" not in ordinary
    assert GitSpecificationStore(str(wiki), "optional").status(
        "payments"
    ).state == "ready"


def test_empty_semantic_source_set_is_stale_until_empty_rebuild(
    tmp_path, monkeypatch
):
    wiki, domain = _seed(tmp_path, monkeypatch, "optional")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    (domain / "specification" / "account.md").unlink()
    (domain / "seed.md").unlink()

    stale = GitSpecificationStore(str(wiki), "optional").status("payments")
    assert stale.state == "stale"
    assert stale.reason == "out_of_band_change"

    rebuilt = server.wiki_index("payments")
    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    ready = GitSpecificationStore(str(wiki), "optional").status("payments")
    assert rebuilt["specifications"]["state"] == "ready"
    assert projection.scenario_count == 0
    assert ready.state == "ready"


def test_optional_heading_projection_edit_failure_keeps_prior_projection(
    tmp_path, monkeypatch
):
    _, domain = _seed(tmp_path, monkeypatch, "optional")
    created = server.wiki_write_page(
        "payments", "account", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in created
    projection_path = domain / "specifications.jsonl"
    projection_before = projection_path.read_bytes()
    real_apply = server.cross_domain._apply_edit

    def fail_projection_edit(base_dir, edit):
        if edit.file == "specifications.jsonl":
            raise RuntimeError("private projection failure")
        return real_apply(base_dir, edit)

    monkeypatch.setattr(server.cross_domain, "_apply_edit", fail_projection_edit)

    result = server.wiki_update_page(
        "payments",
        "specification/account",
        "Open account",
        VALID_SPECIFICATION.split("## Open account\n\n", 1)[1],
        new_heading="Confirm account",
    )

    assert "error" not in result
    assert result["specifications"]["state"] == "stale"
    assert result["specifications"]["warning"] == (
        "specification projection is stale"
    )
    assert projection_path.read_bytes() == projection_before
    assert "## Confirm account" in (
        domain / "specification" / "account.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "markdown", "page_type", "slug", "heading", "replacement"),
    [
        (
            "strict",
            "# Notes\n\n## Body\nordinary\n",
            None,
            "concept/notes",
            "Body",
            "updated ordinary",
        ),
        (
            "disabled",
            VALID_SPECIFICATION,
            "specification",
            "specification/notes",
            "Open account",
            VALID_SPECIFICATION.split("## Open account\n\n", 1)[1],
        ),
    ],
)
def test_all_mutation_surfaces_bypass_projection_when_not_active(
    tmp_path, monkeypatch, mode, markdown, page_type, slug, heading, replacement
):
    _, domain = _seed(tmp_path, monkeypatch, mode)
    monkeypatch.setattr(
        server,
        "_assemble_specification_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bypass path parsed specification")
        ),
    )
    monkeypatch.setattr(
        server,
        "_git_specification_store_factory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bypass path opened projection")
        ),
    )

    written = server.wiki_write_page(
        "payments", "notes", markdown, type=page_type
    )
    updated = server.wiki_update_page(
        "payments", slug, heading, replacement
    )
    inserted = server.wiki_insert_section(
        "payments", slug, "Extra", "extra body"
    )
    moved = server.wiki_move_section(
        "payments", slug, "Extra", before_heading=heading
    )
    deleted_section = server.wiki_delete_section(
        "payments", slug, "Extra"
    )
    indexed = server.wiki_index("payments")
    deleted_page = server.wiki_delete_page("payments", slug)

    for result in (
        written,
        updated,
        inserted,
        moved,
        deleted_section,
        indexed,
        deleted_page,
    ):
        assert "error" not in result
        assert "specifications" not in result
    assert not (domain / "specifications.jsonl").exists()


@pytest.mark.parametrize(
    ("markdown", "finding_type"),
    [
        ("# Missing\n\n## Scenario\nNo block.\n", "missing_scenario"),
        (
            VALID_SPECIFICATION.replace(
                '  { relation = "verifies", file = "tests/test_account.py" }\n',
                "",
            ),
            "incomplete_bindings",
        ),
        (
            VALID_SPECIFICATION.replace('id = "open-account"', 'id = "BAD ID"'),
            "invalid_scenario",
        ),
    ],
)
def test_strict_invalid_semantics_reject_before_new_page_visibility(
    tmp_path, monkeypatch, markdown, finding_type
):
    wiki, domain = _seed(tmp_path, monkeypatch, "strict")
    head_before = _git(wiki, "rev-parse", "HEAD")

    result = server.wiki_write_page(
        "payments", "invalid", markdown, type="specification"
    )

    assert result["error"] == "specification validation failed"
    assert any(
        item["type"] == finding_type
        for item in result["specifications"]["findings"]
    )
    assert not (domain / "specification" / "invalid.md").exists()
    assert _git(wiki, "rev-parse", "HEAD") == head_before


@pytest.mark.parametrize(
    ("markdown", "finding_type"),
    [
        ("# Missing\n\n## Scenario\nNo block.\n", "missing_scenario"),
        (
            VALID_SPECIFICATION.replace(
                '  { relation = "verifies", file = "tests/test_account.py" }\n',
                "",
            ),
            "incomplete_bindings",
        ),
        (
            VALID_SPECIFICATION.replace('id = "open-account"', 'id = "BAD ID"'),
            "invalid_scenario",
        ),
    ],
)
def test_optional_invalid_semantics_are_advisory_and_excluded(
    tmp_path, monkeypatch, markdown, finding_type
):
    _, domain = _seed(tmp_path, monkeypatch, "optional")

    result = server.wiki_write_page(
        "payments", "invalid", markdown, type="specification"
    )

    projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
    assert "error" not in result
    assert any(
        item["type"] == finding_type
        for item in result["specifications"]["findings"]
    )
    assert projection.scenario_count == 0


@pytest.mark.parametrize("mode", ["strict", "optional"])
def test_domain_duplicate_mode_policy_is_enforced(tmp_path, monkeypatch, mode):
    wiki, domain = _seed(tmp_path, monkeypatch, mode)
    first = server.wiki_write_page(
        "payments", "first", VALID_SPECIFICATION, type="specification"
    )
    assert "error" not in first
    head_before = _git(wiki, "rev-parse", "HEAD")

    second = server.wiki_write_page(
        "payments", "second", VALID_SPECIFICATION, type="specification"
    )

    if mode == "strict":
        assert second["error"] == "specification validation failed"
        assert not (domain / "specification" / "second.md").exists()
        assert _git(wiki, "rev-parse", "HEAD") == head_before
    else:
        projection = decode_jsonl((domain / "specifications.jsonl").read_bytes())
        assert "error" not in second
        assert projection.scenario_count == 0
        finding = second["specifications"]["findings"][0]
        assert finding["type"] == "duplicate_scenario_id"
        assert [item["slug"] for item in finding["locations"]] == [
            "specification/first",
            "specification/second",
        ]
