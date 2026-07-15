_BUCKETS = (
    ("auth", "credential", "login", "renew", "refresh_token"),
    ("deploy", "release", "rollback", "restore"),
    ("config", "iwiki_search_mode", "runtime"),
    ("semantic", "meaning", "similarity", "wording"),
    ("break_glass_token", "emergency", "recovery"),
)


def embed(text: str) -> list[float]:
    lowered = text.lower()
    return [float(sum(lowered.count(term) for term in bucket)) for bucket in _BUCKETS]


VAULT = {
    "guide/auth.md": (
        "---\ndescription: credential lifecycle and session renewal\n---\n"
        "# Authentication\n\n"
        "## Rotation\nrefresh_token rotates credentials safely\n\n"
        "## Links\nSee [Deployment](guide/deploy.md).\n"
    ),
    "guide/deploy.md": (
        "---\ndescription: release rollout procedure\n---\n"
        "# Deployment\n\n"
        "## Rollback\nrestore the previous release atomically\n"
    ),
    "reference/config.md": (
        "---\ndescription: runtime configuration keys\n---\n"
        "# Configuration\n\n"
        "## Search Mode\nIWIKI_SEARCH_MODE selects retrieval behavior\n"
    ),
    "concept/semantic.md": (
        "---\ndescription: meaning based document discovery\n---\n"
        "# Semantic Discovery\n\n"
        "## Similarity\nfind passages with different wording\n"
    ),
    "concept/distractor.md": (
        "---\ndescription: release search credential configuration\n---\n"
        "# Distractor\n\n"
        "## Noise\nrelease search credential configuration\n"
    ),
    "runbook/orphan.md": (
        "---\ndescription: unrelated maintenance notes\n---\n"
        "# Orphan\n\n"
        "## Emergency Token\nbreak_glass_token recovery procedure\n"
    ),
}

QUERIES = [
    {"query": "renew login access", "relevant": [("guide/auth.md", "Rotation")]},
    {"query": "IWIKI_SEARCH_MODE", "relevant": [("reference/config.md", "Search Mode")]},
    {"query": "restore a bad release", "relevant": [("guide/deploy.md", "Rollback")]},
    {"query": "different words same meaning", "relevant": [("concept/semantic.md", "Similarity")]},
    {"query": "break_glass_token", "relevant": [("runbook/orphan.md", "Emergency Token")]},
    {"query": "refresh_token credentials", "relevant": [("guide/auth.md", "Rotation")]},
]
