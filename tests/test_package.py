from importlib.metadata import version

import iwiki_mcp


def test_package_version_matches_distribution_metadata():
    assert iwiki_mcp.__version__ == version("iwiki-mcp")


def test_v1_search_latency_intent_package_version():
    assert iwiki_mcp.__version__ == "0.7.88"
