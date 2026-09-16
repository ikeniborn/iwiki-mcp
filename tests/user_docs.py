"""Read the user-facing documentation as one text per language.

README.md is a landing page; the reference documentation lives in one module per topic
under docs/, each with a Russian `<stem>.ru.md` sibling. Documentation-contract tests
assert against the concatenation instead of a single file, so a section keeps its
guarantees wherever it is filed. A new module must be added to DOC_MODULES.
"""
from pathlib import Path

ROOT = Path(__file__).parents[1]

DOC_MODULES = (
    "storage-modes",
    "postgres-setup",
    "wiki-model",
    "code-graph",
    "code-graph-publishing",
    "specifications",
    "tools-reference",
    "env-reference",
    "okf-compatibility",
    "benchmarks",
)


def user_docs(suffix: str = "") -> str:
    """Concatenate the landing page and every documentation module of one language."""
    landing = ROOT / ("docs/README.ru.md" if suffix else "README.md")
    parts = [landing.read_text(encoding="utf-8")]
    parts += [
        (ROOT / f"docs/{stem}{suffix}.md").read_text(encoding="utf-8")
        for stem in DOC_MODULES
    ]
    return "\n".join(parts)
