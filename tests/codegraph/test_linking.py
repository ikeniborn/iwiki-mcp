"""Wiki selector grammar and derived code-link contracts."""
from __future__ import annotations

from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import json
import sys
import threading
import time

import pytest
from filelock import Timeout

from iwiki_mcp import indexer as wiki_indexer
from iwiki_mcp import server
from iwiki_mcp.codegraph import linking
from iwiki_mcp.codegraph import indexer as codegraph_indexer
from iwiki_mcp.codegraph.context import CodeGraphContext, validate_context_request
from iwiki_mcp.codegraph.indexer import CodeGraphStaleError
from iwiki_mcp.codegraph.linking import (
    SelectorError,
    SelectorSnapshotChanged,
    WikiPageSnapshot,
    WikiSelectorResolver,
    WikiSelectorSnapshot,
    resolve_selectors,
    selector_capture_budget,
)


def test_selectors_materialize_only_symbol_or_file_targets(link_fixture):
    links = resolve_selectors(
        link_fixture.markdown,
        link_fixture.snapshot,
        domain="project",
        page_id="project/concept/service",
    )

    assert {row["selector_kind"] for row in links} == {
        "symbol", "file", "source_glob",
    }
    assert all(
        (row["symbol_id"] is None) != (row["file_id"] is None)
        for row in links
    )
    assert not any("module_id" in row for row in links)
    assert {row["relation_type"] for row in links} == {"DOCUMENTED_BY"}


def test_specificity_deduplicates_file_target_and_retains_provenance(link_fixture):
    links = resolve_selectors(
        link_fixture.markdown,
        link_fixture.snapshot,
        domain="project",
        page_id="project/concept/service",
    )

    service_file = [
        row for row in links if row["file_id"] == link_fixture.service_file_id
    ]
    assert len(service_file) == 1
    assert service_file[0]["selector_kind"] == "file"
    assert service_file[0]["source"] == "src/pkg/service.py"
    glob_link = next(row for row in links if row["selector_kind"] == "source_glob")
    assert glob_link["source"] == "src/pkg/**"


@pytest.mark.parametrize(
    "code",
    [
        {"modules": ["pkg.service"]},
        {"module_id": ["py:module:" + "a" * 64]},
        {"aliases": ["ServiceAlias"]},
        {"imports": ["pkg.service.Service"]},
        {"symbols": [{"qualified_name": "pkg.Service", "alias": "S"}]},
        {"files": [{"module_id": "pkg.service"}]},
        {"source_globs": ["../**"]},
        {"files": ["/etc/passwd"]},
        {"files": ["."]},
        {"files": ["src//pkg/service.py"]},
        {"source_globs": ["/".join(["**"] * 1200)]},
    ],
)
def test_selector_grammar_rejects_modules_aliases_bindings_unknown_and_unsafe(
    link_fixture, code
):
    with pytest.raises(SelectorError):
        resolve_selectors(
            {"code": code},
            link_fixture.snapshot,
            domain="project",
            page_id="project/concept/service",
        )


def test_resolution_is_deterministic_and_bounded(link_fixture):
    kwargs = {
        "domain": "project",
        "page_id": "project/concept/service",
    }
    first = resolve_selectors(link_fixture.markdown, link_fixture.snapshot, **kwargs)
    second = resolve_selectors(link_fixture.markdown, link_fixture.snapshot, **kwargs)

    assert first == second
    assert [row["link_id"] for row in first] == sorted(
        row["link_id"] for row in first
    )
    with pytest.raises(SelectorError):
        resolve_selectors(
            {"code": {"files": ["x" * 4097]}},
            link_fixture.snapshot,
            **kwargs,
        )


def test_resolution_deduplicates_repeated_globs_before_matching(
    link_fixture, monkeypatch
):
    calls = 0
    original = linking._glob_matches

    def counted(path, pattern):
        nonlocal calls
        calls += 1
        return original(path, pattern)

    monkeypatch.setattr(linking, "_glob_matches", counted)
    repeated = {"code": {"source_globs": ["src/pkg/**"] * 256}}

    links = resolve_selectors(repeated, link_fixture.snapshot)

    assert links == resolve_selectors(
        {"code": {"source_globs": ["src/pkg/**"]}}, link_fixture.snapshot
    )
    assert calls <= len(link_fixture.snapshot["files"]) * 2


def test_resolution_cooperatively_aborts_inside_bounded_work(link_fixture):
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        resolve_selectors(
            link_fixture.markdown,
            link_fixture.snapshot,
            check_control=interrupt,
        )
    assert calls == 1


def test_nested_unknown_symbol_key_is_not_dropped_before_grammar_validation(
    link_fixture
):
    markdown = (
        "---\ncode:\n  symbols:\n"
        "    - qualified_name: pkg.service.Service.run\n"
        "      module_id: forbidden\n---\n# Service\n"
    )

    with pytest.raises(SelectorError):
        resolve_selectors(markdown, link_fixture.snapshot)


def test_single_star_does_not_cross_path_segments(link_fixture):
    markdown = (
        "---\ncode:\n  source_globs:\n    - src/*\n---\n# Source\n"
    )

    assert resolve_selectors(markdown, link_fixture.snapshot) == ()


def test_removed_page_is_removed_from_next_materialized_snapshot(
    tmp_path, link_fixture
):
    base = tmp_path / "base"
    page = base / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(link_fixture.markdown, encoding="utf-8")
    resolver = WikiSelectorResolver(base)
    kwargs = {
        "domain": "project",
        "project_dir": str(tmp_path / "project"),
        "parsed_files": (),
        "relations": (),
        "snapshot": link_fixture.snapshot,
    }

    assert resolver.resolve(**kwargs)
    page.unlink()
    assert resolver.resolve(**kwargs) == ()


def test_selector_capture_is_compact_and_prose_does_not_change_fingerprint(
    tmp_path
):
    base = tmp_path / "base"
    page = base / "project" / "concept" / "large.md"
    page.parent.mkdir(parents=True)
    header = "---\ncode:\n  files:\n    - src/a.py\n---\n"
    page.write_text(header + "x" * 1_000_000, encoding="utf-8")
    resolver = WikiSelectorResolver(base)

    first = resolver.capture(domain="project")
    page.write_text(header + "y" * 1_000_000, encoding="utf-8")
    second = resolver.capture(domain="project")

    assert first.fingerprint == second.fingerprint
    assert not hasattr(first.pages[0], "content")
    assert len(repr(first.pages[0])) < 2_000
    assert first.pages[0].selectors == {
        "symbols": [], "files": ["src/a.py"], "source_globs": [],
    }


def test_selector_verification_reads_only_frontmatter_evidence(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    page = base / "project" / "large.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n---\n" + "x" * 1_000_000,
        encoding="utf-8",
    )
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    original_read = linking.os.read
    read_bytes = 0

    def counted_read(descriptor, size):
        nonlocal read_bytes
        data = original_read(descriptor, size)
        read_bytes += len(data)
        return data

    monkeypatch.setattr(linking.os, "read", counted_read)

    resolver.verify_snapshot(snapshot)

    assert read_bytes <= 4100


def test_selector_verification_checks_deadline_after_last_page(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n---\n# Page\n",
        encoding="utf-8",
    )
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    checks = 0

    def deadline():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Timeout("selector-deadline")

    with pytest.raises(Timeout):
        resolver.verify_snapshot(snapshot, check_control=deadline)


def test_capture_accepts_maximum_legal_selector_frontmatter(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "maximum.md"
    page.parent.mkdir(parents=True)
    selectors = {
        "symbols": [
            {"qualified_name": f"?{index:03d}" + "\x01" * 4092}
            for index in range(256)
        ],
    }
    markdown = linking.frontmatter.render({
        "description": "p" * 4000,
        "code": selectors,
    }) + "# Maximum\n"
    page.write_text(markdown, encoding="utf-8")

    snapshot = WikiSelectorResolver(base).capture(domain="project")

    assert len(snapshot.pages[0].selectors["symbols"]) == 256
    assert selector_capture_budget(1, 1) >= len(markdown.encode("utf-8"))
    assert selector_capture_budget(1, 1) >= linking._MAX_WIKI_PAGE_BYTES


def test_capture_accepts_frontmatter_delimiter_near_page_byte_cap(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "near-cap.md"
    page.parent.mkdir(parents=True)
    suffix = "\ncode:\n  files:\n    - src/a.py\n---\n# Page\n"
    prefix = "---\ndescription: "
    padding = "p" * (
        linking._MAX_WIKI_PAGE_BYTES
        - len(prefix.encode("utf-8"))
        - len(suffix.encode("utf-8"))
    )
    page.write_text(prefix + padding + suffix, encoding="utf-8")

    snapshot = WikiSelectorResolver(base).capture(domain="project")
    try:
        assert snapshot.pages[0].selectors == {
            "symbols": [],
            "files": ["src/a.py"],
            "source_globs": [],
        }
    finally:
        WikiSelectorResolver(base).close_snapshot(snapshot)


def test_capture_rejects_page_over_page_byte_cap(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "over-cap.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"x" * (linking._MAX_WIKI_PAGE_BYTES + 1))

    with pytest.raises(SelectorError, match="page byte budget"):
        WikiSelectorResolver(base).capture(domain="project")


def test_capture_rejects_missing_domain_before_it_can_become_nonempty(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    resolver = WikiSelectorResolver(base)

    with pytest.raises(SelectorError, match="Wiki domain unavailable"):
        resolver.capture(domain="project")

    page = base / "project" / "page.md"
    page.parent.mkdir()
    page.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n---\n# Page\n",
        encoding="utf-8",
    )
    snapshot = resolver.capture(domain="project")
    resolver.close_snapshot(snapshot)


def test_capture_accepts_empty_existing_domain_as_watched_snapshot(tmp_path):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    try:
        assert snapshot.pages == ()
        resolver.verify_snapshot(snapshot)
    finally:
        resolver.close_snapshot(snapshot)


def test_compact_snapshot_payload_does_not_scale_with_irrelevant_page_bodies():
    pages = tuple(
        WikiPageSnapshot(
            relative=f"concept/page-{index}.md",
            page_id=f"project/concept/page-{index}",
            content_hash="a" * 64,
            selectors=None,
        )
        for index in range(10_000)
    )
    snapshot = WikiSelectorSnapshot(
        domain="project",
        pages=pages,
        fingerprint="b" * 64,
        generation_fingerprint="c" * 64,
        max_bytes=64_000_000,
    )
    retained_payload = sum(
        sys.getsizeof(page)
        + sys.getsizeof(page.relative)
        + sys.getsizeof(page.page_id)
        + sys.getsizeof(page.content_hash)
        for page in snapshot.pages
    )

    assert tuple(WikiPageSnapshot.__dataclass_fields__) == (
        "relative", "page_id", "content_hash", "selectors",
    )
    assert retained_payload < 4_000_000


def test_capture_rejects_aggregate_raw_work_without_partial_snapshot(tmp_path):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    for index in range(3):
        domain.joinpath(f"{index}.md").write_text(
            "---\ncode:\n  files:\n    - src/a.py\n---\n" + "x" * 512,
            encoding="utf-8",
        )

    with pytest.raises(SelectorError, match="capture budget"):
        WikiSelectorResolver(base).capture(domain="project", max_bytes=700)


def test_capture_bounds_directory_traversal_at_exactly_ten_thousand_entries(
    tmp_path,
):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    for index in range(10_001):
        domain.joinpath(f"irrelevant-{index}.txt").write_text("x", encoding="utf-8")
    checks = 0

    def check():
        nonlocal checks
        checks += 1

    with pytest.raises(SelectorError, match="traversal budget"):
        WikiSelectorResolver(base).capture(
            domain="project", check_control=check, max_bytes=64_000_000
        )

    assert linking._MAX_WIKI_ENTRIES == 10_000
    assert checks == 10_001


def test_snapshot_verification_is_constant_work_and_detects_out_of_band_edit(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n---\n# Page\n",
        encoding="utf-8",
    )
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    monkeypatch.setattr(
        resolver,
        "_page_descriptors",
        lambda *args, **kwargs: pytest.fail("verification rescanned Wiki pages"),
    )

    resolver.verify_snapshot(snapshot)
    page.write_text(
        "---\ncode:\n  files:\n    - src/b.py\n---\n# Page\n",
        encoding="utf-8",
    )

    with pytest.raises(SelectorSnapshotChanged):
        resolver.verify_snapshot(snapshot)

    resolver.close_snapshot(snapshot)


@pytest.mark.parametrize("operation", ["create", "delete", "rename", "attrib"])
def test_snapshot_watch_rejects_every_out_of_band_tree_change(
    tmp_path, operation
):
    base = tmp_path / "base"
    domain = base / "project"
    page = domain / "page.md"
    domain.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    try:
        if operation == "create":
            domain.joinpath("created.md").write_text("# New\n", encoding="utf-8")
        elif operation == "delete":
            page.unlink()
        elif operation == "rename":
            page.rename(domain / "renamed.md")
        else:
            page.chmod(0o600)

        with pytest.raises(SelectorSnapshotChanged):
            resolver.verify_snapshot(snapshot)
    finally:
        resolver.close_snapshot(snapshot)


def test_snapshot_watch_rejects_write_through_external_hardlink(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    outside = tmp_path / "outside.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n---\n# Page\n",
        encoding="utf-8",
    )
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    os.link(page, outside)
    outside.write_text(
        "---\ncode:\n  files:\n    - src/b.py\n---\n# Page\n",
        encoding="utf-8",
    )

    try:
        with pytest.raises(SelectorSnapshotChanged):
            resolver.verify_snapshot(snapshot)
    finally:
        resolver.close_snapshot(snapshot)


def test_capture_rejects_page_hardlinked_before_capture(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    os.link(page, tmp_path / "outside.md")

    with pytest.raises(SelectorError, match="unsafe Wiki page"):
        WikiSelectorResolver(base).capture(domain="project")


def test_capture_installs_directory_watch_before_enumeration(tmp_path, monkeypatch):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    original = linking._SelectorWatch.add_directory
    created = False

    def observed(watch, descriptor):
        nonlocal created
        original(watch, descriptor)
        if not created:
            created = True
            domain.joinpath("raced.md").write_text("# Raced\n", encoding="utf-8")

    monkeypatch.setattr(linking._SelectorWatch, "add_directory", observed)

    with pytest.raises(SelectorSnapshotChanged):
        WikiSelectorResolver(base).capture(domain="project")


def test_repeated_selector_snapshots_release_watch_descriptors(tmp_path):
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        pytest.skip("descriptor accounting requires procfs")
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    resolver = WikiSelectorResolver(base)
    before = len(tuple(proc_fds.iterdir()))

    for _ in range(500):
        snapshot = resolver.capture(domain="project")
        resolver.verify_snapshot(snapshot)
        resolver.close_snapshot(snapshot)

    assert len(tuple(proc_fds.iterdir())) == before


def test_five_hundred_context_requests_release_selector_watch_descriptors(
    ready_context
):
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        pytest.skip("descriptor accounting requires procfs")
    before = len(tuple(proc_fds.iterdir()))

    for _ in range(500):
        response = ready_context.context(
            [ready_context.run_symbol_id], include_wiki=True
        )
        assert response["fresh"] is True

    assert len(tuple(proc_fds.iterdir())) == before


def test_flat_ten_thousand_entry_capture_and_verification_stay_bounded(tmp_path):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    for index in range(10_000):
        domain.joinpath(f"irrelevant-{index}.txt").write_text("x", encoding="utf-8")
    resolver = WikiSelectorResolver(base)
    started = time.monotonic()

    snapshot = resolver.capture(domain="project")
    resolver.verify_snapshot(snapshot)
    elapsed = time.monotonic() - started
    resolver.close_snapshot(snapshot)

    assert elapsed < 0.3


def test_flat_ten_thousand_pages_use_at_most_ten_thousand_one_watches(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    for index in range(10_000):
        domain.joinpath(f"page-{index}.md").write_text("", encoding="utf-8")
    original = linking._SelectorWatch.add_directory
    watches = 0

    def counted(watch, descriptor):
        nonlocal watches
        watches += 1
        return original(watch, descriptor)

    monkeypatch.setattr(linking._SelectorWatch, "add_directory", counted)
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    resolver.close_snapshot(snapshot)

    assert watches == 10_001


def test_selector_watch_overflow_or_ignored_event_fails_closed(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    descriptor = snapshot._watch._descriptor
    original_read = linking.os.read

    def event(read_descriptor, size):
        if read_descriptor == descriptor:
            return b"\0" * 16
        return original_read(read_descriptor, size)

    monkeypatch.setattr(linking.os, "read", event)
    try:
        with pytest.raises(SelectorSnapshotChanged):
            resolver.verify_snapshot(snapshot)
    finally:
        resolver.close_snapshot(snapshot)


def test_context_with_ten_thousand_flat_wiki_entries_stays_bounded(
    ready_context
):
    domain = Path(ready_context.binding.base) / "project"
    existing_entries = sum(
        len(directories) + len(files)
        for _, directories, files in os.walk(domain)
    )
    for index in range(10_000 - existing_entries):
        domain.joinpath(f"irrelevant-{index}.txt").write_text("x", encoding="utf-8")
    started = time.monotonic()

    response = ready_context.context(
        [ready_context.run_symbol_id], include_wiki=True
    )
    elapsed = time.monotonic() - started

    assert response["fresh"] is True
    assert elapsed < 0.3


def test_closed_or_invalidated_selector_watch_fails_closed(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    resolver = WikiSelectorResolver(base)
    snapshot = resolver.capture(domain="project")
    resolver.close_snapshot(snapshot)

    with pytest.raises(SelectorSnapshotChanged, match="watch is invalid"):
        resolver.verify_snapshot(snapshot)


def test_capture_deep_tree_uses_linear_descriptor_work(tmp_path, monkeypatch):
    base = tmp_path / "base"
    directory = base / "project"
    directory.mkdir(parents=True)
    depth = 40
    for index in range(depth):
        directory = directory / f"d{index}"
        directory.mkdir()
    directory.joinpath("page.md").write_text("# Deep\n", encoding="utf-8")
    original_open = linking.os.open
    opens = 0

    def counted_open(*args, **kwargs):
        nonlocal opens
        opens += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(linking.os, "open", counted_open)

    WikiSelectorResolver(base).capture(domain="project", check_control=lambda: None)

    assert opens < depth * 4


def test_capture_wide_tree_keeps_descriptor_width_bounded(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    domain = base / "project"
    domain.mkdir(parents=True)
    for index in range(100):
        directory = domain / f"d{index}"
        directory.mkdir()
        directory.joinpath("page.md").write_text("# Page\n", encoding="utf-8")
    original_open = linking.os.open
    original_close = linking.os.close
    live = 0
    maximum = 0

    def tracked_open(*args, **kwargs):
        nonlocal live, maximum
        descriptor = original_open(*args, **kwargs)
        live += 1
        maximum = max(maximum, live)
        return descriptor

    def tracked_close(descriptor):
        nonlocal live
        original_close(descriptor)
        live -= 1

    monkeypatch.setattr(linking.os, "open", tracked_open)
    monkeypatch.setattr(linking.os, "close", tracked_close)

    snapshot = WikiSelectorResolver(base).capture(domain="project")

    assert len(snapshot.pages) == 100
    assert maximum < 16


def test_capture_deadline_releases_owned_directory_descriptors(tmp_path):
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        pytest.skip("descriptor accounting requires procfs")
    base = tmp_path / "base"
    page = base / "project" / "child" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    before = len(tuple(proc_fds.iterdir()))

    for _attempt in range(10):
        with pytest.raises(Timeout):
            WikiSelectorResolver(base).capture(
                domain="project",
                check_control=lambda: (_ for _ in ()).throw(
                    Timeout("selector-deadline")
                ),
            )

    assert len(tuple(proc_fds.iterdir())) == before


def test_capture_treats_invalid_utf8_frontmatter_as_invalid_selector(tmp_path):
    base = tmp_path / "base"
    page = base / "project" / "invalid.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\ncode:\n  files: [\xff]\n---\n# Invalid\n")

    snapshot = WikiSelectorResolver(base).capture(domain="project")

    assert snapshot.pages[0].selectors == {"invalid": True}


def test_prose_only_wiki_edit_keeps_code_graph_noop(seed_runtime):
    page = Path(seed_runtime.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    header = "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n"
    page.write_text(header + "# Service\n\n## Notes\nA.\n", encoding="utf-8")
    first = seed_runtime.index(force=True)
    page.write_text(header + "# Service\n\n## Notes\nB.\n", encoding="utf-8")

    second = seed_runtime.index()

    assert second["no_op"] is True
    assert second["revision"] == first["revision"]


def test_capture_uses_nofollow_descriptor_open(tmp_path, monkeypatch):
    base = tmp_path / "base"
    page = base / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ncode:\n  files: [\"src/a.py\"]\n---\n", encoding="utf-8")
    real_open = linking.os.open
    file_flags = []

    def observed(path, flags, *args, **kwargs):
        if str(path).endswith(".md"):
            file_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(linking.os, "open", observed)

    WikiSelectorResolver(base).capture(domain="project")

    assert file_flags
    assert all(flags & getattr(linking.os, "O_NOFOLLOW", 0) for flags in file_flags)


def test_capture_rejects_page_swapped_to_external_symlink_before_open(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    page = base / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ncode:\n  files: [\"src/a.py\"]\n---\n", encoding="utf-8")
    external = tmp_path / "secret.md"
    secret = b"EXTERNAL-SECRET"
    external.write_bytes(secret)
    original_open = linking.os.open
    swapped = False

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path) == "service.md" and not swapped:
            swapped = True
            page.unlink()
            page.symlink_to(external)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(linking.os, "open", race)

    with pytest.raises(SelectorError, match="unsafe Wiki page"):
        WikiSelectorResolver(base).capture(domain="project")

    assert external.read_bytes() == secret


def test_capture_fails_closed_without_safe_descriptor_traversal(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    page = base / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/a.py\n---\n# Service\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(linking, "_HAS_SAFE_DESCRIPTOR_TRAVERSAL", False)

    with pytest.raises(SelectorError, match="safe Wiki selector capture"):
        WikiSelectorResolver(base).capture(domain="project")


def test_runtime_rebuild_materializes_selectors_and_selector_change_breaks_noop(
    seed_runtime
):
    page = Path(seed_runtime.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n"
        "# Service\n\n## Notes\nAuthored.\n",
        encoding="utf-8",
    )

    built = seed_runtime.index(force=True)
    with closing(sqlite3.connect(seed_runtime.paths.database)) as connection:
        assert connection.execute(
            "SELECT selector_kind, source FROM wiki_code_links"
        ).fetchall() == [("file", "src/pkg/service.py")]
    assert seed_runtime.index()["no_op"] is True

    page.write_text(
        "---\ncode:\n  source_globs:\n    - src/pkg/**\n---\n"
        "# Service\n\n## Notes\nAuthored.\n",
        encoding="utf-8",
    )
    rebuilt = seed_runtime.index()

    assert built["revision"] != rebuilt["revision"]
    assert rebuilt["no_op"] is False
    with closing(sqlite3.connect(seed_runtime.paths.database)) as connection:
        assert {
            row[0] for row in connection.execute(
                "SELECT selector_kind FROM wiki_code_links"
            )
        } == {"source_glob"}


def test_rebuild_rejects_selector_page_change_between_capture_and_materialization(
    seed_runtime, monkeypatch
):
    assert seed_runtime.runtime._indexer.build(force=True)["state"] == "ready"
    page = Path(seed_runtime.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    selector_a = (
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n"
        "# Service\n\n## Notes\nA.\n"
    )
    selector_b = (
        "---\ncode:\n  source_globs:\n    - src/pkg/**\n---\n"
        "# Service\n\n## Notes\nB.\n"
    )
    page.write_text(selector_a, encoding="utf-8")
    resolver = seed_runtime.runtime._indexer.wiki_selector_resolver
    original = resolver.resolve_snapshot

    def race(*args, **kwargs):
        page.write_text(selector_b, encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(resolver, "resolve_snapshot", race)

    result = seed_runtime.index(force=True)

    assert result["code"] in {"stale", "busy"}
    assert seed_runtime.status()["state"] != "ready"
    with closing(sqlite3.connect(seed_runtime.paths.database)) as connection:
        assert connection.execute(
            "SELECT selector_kind, source FROM wiki_code_links"
        ).fetchall() == []


def test_selector_race_cannot_restore_prior_ready_state(
    seed_runtime, monkeypatch
):
    assert seed_runtime.index(force=True)["state"] == "ready"
    page = Path(seed_runtime.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n# Service\n",
        encoding="utf-8",
    )
    resolver = seed_runtime.runtime._indexer.wiki_selector_resolver
    original = resolver.resolve_snapshot

    def race(*args, **kwargs):
        page.write_text(
            "---\ncode:\n  source_globs:\n    - src/pkg/**\n---\n# Service\n",
            encoding="utf-8",
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(resolver, "resolve_snapshot", race)

    with pytest.raises(CodeGraphStaleError):
        seed_runtime.runtime._indexer.build(
            force=True, restore_prior_on_abort=True
        )

    assert seed_runtime.status()["state"] != "ready"


def test_selector_change_after_post_replace_verify_cannot_return_ready(
    seed_runtime, monkeypatch
):
    assert seed_runtime.index(force=True)["state"] == "ready"
    page = Path(seed_runtime.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n# Service\n",
        encoding="utf-8",
    )
    resolver = seed_runtime.runtime._indexer.wiki_selector_resolver
    original = resolver.verify_snapshot
    calls = 0

    def race(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 4:
            page.write_text(
                "---\ncode:\n  source_globs:\n    - src/pkg/**\n---\n# Service\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(resolver, "verify_snapshot", race)

    result = seed_runtime.index(force=True)

    assert result["code"] in {"stale", "busy"}
    assert seed_runtime.status()["state"] != "ready"


def test_rebuild_holds_shared_wiki_mutation_lock_through_final_verify(
    seed_runtime, monkeypatch
):
    held = False
    original_lock = codegraph_indexer._wiki_read_lock

    @contextmanager
    def observed_lock(base, timeout=15.0):
        nonlocal held
        with original_lock(base, timeout):
            held = True
            try:
                yield
            finally:
                held = False

    resolver = seed_runtime.runtime._indexer.wiki_selector_resolver
    original_verify = resolver.verify_snapshot
    observed = []

    def verify(*args, **kwargs):
        observed.append(held)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(codegraph_indexer, "_wiki_read_lock", observed_lock)
    monkeypatch.setattr(resolver, "verify_snapshot", verify)

    assert seed_runtime.runtime._indexer.build(force=True)["state"] == "ready"
    assert observed[-1] is True


def test_build_releases_wiki_mutation_lock_during_parse(seed_runtime, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_parse = seed_runtime.runtime._indexer._parse

    def paused_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(seed_runtime.runtime._indexer, "_parse", paused_parse)
    result = {}

    def build():
        try:
            result.update(seed_runtime.runtime._indexer.build(force=True))
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=build)
    thread.start()
    assert entered.wait(timeout=5)
    with codegraph_indexer.mutation_lock(seed_runtime.binding.base, timeout=1):
        pass
    release.set()
    thread.join(timeout=5)

    assert result.get("state") == "ready"


def test_real_wiki_write_completes_while_code_build_is_parsing(
    seed_runtime, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    original_parse = seed_runtime.runtime._indexer._parse

    def paused_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(seed_runtime.runtime._indexer, "_parse", paused_parse)
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://example.invalid")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test-key")
    monkeypatch.setattr(
        server.base, "resolve_binding", lambda: seed_runtime.binding
    )
    monkeypatch.setattr(
        server.sync, "ensure_fresh", lambda _base: {"state": "clean"}
    )
    monkeypatch.setattr(
        server.sync,
        "commit_and_push",
        lambda *_args, **_kwargs: {"committed": True, "pushed": False},
    )
    monkeypatch.setattr(
        wiki_indexer,
        "embed_texts",
        lambda _config, texts: [[0.1, 0.2] for _text in texts],
    )
    monkeypatch.chdir(seed_runtime.project_dir)
    build_result = {}

    def build():
        build_result.update(seed_runtime.index(force=True))

    thread = threading.Thread(target=build)
    thread.start()
    assert entered.wait(timeout=5)
    started = codegraph_indexer.time.monotonic()
    try:
        written = server.wiki_write_page(
            "project", "during-build",
            "# During Build\n\n## Overview\nWritten concurrently.\n",
            type="concept",
        )
        elapsed = codegraph_indexer.time.monotonic() - started
        assert written.get("page") == "project/concept/during-build.md", written
        assert elapsed < 1.5
        assert thread.is_alive()
    finally:
        release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert build_result["code"] in {"stale", "busy"}
    assert seed_runtime.status()["state"] != "ready"


def test_publication_lease_contention_aborts_without_lock_cycle(
    seed_runtime, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    base_held = threading.Event()
    writer_finished = threading.Event()
    original_parse = seed_runtime.runtime._indexer._parse

    def paused_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(seed_runtime.runtime._indexer, "_parse", paused_parse)
    result = {}
    build = threading.Thread(
        target=lambda: result.update(seed_runtime.index(force=True))
    )
    build.start()
    assert entered.wait(timeout=5)

    def mutate():
        with codegraph_indexer.mutation_lock(
            seed_runtime.binding.base, timeout=5
        ):
            base_held.set()
            with codegraph_indexer.code_graph_write_lock(
                seed_runtime.paths.lock, timeout=5
            ):
                writer_finished.set()

    writer = threading.Thread(target=mutate)
    writer.start()
    assert base_held.wait(timeout=5)
    release.set()
    build.join(timeout=5)
    writer.join(timeout=5)

    assert not build.is_alive()
    assert not writer.is_alive()
    assert writer_finished.is_set()
    assert result["code"] in {"busy", "rebuild_failed"}


def test_freshness_fast_path_holds_shared_wiki_mutation_lock(
    seed_runtime, monkeypatch
):
    assert seed_runtime.index(force=True)["state"] == "ready"
    held = False
    original_lock = codegraph_indexer._wiki_read_lock

    @contextmanager
    def observed_lock(base, timeout=15.0):
        nonlocal held
        with original_lock(base, timeout):
            held = True
            try:
                yield
            finally:
                held = False

    original_ready = seed_runtime.runtime._indexer._ready_metadata

    def ready(*args, **kwargs):
        assert held
        return original_ready(*args, **kwargs)

    monkeypatch.setattr(codegraph_indexer, "_wiki_read_lock", observed_lock)
    monkeypatch.setattr(seed_runtime.runtime._indexer, "_ready_metadata", ready)

    assert seed_runtime.runtime._indexer.mark_dirty_if_stale() is False


def test_stale_cleanup_does_not_overwrite_newer_generation(
    seed_runtime, monkeypatch
):
    assert seed_runtime.index(force=True)["state"] == "ready"
    page = Path(seed_runtime.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n# Service\n",
        encoding="utf-8",
    )
    resolver = seed_runtime.runtime._indexer.wiki_selector_resolver
    original_resolve = resolver.resolve_snapshot

    def race(*args, **kwargs):
        page.write_text(
            "---\ncode:\n  source_globs:\n    - src/pkg/**\n---\n# Service\n",
            encoding="utf-8",
        )
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(resolver, "resolve_snapshot", race)
    indexer = seed_runtime.runtime._indexer
    original_cleanup = indexer._mark_selector_snapshot_dirty

    def interleave(*, generation, maximum):
        metadata = json.loads(
            seed_runtime.paths.metadata.read_text(encoding="utf-8")
        )
        metadata.update({
            "generation": generation + 1,
            "state": "ready",
            "fresh": True,
        })
        seed_runtime.paths.metadata.write_text(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return original_cleanup(generation=generation, maximum=maximum)

    monkeypatch.setattr(indexer, "_mark_selector_snapshot_dirty", interleave)

    with pytest.raises(CodeGraphStaleError):
        indexer.build(force=True)

    metadata = json.loads(seed_runtime.paths.metadata.read_text(encoding="utf-8"))
    assert metadata["generation"] > 2
    assert metadata["state"] == "ready"


def test_indexer_selector_seam_remains_compatible_with_exact_protocol(
    seed_runtime
):
    calls = []

    class ExactResolver:
        def resolve(
            self, *, domain, project_dir, parsed_files, relations
        ):
            calls.append((domain, project_dir, parsed_files, relations))
            return ()

    seed_runtime.runtime._indexer.wiki_selector_resolver = ExactResolver()

    result = seed_runtime.index(force=True)

    assert result["state"] == "ready"
    assert len(calls) == 1


def test_rebuild_resolver_materializes_pages_without_mutating_markdown(
    tmp_path, link_fixture
):
    base = tmp_path / "base"
    page = base / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True)
    page.write_text(link_fixture.markdown, encoding="utf-8")
    before = page.read_bytes()

    resolver = WikiSelectorResolver(base)
    links = resolver.resolve(
        domain="project",
        project_dir=str(tmp_path / "project"),
        parsed_files=(),
        relations=(),
        snapshot=link_fixture.snapshot,
    )

    assert links
    assert page.read_bytes() == before


def test_context_include_wiki_hydrates_derived_pages_without_authority_mutation(
    ready_context
):
    page = Path(ready_context.binding.base) / "project" / "concept" / "service.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        "---\ntype: concept\ncode:\n  symbols:\n"
        "    - qualified_name: pkg.service.Service.run\n---\n"
        "# Service\n\n## Notes\nAuthored.\n"
    )
    page.write_text(authored, encoding="utf-8")
    with closing(sqlite3.connect(ready_context.paths.database)) as connection:
        connection.execute(
            "INSERT INTO wiki_code_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "link-context", "project", "project/concept/service",
                ready_context.run_symbol_id, None, "symbol", "DOCUMENTED_BY",
                1.0, "pkg.service.Service.run",
            ),
        )
        connection.commit()
        engine = CodeGraphContext(
            "project", ready_context.runtime._context_root,
            ready_context.runtime.config.max_file_bytes,
        )
        included = engine.context(
            connection,
            validate_context_request([ready_context.run_symbol_id], include_wiki=True),
        )
        omitted = engine.context(
            connection,
            validate_context_request([ready_context.run_symbol_id], include_wiki=False),
        )

    assert included["wiki_pages"] == [{
        "domain": "project",
        "page_id": "project/concept/service",
        "relation_type": "DOCUMENTED_BY",
        "selector_kind": "symbol",
        "source": "pkg.service.Service.run",
    }]
    assert omitted["wiki_pages"] == []
    assert page.read_text(encoding="utf-8") == authored


def test_runtime_include_wiki_holds_selector_lease_after_guard_until_return(
    ready_context, monkeypatch
):
    guarded = threading.Event()
    release = threading.Event()
    writer_acquired = threading.Event()
    original_guard = ready_context.runtime.query_guard

    def paused_guard(**kwargs):
        result = original_guard(**kwargs)
        guarded.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(ready_context.runtime, "query_guard", paused_guard)
    response = {}

    reader = threading.Thread(target=lambda: response.update(
        ready_context.context(
            [ready_context.run_symbol_id], include_wiki=True
        )
    ))

    def mutate():
        with codegraph_indexer.mutation_lock(
            ready_context.binding.base, timeout=5
        ):
            writer_acquired.set()

    reader.start()
    assert guarded.wait(timeout=5)
    writer = threading.Thread(target=mutate)
    writer.start()
    assert not writer_acquired.wait(timeout=0.2)
    release.set()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert response["fresh"] is True
    assert writer_acquired.is_set()


def test_runtime_include_wiki_marks_stale_without_shared_lock_upgrade(
    ready_context
):
    Path(ready_context.binding.base).joinpath(
        "project", "concept", "changed.md"
    ).parent.mkdir(parents=True, exist_ok=True)
    Path(ready_context.binding.base).joinpath(
        "project", "concept", "changed.md"
    ).write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n# Changed\n",
        encoding="utf-8",
    )

    response = ready_context.context(
        [ready_context.run_symbol_id], include_wiki=True
    )

    assert response["code"] == "stale"
    assert response["fresh"] is False
    assert ready_context.status()["state"] == "dirty"


def test_runtime_include_wiki_reuses_one_selector_capture(
    ready_context, monkeypatch
):
    resolver = ready_context.runtime._indexer.wiki_selector_resolver
    original_capture = resolver.capture
    captures = 0

    def counted_capture(*args, **kwargs):
        nonlocal captures
        captures += 1
        assert callable(kwargs.get("check_control"))
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(resolver, "capture", counted_capture)

    response = ready_context.context(
        [ready_context.run_symbol_id], include_wiki=True
    )

    assert response["fresh"] is True
    assert captures == 1


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (Timeout("selector-deadline"), "busy"),
        (SelectorError("safe capture unavailable"), "stale"),
    ],
)
def test_runtime_selector_capture_failure_returns_exact_empty_context(
    ready_context, monkeypatch, failure, code
):
    resolver = ready_context.runtime._indexer.wiki_selector_resolver

    def fail_capture(*_args, **kwargs):
        assert callable(kwargs.get("check_control"))
        raise failure

    monkeypatch.setattr(resolver, "capture", fail_capture)

    response = ready_context.context(
        [ready_context.run_symbol_id],
        include_wiki=True,
        depth=2,
        max_nodes=7,
        max_files=3,
        max_source_bytes=1234,
    )

    assert set(response) == {
        "domain", "state", "revision", "seeds", "nodes", "relations",
        "files", "wiki_pages", "limits", "truncated", "warnings", "fresh",
        "error", "code", "hint",
    }
    assert response["code"] == code
    assert response["seeds"] == [ready_context.run_symbol_id]
    assert response["nodes"] == []
    assert response["relations"] == []
    assert response["files"] == []
    assert response["wiki_pages"] == []
    assert response["limits"] == {
        "depth": 2,
        "max_nodes": 7,
        "max_files": 3,
        "max_source_bytes": 1234,
    }
    assert response["truncated"] is False
    assert response["warnings"] == ready_context.status()["warnings"]
    assert response["fresh"] is False


def test_runtime_missing_wiki_domain_returns_complete_nonready_context(
    ready_context
):
    domain = Path(ready_context.binding.base) / "project"
    domain.rename(Path(ready_context.binding.base) / "project-missing")

    response = ready_context.context(
        [ready_context.run_symbol_id],
        include_wiki=True,
        depth=2,
        max_nodes=7,
        max_files=3,
        max_source_bytes=1234,
    )

    assert set(response) == {
        "domain", "state", "revision", "seeds", "nodes", "relations",
        "files", "wiki_pages", "limits", "truncated", "warnings", "fresh",
        "error", "code", "hint",
    }
    assert response["fresh"] is False
    assert response["code"] == "stale"
    assert response["nodes"] == []
    assert response["relations"] == []
    assert response["files"] == []
    assert response["wiki_pages"] == []


def test_rebuild_missing_wiki_domain_cannot_publish_ready(seed_runtime):
    assert seed_runtime.index(force=True)["state"] == "ready"
    domain = Path(seed_runtime.binding.base) / "project"
    domain.rename(Path(seed_runtime.binding.base) / "project-missing")

    result = seed_runtime.index(force=True)

    assert result.get("state") != "ready"
    assert seed_runtime.status()["state"] != "ready"


def test_runtime_context_without_wiki_does_not_take_selector_lease(
    ready_context, monkeypatch
):
    calls = 0
    original = codegraph_indexer._wiki_read_lock

    @contextmanager
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        with original(*args, **kwargs):
            yield

    monkeypatch.setattr(codegraph_indexer, "_wiki_read_lock", counted)

    response = ready_context.context(
        [ready_context.run_symbol_id], include_wiki=False
    )

    assert response["fresh"] is True
    assert response["wiki_pages"] == []
    assert calls == 1


def test_wiki_link_rows_cascade_on_symbol_file_and_repository(ready_runtime):
    result = ready_runtime.index(force=True)
    assert result["state"] == "ready"
    with closing(sqlite3.connect(ready_runtime.paths.database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        file_id, symbol_id = connection.execute(
            "SELECT f.file_id, s.symbol_id FROM files AS f "
            "JOIN symbols AS s ON s.file_id = f.file_id "
            "WHERE s.qualified_name = 'pkg.service.Service.run'"
        ).fetchone()
        rows = (
            ("link-file", "project", "project/page", None, file_id, "file",
             "DOCUMENTED_BY", 1.0, "src/pkg/service.py"),
            ("link-symbol", "project", "project/page", symbol_id, None, "symbol",
             "DOCUMENTED_BY", 1.0, "pkg.service.Service.run"),
        )
        connection.executemany(
            "INSERT INTO wiki_code_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        connection.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM wiki_code_links"
        ).fetchone() == (0,)
