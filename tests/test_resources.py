from pathlib import Path

from iwiki_mcp.resources import AUTHORING_RULES


def test_authoring_rules_cover_section_format():
    text = AUTHORING_RULES.lower()
    assert "description" in text          # description is the authored summary
    assert "](<type>/<slug>.md#heading)" in AUTHORING_RULES
    assert "[[" not in AUTHORING_RULES
    assert "##" in AUTHORING_RULES


def test_description_is_separate_summary_vector_not_prefix():
    # The stale two-level lie must be gone.
    assert "context prefix" not in AUTHORING_RULES
    # The corrected model must be stated.
    assert "summary-level vector" in AUTHORING_RULES


def test_links_use_type_slug_path_and_export_only_artifacts():
    assert "(<type>/<slug>.md#heading)" in AUTHORING_RULES
    assert "iwiki://<domain>/<page-id>#<anchor>" in AUTHORING_RULES
    assert "export-only" in AUTHORING_RULES


def test_authoring_rules_describe_current_search_and_update_tools():
    assert "hybrid`, `lexical`, and `semantic" in AUTHORING_RULES
    assert "IWIKI_SEARCH_MODE" in AUTHORING_RULES
    assert "IWIKI_RERANK_MODEL" in AUTHORING_RULES
    assert "wiki_update_page" in AUTHORING_RULES
    assert "wiki_remediation_plan" in AUTHORING_RULES


def _section(text, start, end):
    start_at = text.index(start) + len(start)
    end_at = text.index(end, start_at)
    return text[start_at:end_at]


def _update_page_contract_sections():
    root = Path(__file__).parents[1]
    return {
        "resource": _section(
            AUTHORING_RULES,
            "## Existing page updates",
            "## OKF frontmatter",
        ),
        "readme": _section(
            (root / "README.md").read_text(encoding="utf-8"),
            "## Tools",
            "## Pareto benchmark",
        ),
        "architecture": _section(
            (root / "docs/architecture.md").read_text(encoding="utf-8"),
            "### Transaction phase",
            "### Cross-domain rewrite coordinator",
        ),
        "russian": _section(
            (root / "docs/README.ru.md").read_text(encoding="utf-8"),
            "## Инструменты",
            "## Pareto-бенчмарк",
        ),
    }


def test_update_page_documentation_covers_selector_update_contract():
    sections = _update_page_contract_sections()

    english_markers = (
        "section-only",
        "code-only",
        "combined",
        "`heading`",
        "`new_body`",
        "`code`",
        "code.symbols",
        "code.files",
        "code.source_globs",
        "completely replaces",
        "all-empty lists",
        "`null`",
        "omits `heading`",
        "anyOf",
        "root-required",
        "unsafe selectors",
        "expected_revision",
        "one revision and transaction",
        "unchanged chunks reuse embeddings",
        "Code-graph Wiki links current",
    )
    for name in ("resource", "readme", "architecture"):
        assert all(marker in sections[name] for marker in english_markers), name

    russian_markers = (
        "обновление только секции",
        "обновление только метаданных `code`",
        "совмещённое обновление",
        "`heading`",
        "`new_body`",
        "code.symbols",
        "code.files",
        "code.source_globs",
        "полностью заменяет селекторы",
        "всеми пустыми списками",
        "`null`",
        "опускает `heading`",
        "anyOf",
        "обязательными на верхнем уровне",
        "небезопасные селекторы",
        "expected_revision",
        "одной ревизии и одной транзакции",
        "неизменённые чанки повторно используют embeddings",
        "Wiki-ссылки в Code Graph актуальными",
    )
    assert all(marker in sections["russian"] for marker in russian_markers)


def test_code_only_selector_updates_preserve_page_body_byte_for_byte():
    sections = _update_page_contract_sections()
    for name in ("resource", "readme", "architecture"):
        assert "body byte-for-byte" in sections[name], name
        assert "code-only uses `new_body`" not in sections[name], name

    russian = sections["russian"]
    assert "сохраняется байт в байт" in russian
    assert "только метаданные `code` используют `new_body`" not in russian


def test_agent_snippets_use_supported_existing_page_update_path():
    root = Path(__file__).parents[1]
    for relative in ("templates/AGENTS.md.snippet", "templates/CLAUDE.md.snippet"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "wiki_update_page" in text
        assert "iwiki://<domain>/<page-id>#<anchor>" in text
        assert ".iwiki/graph.sqlite3" in text
        assert "Do not imply the tool can update existing pages directly" not in text


def test_authoring_rules_explain_safe_cross_domain_rewrites():
    assert "new_heading" in AUTHORING_RULES
    assert "all visible referrers are writable" in AUTHORING_RULES
    assert "hidden domains are not inspected" in AUTHORING_RULES


def test_agent_snippets_explain_cross_domain_rewrite_boundary():
    root = Path(__file__).parents[1]
    for relative in ("templates/AGENTS.md.snippet", "templates/CLAUDE.md.snippet"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "new_heading" in text
        assert "all visible referrers are writable" in text


def test_public_readmes_describe_description_as_a_separate_summary_vector():
    root = Path(__file__).parents[1]
    english = (root / "README.md").read_text(encoding="utf-8")
    russian = (root / "docs/README.ru.md").read_text(encoding="utf-8")

    assert "embedded as each section's context prefix" not in english
    assert "stored as a separate summary vector" in english
    assert "встраивается как контекстный префикс каждой секции" not in russian
    assert "хранится как отдельный summary-вектор" in russian


def test_authoring_rules_cover_gwt_authoring_and_maintenance_contract():
    text = " ".join(AUTHORING_RULES.casefold().split())

    assert "new observable domain behavior" in text
    assert "public contract" in text
    assert "bug reproduction" in text
    assert "business invariant" in text
    assert "keep the existing scenario id" in text
    assert "given" in text and "prior domain facts" in text
    assert "when" in text and "one observable trigger" in text
    assert "then" in text and "exclusive exception" in text
    assert "given roles: `event`, `state`, `fact`" in text
    assert "when roles: `command`, `request`, `action`" in text
    assert "then roles: `event`, `response`, `outcome`, `exception`" in text
    assert "implements" in text and "verifies" in text
    assert "executable test" in text
    assert "stale" in text and "unresolved evidence" in text
    assert "graph is absent or unusable" in text
    assert "repository search" in text
    assert "one coherent unit" in text
    assert all(mode in text for mode in ("disabled", "optional", "strict"))
    assert all(
        tool in AUTHORING_RULES
        for tool in ("wiki_spec_search", "wiki_spec_context", "wiki_spec_resolve")
    )
    assert "ordinary wiki" in text
    assert "```iwiki-gwt" in AUTHORING_RULES
    assert 'id = "confirm-account-opening"' in AUTHORING_RULES
    assert 'relation = "implements"' in AUTHORING_RULES
    assert 'relation = "verifies"' in AUTHORING_RULES


def _assert_complete_gwt_contract_terms(text):
    required = (
        "missing_scenario",
        "invalid_scenario",
        "duplicate_scenario_id",
        "incomplete_bindings",
        "projection_stale",
        "projection_failed",
        "binding_unresolved",
        "binding_ambiguous",
        "resolution_not_checked",
        "resolution_stale_spec",
        "resolution_stale_graph",
        "graph_unavailable",
        "domain",
        "mode",
        "source",
        "projection_state",
        "scenarios",
        "bindings",
        "project | hosted_default | hosted_override | built_in_default",
        "disabled | absent | ready | stale | failed",
        "symbol",
        "file",
        "source_glob",
        "implements",
        "verifies",
    )
    assert all(term in text for term in required)


def test_authoring_rules_publish_complete_gwt_grammar_status_and_lint_contract():
    normalized = " ".join(AUTHORING_RULES.casefold().split())
    _assert_complete_gwt_contract_terms(AUTHORING_RULES)

    assert "`id` is required" in normalized
    assert "1-128 utf-8 bytes" in normalized
    assert "[a-z0-9]+(?:-[a-z0-9]+)*" in normalized
    assert "`title` is required and nonblank" in normalized
    assert "250 unicode code points" in normalized
    assert "phase-item `name` is required and nonblank" in normalized
    assert "1,024 utf-8 bytes" in normalized
    assert "nul" in normalized
    assert "unknown keys" in normalized
    assert "duplicate toml keys" in normalized
    assert "`given` is required and accepts 0 or more items" in normalized
    assert "`when` is required and contains exactly one item" in normalized
    assert "`then` is required and contains 1 or more items" in normalized
    assert "`code` is required and contains 1 or more bindings" in normalized
    assert "`phase` is optional" in normalized
    assert "exactly one of `symbol`, `file`, or `source_glob`" in normalized
    assert "disabled mode produces no projection and no specification findings" in normalized
    assert "optional mode makes every specification finding advisory" in normalized
    assert (
        "blocking only for future mutations of the reported explicit specification page"
        in normalized
    )
    assert "projection and resolution findings remain advisory" in normalized
    assert "ordinary wiki pages remain unaffected" in normalized


def test_gwt_documentation_covers_configuration_lifecycle_and_operations():
    root = Path(__file__).parents[1]
    documents = (
        root / "README.md",
        root / "docs/README.ru.md",
        root / "docs/architecture.md",
    )
    required = (
        "[specifications]",
        'mode = "optional"',
        "[[specifications.overrides]]",
        "disabled",
        "optional",
        "strict",
        "```iwiki-gwt",
        "wiki_spec_search",
        "wiki_spec_context",
        "wiki_spec_resolve",
        "wiki_status",
        "wiki_lint",
        "specifications.jsonl",
        "PostgreSQL",
        "fresh",
        "stale_spec",
        "stale_graph",
        "graph_unavailable",
        "read scope",
        "write scope",
        "uv run pytest -q -m measurement tests/measurement/test_specification_paths.py -s",
    )

    for path in documents:
        text = path.read_text(encoding="utf-8")
        assert all(term in text for term in required), path
        _assert_complete_gwt_contract_terms(text)

    assert "ordinary wiki pages" in documents[0].read_text(encoding="utf-8").casefold()
    assert "обычные wiki-страницы" in documents[1].read_text(encoding="utf-8").casefold()
    assert "ordinary wiki pages" in documents[2].read_text(encoding="utf-8").casefold()


def test_english_docs_publish_complete_gwt_semantics():
    root = Path(__file__).parents[1]
    documents = (root / "README.md", root / "docs/architecture.md")
    required = (
        "`id` is required",
        "1-128 UTF-8 bytes",
        "`title` is required and nonblank",
        "250 Unicode code points",
        "phase-item `name` is required and nonblank",
        "1,024 UTF-8 bytes",
        "unknown keys",
        "duplicate TOML keys",
        "`given` is required and accepts 0 or more items",
        "`when` is required and contains exactly one item",
        "`then` is required and contains 1 or more items",
        "`code` is required and contains 1 or more bindings",
        "`phase` is optional",
        "exactly one of `symbol`, `file`, or `source_glob`",
        "disabled mode produces no projection and no specification findings",
        "optional mode makes every specification finding advisory",
        "blocking only for future mutations of the reported explicit specification page",
        "projection and resolution findings remain advisory",
        "ordinary Wiki pages remain unaffected",
    )
    for path in documents:
        normalized = " ".join(path.read_text(encoding="utf-8").casefold().split())
        assert all(term.casefold() in normalized for term in required), path


def test_russian_docs_publish_complete_gwt_semantics_in_russian():
    root = Path(__file__).parents[1]
    text = " ".join((root / "docs/README.ru.md").read_text(encoding="utf-8").split())
    required = (
        "`id` обязателен",
        "1–128 байт UTF-8",
        "`title` обязателен и не может быть пустым",
        "250 Unicode code points",
        "`name` каждого элемента фазы обязателен и не может быть пустым",
        "1 024 байт UTF-8",
        "Неизвестные ключи",
        "повторяющиеся TOML-ключи",
        "`given` обязателен и содержит 0 или больше элементов",
        "`when` обязателен и содержит ровно один элемент",
        "`then` обязателен и содержит 1 или больше элементов",
        "`code` обязателен и содержит 1 или больше bindings",
        "`phase` необязателен",
        "ровно одним из `symbol`, `file` или `source_glob`",
        "Режим `disabled` не создаёт projection и specification-находок",
        "В режиме `optional` все specification-находки advisory",
        "блокируют только будущие мутации указанной явной specification-страницы",
        "Projection- и resolution-находки остаются advisory",
        "обычные Wiki-страницы не затрагиваются",
    )
    assert all(term in text for term in required)


def test_english_surfaces_publish_exact_binding_and_selector_grammar():
    root = Path(__file__).parents[1]
    surfaces = (
        AUTHORING_RULES,
        (root / "README.md").read_text(encoding="utf-8"),
        (root / "docs/architecture.md").read_text(encoding="utf-8"),
    )
    required = (
        "relation is exactly `implements | verifies`",
        "`phase` is optional and exactly `given | when | then`",
        "selector value is a nonempty UTF-8 string of at most 4,096 bytes with no NUL",
        "`symbol` is a code-graph qualified-name string",
        "only the shared selector scalar constraints",
        "`file` and `source_glob` are trimmed, safe, relative POSIX paths or patterns",
        "at most 256 path segments",
        "backslash, absolute path, Windows drive, empty segment, `.` or `..`",
        "`file` forbids glob metacharacters `*`, `?`, and `[`",
        "`source_glob` allows them",
        "`code` is limited to at most 256 bindings",
        "duplicate phase identity `(phase, role, name)` is invalid",
        "duplicate binding identity `(relation, phase, selector kind, selector)` is invalid",
    )
    for surface in surfaces:
        normalized = " ".join(surface.casefold().split())
        assert all(term.casefold() in normalized for term in required)


def test_russian_docs_publish_exact_binding_and_selector_grammar():
    root = Path(__file__).parents[1]
    text = " ".join((root / "docs/README.ru.md").read_text(encoding="utf-8").split())
    required = (
        "relation принимает строго `implements | verifies`",
        "`phase` необязателен и принимает строго `given | when | then`",
        "непустая UTF-8 строка размером не более 4 096 байт без NUL",
        "`symbol` — строка qualified name для code graph",
        "только общие scalar-ограничения selector",
        "`file` и `source_glob` — безопасные относительные POSIX-пути или шаблоны "
        "без внешних пробелов",
        "не более 256 сегментов пути",
        "обратную косую черту, абсолютный путь, префикс диска Windows, пустой "
        "сегмент, `.` или `..`",
        "`file` запрещает glob-метасимволы `*`, `?` и `[`",
        "`source_glob` разрешает их",
        "`code` ограничен 256 bindings",
        "Повторяющаяся идентичность фазы `(phase, role, name)` невалидна",
        "Повторяющаяся идентичность binding `(relation, phase, selector kind, selector)` невалидна",
    )
    assert all(term in text for term in required)


def test_agent_snippets_define_client_neutral_gwt_workflow():
    root = Path(__file__).parents[1]
    for relative in ("templates/AGENTS.md.snippet", "templates/CLAUDE.md.snippet"):
        text = (root / relative).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        assert "wiki_spec_context" in normalized
        assert "wiki_spec_resolve" in normalized
        assert "ready code graph" in normalized
        assert "repository search and executable tests" in normalized
        assert "test command, exit status, and repository revision" in normalized
        assert "task ledger" in normalized
