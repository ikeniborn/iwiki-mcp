"""The CI gate must reject a green pytest run with missing database coverage."""
from pathlib import Path
import subprocess
import sys

import pytest


CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_postgres_junit.py"


@pytest.mark.parametrize(
    "report,accepted",
    [
        ('<testsuites><testsuite><testcase classname="tests.postgres.test_store" '
         'name="test_roundtrip"/></testsuite></testsuites>', True),
        ('<testsuites><testsuite/></testsuites>', False),
        ('<testsuite><testcase classname="tests.test_unit"/></testsuite>', False),
        ('<testsuite><testcase classname="tests.postgres.test_store">'
         '<skipped message="IWIKI_TEST_POSTGRES_DSN is not set"/>'
         '</testcase></testsuite>', False),
        ('<testsuite><testcase classname="tests.postgres.test_store">'
         '<failure/></testcase></testsuite>', False),
        ('<testsuite><testcase classname="tests.postgres.test_store">'
         '<error/></testcase></testsuite>', False),
        ('<testsuites>', False),
        (None, False),
    ],
)
def test_postgres_report_requires_executed_passing_tests(tmp_path, report, accepted):
    path = tmp_path / "postgres.xml"
    if report is not None:
        path.write_text(report, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(path)], capture_output=True, text=True,
    )
    assert result.returncode == (0 if accepted else 1), result.stderr
    assert "PostgreSQL CI" in result.stdout + result.stderr
