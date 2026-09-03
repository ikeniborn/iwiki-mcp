from iwiki_mcp.codegraph.linking import links_stale


def _never():
    raise AssertionError("the revision must not be computed")


def test_equal_generations_answer_current_without_hashing():
    assert links_stale(
        "sha256:a", _never, stored_generation=7, current_generation=7
    ) is False


def test_differing_generations_defer_to_the_revision():
    assert links_stale(
        "sha256:a", lambda: "sha256:a", stored_generation=7, current_generation=9
    ) is False
    assert links_stale(
        "sha256:a", lambda: "sha256:b", stored_generation=7, current_generation=9
    ) is True


def test_a_storage_without_counters_compares_revisions():
    assert links_stale("sha256:a", lambda: "sha256:a") is False
    assert links_stale("sha256:a", lambda: "sha256:b") is True


def test_a_missing_stored_revision_is_stale():
    assert links_stale(None, lambda: "sha256:a") is True


def test_one_counter_alone_does_not_short_circuit():
    assert links_stale(
        "sha256:a", lambda: "sha256:b", stored_generation=7
    ) is True
    assert links_stale(
        "sha256:a", lambda: "sha256:b", current_generation=7
    ) is True
