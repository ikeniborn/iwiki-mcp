from importlib.metadata import version

import iwiki_mcp


def test_package_version_matches_distribution_metadata():
    assert iwiki_mcp.__version__ == version("iwiki-mcp")
