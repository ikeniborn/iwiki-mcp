_KEYS = ["alpha", "beta", "gamma", "delta", "config", "search"]


def embed(text: str) -> list[float]:
    t = text.lower()
    return [float(t.count(k)) for k in _KEYS] or [0.0] * len(_KEYS)


VAULT = {
    "alpha.md": ("---\ndescription: \"alpha alpha topic overview\"\n---\n"
                 "# Alpha\n\n## Alpha Core\nalpha alpha body\n"
                 "\n## Notes\nlinks [Beta](beta.md)\n"),
    "beta.md": ("---\ndescription: \"beta beta topic overview\"\n---\n"
                "# Beta\n\n## Beta Core\nbeta beta body\n"),
    "gamma.md": ("---\ndescription: \"gamma gamma unrelated\"\n---\n"
                 "# Gamma\n\n## Gamma Core\ngamma gamma body\n"),
}

QUERIES = [
    {"query": "alpha", "vec": embed("alpha"),
     "articles": ["alpha.md"], "sections": ["Alpha Core"]},
    {"query": "beta", "vec": embed("beta"),
     "articles": ["beta.md"], "sections": ["Beta Core"]},
]
