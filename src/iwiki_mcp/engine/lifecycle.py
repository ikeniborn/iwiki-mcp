"""Interpret the lifecycle of a concept within one OKF bundle."""

import posixpath
import re
from urllib.parse import unquote


class LifecycleError(ValueError):
    """An invalid combination of public lifecycle arguments."""


def is_current(metadata: dict) -> bool:
    return metadata.get("status", "stable") == "stable"


def _successor(source: str, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if re.search(r"^(?:[a-z][a-z0-9+.-]*:|//)|[?#\\]|%(?:2f|5c)|%(?![a-f0-9]{2})",
                 value, re.IGNORECASE):
        return None
    try:
        path = unquote(value, errors="strict")
    except UnicodeError:
        return None
    if any(ord(char) < 32 for char in path) or any(char.isspace() for char in value):
        return None
    joined = (path[1:] if path.startswith("/") else
              posixpath.join(posixpath.dirname(source), path))
    target = posixpath.normpath(joined)
    if target.startswith("../") or not target.endswith(".md"):
        return None
    return target


class BundleLifecycle:
    """Resolve only against the authorized page map supplied by the caller."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self._resolved: dict[str, str | None] = {}
        self._issues: dict[str, str] = {}

    def resolve(self, file: str) -> str | None:
        path = file
        visited: set[str] = set()
        result = issue = None
        while True:
            if path in self._resolved:
                result, issue = self._resolved[path], self._issues.get(path)
                break
            if path in visited:
                issue = "cycle"
                break
            visited.add(path)
            meta = self.pages.get(path)
            if meta is None:
                issue = "missing_replacement"
                break
            if is_current(meta):
                result = path
                break
            if meta.get("status") != "deprecated" or not meta.get("superseded_by"):
                break
            target = _successor(path, meta["superseded_by"])
            if target is None:
                issue = "invalid_replacement"
                break
            path = target
        for source in visited:
            self._resolved[source] = result
            if issue:
                self._issues[source] = issue
        return result

    def findings(self) -> list[dict]:
        for file in self.pages:
            self.resolve(file)
        return [{"page": file, "type": self._issues[file]}
                for file in sorted(self.pages) if file in self._issues]


def update_metadata(meta: dict, status: str | None, replacement: str | None) -> None:
    """Apply an explicit lifecycle transition, preserving omitted fields."""
    if status is not None:
        meta["status"] = status.strip().lower()
        if meta["status"] != "deprecated":
            meta.pop("superseded_by", None)
    if replacement is None:
        return
    if replacement == "":
        meta.pop("superseded_by", None)
        return
    if meta.get("status") != "deprecated" or not isinstance(replacement, str):
        raise LifecycleError("superseded_by requires deprecated status and a path string")
    meta["superseded_by"] = replacement
