"""`.iwikiignore` -- a gitignore-syntax filter at the project root.

Keeps secret / noise source paths out of the wiki. Created (seeded from
.gitignore) when a domain is initialized or a project binds; enforced as a
gate on the `source=` argument of wiki_write_page. The server reads only
.iwikiignore at runtime; the .gitignore copy is a one-time seed."""
from __future__ import annotations

import os

from pathspec import PathSpec

from .engine.config import _load_ignore
from .project_files import initialize_text_file

_DEFAULT = """\
# .iwikiignore -- source paths that must NOT be ingested into the wiki.
# gitignore syntax. Created once when missing or empty; edit manually afterward.

# --- secrets ---
.env
.env.*
*.key
*.pem
*.p12
*secret*
*credentials*

# --- common project noise ---
.git/
.venv/
venv/
__pycache__/
*.py[cod]
node_modules/
dist/
build/
coverage/
*.log
.DS_Store
"""


def ensure_iwikiignore(project_dir: str) -> bool:
    """Fill project_dir/.iwikiignore only when it is missing or empty."""
    path = os.path.join(project_dir, ".iwikiignore")
    content = _DEFAULT
    gitignore = os.path.join(project_dir, ".gitignore")
    if os.path.exists(gitignore):
        try:
            with open(gitignore, encoding="utf-8") as fh:
                inherited = fh.read()
        except (OSError, UnicodeDecodeError):
            inherited = ""
        if inherited:
            if not inherited.endswith("\n"):
                inherited += "\n"
            content += "\n# --- inherited from .gitignore ---\n" + inherited
    return initialize_text_file(path, content)


def load_project_ignore(project_dir: str) -> PathSpec | None:
    """Compile project_dir/.iwikiignore. None if absent or only comments/blanks."""
    return _load_ignore(os.path.join(project_dir, ".iwikiignore"))


def is_ignored(spec: PathSpec | None, source: str, project_dir: str) -> bool:
    """True if source matches spec. Inside project_dir -> relpath match;
    outside -> basename match. spec None / empty source -> False."""
    if spec is None or not source:
        return False
    abs_source = os.path.abspath(source)
    rel = os.path.relpath(abs_source, os.path.abspath(project_dir))
    if rel.startswith(".."):
        rel = os.path.basename(abs_source)
    return spec.match_file(rel)
