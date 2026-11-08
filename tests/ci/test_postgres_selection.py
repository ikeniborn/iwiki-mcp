"""Exercise PostgreSQL CI selection through real Git changes."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SELECTOR = Path(__file__).resolve().parents[2] / "scripts/select_postgres_ci.py"


class PostgresSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "CI test")
        self.base = self.commit({
            "README.md": "before\n",
            "src/iwiki_mcp/server.py": "VALUE = 1\n",
            "src/iwiki_mcp/resources.py": 'AUTHORING_RULES: str = "before"\n',
            "src/iwiki_mcp/__init__.py": '__version__ = "1.0.0"\n',
            "pyproject.toml": '[project]\nname = "iwiki-mcp"\nversion = "1.0.0"\n'
                              'dependencies = ["psycopg>=3.2"]\n',
            "uv.lock": 'version = 1\n[[package]]\nname = "iwiki-mcp"\n'
                       'version = "1.0.0"\nsource = { editable = "." }\n'
                       '[[package]]\nname = "psycopg"\nversion = "3.2.0"\n',
        })

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, text=True, stderr=subprocess.PIPE,
        ).strip()

    def commit(self, changes):
        for name, content in changes.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.unlink()
            else:
                path.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture change", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def select(self, expected, *, event="pull_request", base=None, head="HEAD"):
        output = Path(self.directory.name) / "selection-output"
        output.unlink(missing_ok=True)
        env = {**os.environ, "EVENT_NAME": event, "BASE_SHA": base or self.base,
               "HEAD_SHA": head, "GITHUB_OUTPUT": str(output)}
        result = subprocess.run(
            [sys.executable, str(SELECTOR)], cwd=self.repo, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["run_postgres"], expected, decision)
        self.assertTrue(decision["reason"])
        self.assertEqual(output.read_text(), f"run_postgres={str(expected).lower()}\n")

    def test_documentation(self):
        self.commit({"README.md": "after\n", "docs/postgres-setup.md": "text\n"})
        self.select(False)

    def test_independent_test_changes(self):
        self.commit({"tests/codegraph/test_indexer_runtime.py": "def test_x(): pass\n",
                     "tests/test_resources.py": "def test_x(): pass\n"})
        self.select(False)

    def test_instruction_and_version_only_changes(self):
        self.commit({
            "src/iwiki_mcp/resources.py": 'AUTHORING_RULES: str = "preserve case"\n',
            "src/iwiki_mcp/__init__.py": '__version__ = "1.0.1"\n',
            "templates/AGENTS.md.snippet": "preserve case\n",
            "templates/CLAUDE.md.snippet": "preserve case\n",
            "tests/test_package.py": "VERSION = '1.0.1'\n",
            "pyproject.toml": (self.repo / "pyproject.toml").read_text().replace(
                'version = "1.0.0"', 'version = "1.0.1"'),
            "uv.lock": (self.repo / "uv.lock").read_text().replace(
                'version = "1.0.0"', 'version = "1.0.1"'),
        })
        self.select(False)

    def test_runtime_and_shared_inputs_require_database(self):
        for path in (
            "src/iwiki_mcp/server.py", "src/iwiki_mcp/http.py",
            "src/iwiki_mcp/engine/config.py", "src/iwiki_mcp/codegraph/runtime.py",
            "src/iwiki_mcp/postgres/migrations.py", "tests/postgres/test_store.py",
            "tests/conftest.py", "tests/__init__.py",
            "tests/codegraph/publication_contract_support.py",
            "tests/codegraph/synthetic_wiki.py", "tests/fixtures/input.json",
            "tests/codegraph/fixtures/test_module.py",
            "compat/postgres-v4-runtime-guard.json", "eval/code_graph/runner.py",
            ".github/workflows/postgres.yml", "scripts/check_postgres_junit.py",
            "scripts/select_postgres_ci.py", "new-subsystem/config.yaml",
        ):
            with self.subTest(path=path):
                base = self.git("rev-parse", "HEAD")
                self.commit({path: "changed\n"})
                self.select(True, base=base)

    def test_postgres_imported_test_helpers_are_not_excluded(self):
        root = SELECTOR.parent.parent
        helpers = set()
        for source in (root / "tests/postgres").glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if (node.module.startswith("tests.")
                            and not node.module.startswith("tests.postgres")):
                        helpers.add(node.module.replace(".", "/") + ".py")
                elif isinstance(node, ast.Import):
                    helpers.update(alias.name.replace(".", "/") + ".py"
                                   for alias in node.names if alias.name.startswith("tests."))
        for path in helpers:
            with self.subTest(path=path):
                base = self.git("rev-parse", "HEAD")
                self.commit({path: "changed helper\n"})
                self.select(True, base=base)

    def test_dependency_change_requires_database(self):
        text = (self.repo / "pyproject.toml").read_text()
        self.commit({"pyproject.toml": text.replace("psycopg>=3.2", "psycopg>=3.3")})
        self.select(True)

    def test_locked_dependency_change_requires_database(self):
        text = (self.repo / "uv.lock").read_text()
        self.commit({"uv.lock": text.replace('version = "3.2.0"', 'version = "3.3.0"')})
        self.select(True)

    def test_python_version_and_test_config_require_database(self):
        self.commit({"pyproject.toml": (self.repo / "pyproject.toml").read_text()
                     + 'requires-python = ">=3.12"\n[tool.pytest.ini_options]\naddopts = "-x"\n'})
        self.select(True)

    def test_resources_executable_change_requires_database(self):
        self.commit({"src/iwiki_mcp/resources.py": 'AUTHORING_RULES: str = "after"\n'
                     'raise RuntimeError("broken import")\n'})
        self.select(True)

    def test_version_module_executable_change_requires_database(self):
        self.commit({"src/iwiki_mcp/__init__.py": '__version__ = "1.0.1"\nimport missing\n'})
        self.select(True)

    def test_invalid_metadata_requires_database(self):
        for path in ("pyproject.toml", "uv.lock", "src/iwiki_mcp/resources.py"):
            with self.subTest(path=path):
                base = self.git("rev-parse", "HEAD")
                self.commit({path: "not valid [\n"})
                self.select(True, base=base)

    def test_deleted_runtime_and_renamed_runtime_require_database(self):
        self.commit({"src/iwiki_mcp/server.py": None, "docs/server.md": "VALUE = 1\n"})
        self.select(True)

    def test_mixed_docs_and_runtime_requires_database(self):
        self.commit({"README.md": "after\n", "src/iwiki_mcp/server.py": "VALUE = 2\n"})
        self.select(True)

    def test_manual_and_unknown_event_always_require_database(self):
        self.select(True, event="workflow_dispatch")
        self.select(True, event="unknown")

    def test_missing_base_requires_database(self):
        self.select(True, base="0" * 40, event="push")
        self.select(True, base="unavailable-commit")

    def test_pull_request_uses_merge_base_not_unrelated_base_changes(self):
        base_head = self.commit({"src/iwiki_mcp/server.py": "VALUE = 2\n"})
        self.git("checkout", "-q", self.base)
        self.commit({"README.md": "branch docs\n"})
        self.select(False, base=base_head)

    def test_push_checks_whole_range(self):
        self.commit({"src/iwiki_mcp/server.py": "VALUE = 2\n"})
        self.commit({"README.md": "last commit only docs\n"})
        self.select(True, event="push")

    def test_empty_diff_does_not_require_database(self):
        self.select(False)


class CoverageGateTests(unittest.TestCase):
    def test_workflow_gate_requires_successful_selection_and_selected_coverage(self):
        workflow = (SELECTOR.parent.parent / ".github/workflows/postgres.yml").read_text()
        step = workflow.split("      - name: Require selected coverage\n", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        for selection, required, database, accepted in (
            ("success", "true", "success", True),
            ("success", "false", "skipped", True),
            ("failure", "false", "skipped", False),
            ("cancelled", "false", "skipped", False),
            ("success", "true", "skipped", False),
            ("success", "true", "failure", False),
            ("success", "true", "cancelled", False),
            ("success", "", "skipped", False),
        ):
            with self.subTest(selection=selection, required=required, database=database):
                env = {**os.environ, "SELECTION_RESULT": selection,
                       "REQUIRED": required, "DATABASE_RESULT": database}
                result = subprocess.run(["bash", "-e", "-c", script], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stdout)


if __name__ == "__main__":
    unittest.main()
