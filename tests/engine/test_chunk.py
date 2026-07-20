from iwiki_mcp.engine.chunk import chunk_markdown


def test_splits_on_h2_headings():
    md = "intro ignored\n\n## First\nbody one\n\n## Second\nbody two\n"
    chunks = chunk_markdown("f.md", md, size=512, overlap=64)
    assert [c.heading for c in chunks] == ["First", "Second"]
    assert chunks[0].id == "f.md#First"


def test_content_before_first_heading_ignored():
    assert chunk_markdown("f.md", "preamble only, no headings", size=512, overlap=64) == []


def test_long_section_splits_with_overlap_and_indexes():
    body = " ".join(str(i) for i in range(20))
    chunks = chunk_markdown("f.md", f"## H\n{body}\n", size=8, overlap=2)
    assert len(chunks) > 1
    assert all(c.heading == "H" for c in chunks)
    assert [c.chunk for c in chunks] == list(range(len(chunks)))


def test_repeated_headings_continue_chunk_numbers_without_deduplication():
    md = (
        "## Setup\none two three four\n"
        "## Other\nmiddle\n"
        "## Setup\nfive six seven eight\n"
    )
    chunks = chunk_markdown("f.md", md, size=2, overlap=0)
    setup = [c for c in chunks if c.heading == "Setup"]
    assert [(c.chunk, c.ordinal) for c in setup] == [
        (0, 0), (1, 0), (2, 2), (3, 2),
    ]
    assert [c.text for c in setup] == [
        "## Setup\none two",
        "## Setup\nthree four",
        "## Setup\nfive six",
        "## Setup\nseven eight",
    ]


def test_identical_repeated_sections_remain_distinct_chunks():
    md = "## Setup\nsame body\n## Setup\nsame body\n"
    chunks = chunk_markdown("f.md", md, size=512, overlap=64)
    assert [(c.heading, c.chunk, c.ordinal) for c in chunks] == [
        ("Setup", 0, 0),
        ("Setup", 1, 1),
    ]
    assert chunks[0].hash == chunks[1].hash


PAGE = (
    "# Proxy Management\n\n"
    "## Overview\n"
    "The gateway routes API traffic via an HTTPS proxy with OAuth refresh.\n\n"
    "## TLS Handling\n"
    "The proxy terminates TLS using a local CA.\n\n"
    "## OAuth Refresh\n"
    "Tokens refresh before expiry.\n"
)


def test_overview_section_is_not_indexed():
    chunks = chunk_markdown("proxy.md", PAGE, size=512, overlap=64)
    assert "Overview" not in {c.heading for c in chunks}
    assert {c.heading for c in chunks} == {"TLS Handling", "OAuth Refresh"}


def test_section_chunk_is_clean_of_title_and_description():
    page = (
        "---\ntype: concept\ndescription: The gateway routes API traffic via a proxy.\n---\n"
        "# Proxy Management\n\n## TLS Handling\nThe proxy terminates TLS using a local CA.\n"
    )
    chunks = chunk_markdown("proxy.md", page, size=512, overlap=64)
    tls = next(c for c in chunks if c.heading == "TLS Handling")
    assert tls.text == "## TLS Handling\nThe proxy terminates TLS using a local CA."
    assert "Proxy Management" not in tls.text
    assert "gateway routes API traffic" not in tls.text


def test_section_subchunks_all_start_with_heading_only():
    body = " ".join(str(i) for i in range(40))
    md = f"---\ndescription: summ of all.\n---\n# T\n\n## Big\n{body}\n"
    chunks = chunk_markdown("f.md", md, size=8, overlap=2)
    big = [c for c in chunks if c.heading == "Big"]
    assert len(big) > 1
    assert all(c.text.startswith("## Big\n") for c in big)
    assert all("summ of all" not in c.text for c in big)   # description stays out
    assert all("# T" not in c.text.split("\n")[0] for c in big)


def test_no_description_yields_no_summary_chunk():
    md = "# T\n\n## A\nbody alpha.\n"
    chunks = chunk_markdown("f.md", md, size=512, overlap=64)
    assert not any(c.kind == "summary" for c in chunks)
    assert chunks[0].text == "## A\nbody alpha."


def test_section_hash_independent_of_description():
    # section chunk text no longer includes the description, so its hash is stable
    # when only the description changes; the summary chunk's hash tracks it instead.
    a = chunk_markdown("f.md", "---\ndescription: summ one.\n---\n# T\n\n## A\nbody.\n",
                       size=512, overlap=64)
    b = chunk_markdown("f.md", "---\ndescription: summ two.\n---\n# T\n\n## A\nbody.\n",
                       size=512, overlap=64)
    a_sec = next(c for c in a if c.kind == "section")
    b_sec = next(c for c in b if c.kind == "section")
    assert a_sec.hash == b_sec.hash
    a_summ = next(c for c in a if c.kind == "summary")
    b_summ = next(c for c in b if c.kind == "summary")
    assert a_summ.hash != b_summ.hash
