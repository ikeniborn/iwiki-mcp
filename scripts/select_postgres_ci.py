"""Select PostgreSQL integration from Git changes; unknown inputs require it."""
import ast
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 project environments provide tomli.
    import tomli as tomllib


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.PIPE)


def constant_only(source, name):
    tree = ast.parse(source)
    assignments = [
        node for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(isinstance(target, ast.Name) and target.id == name for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        ))
    ]
    if len(assignments) != 1:
        raise ValueError("expected one literal assignment")
    node = assignments[0]
    if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
        raise ValueError("expected a string literal")
    node.value = ast.Constant(value="<text>")
    return ast.dump(tree)


def metadata_without_version(source, path):
    document = tomllib.loads(source)
    if path == "pyproject.toml":
        package = document["project"]
    else:
        packages = [item for item in document["package"]
                    if item["name"] == "iwiki-mcp" and item.get("source") == {"editable": "."}]
        if len(packages) != 1:
            raise ValueError("expected one local package")
        package = packages[0]
    if not isinstance(package.pop("version"), str):
        raise ValueError("expected a version string")
    return document


def independent_change(path, base, head):
    file = PurePosixPath(path)
    # Data and support modules are not isolated tests, even outside tests/postgres.
    if path.startswith("tests/"):
        return (
            not path.startswith("tests/postgres/") and "fixtures" not in file.parts
            and file.name.startswith("test_") and file.suffix == ".py"
        )
    if file.suffix == ".md" and (
        path.startswith("docs/") or len(file.parts) == 1
    ):
        return True
    if path in {"templates/AGENTS.md.snippet", "templates/CLAUDE.md.snippet"}:
        return True
    constants = {
        "src/iwiki_mcp/resources.py": "AUTHORING_RULES",
        "src/iwiki_mcp/__init__.py": "__version__",
    }
    if path not in constants and path not in {"pyproject.toml", "uv.lock"}:
        return False
    try:
        before = git("show", f"{base}:{path}").decode("utf-8")
        after = git("show", f"{head}:{path}").decode("utf-8")
        if path in constants:
            return constant_only(before, constants[path]) == constant_only(after, constants[path])
        return metadata_without_version(before, path) == metadata_without_version(after, path)
    except (subprocess.CalledProcessError, UnicodeError, ValueError,
            SyntaxError, KeyError, TypeError):
        return False


def select(event, base, head):
    if event not in {"pull_request", "push"}:
        return True, "manual or unknown event"
    if not base or not head or set(base) == {"0"}:
        return True, "comparison history unavailable"
    try:
        if event == "pull_request":
            base = git("merge-base", base, head).decode().strip()
        # Treat renames as delete/add so moving runtime into docs cannot hide it.
        paths = git("diff", "--no-renames", "--name-only", "-z", base, head, "--")
        changed = [name.decode("utf-8") for name in paths.split(b"\0") if name]
    except (subprocess.CalledProcessError, UnicodeError):
        return True, "comparison history unavailable"
    for path in changed:
        if not independent_change(path, base, head):
            return True, f"database, shared, or unclassified input: {path}"
    return False, "only independent docs, tests, instruction text, or project version changed"


def main():
    required, reason = select(
        os.environ.get("EVENT_NAME", ""), os.environ.get("BASE_SHA", ""),
        os.environ.get("HEAD_SHA", ""),
    )
    print(json.dumps({"run_postgres": required, "reason": reason}))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"run_postgres={str(required).lower()}\n")


if __name__ == "__main__":
    main()
