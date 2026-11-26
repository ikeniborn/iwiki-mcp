"""Review deadlines and immutable page evidence for both lint backends."""
from datetime import datetime, timezone
from types import SimpleNamespace

from iwiki_mcp import server


def deadline_pages():
    fields = {
        "absent": "",
        "null": "stale_after: null\n",
        "tilde": "stale_after: ~\n",
        "empty": 'stale_after: ""\n',
        "future": "stale_after: 2030-09-01T00:00:00.000001Z\n",
        "future-fraction": "stale_after: 2030-09-01T00:00:00.0000001Z\n",
        "boundary": "stale_after: 2030-09-01T02:00:00+02:00\n",
        "date": 'stale_after: "2030-09-01"\n',
        "invalid": 'stale_after: "null"\n',
        "invalid-list": "stale_after: [yesterday]\n",
        "past": "stale_after: 2030-08-31\n",
    }
    return {slug: f"---\ntype: guide\n{field}---\n# Page\n\n## Detail\nText.\n"
            for slug, field in fields.items()}


def check_deadline_reports(monkeypatch):
    calls = []

    class Clock:
        @classmethod
        def now(cls, tz):
            calls.append(tz)
            return datetime(2030, 9, 1, tzinfo=tz)

    monkeypatch.setattr(server, "_dt", SimpleNamespace(datetime=Clock, timezone=timezone))
    for _ in range(2):
        reports = server.wiki_lint()["reports"]
        assert set(reports) == {"docs", "other"}
        assert reports["docs"].get("freshness") == [
            {"page": "boundary.md", "type": "review_due"},
            {"page": "date.md", "type": "review_due"},
            {"page": "invalid-list.md", "type": "invalid_stale_after"},
            {"page": "invalid.md", "type": "invalid_stale_after"},
            {"page": "past.md", "type": "review_due"},
        ]
        assert reports["other"]["freshness"] == []
        assert reports["docs"]["stale"] == []
    assert calls == [timezone.utc, timezone.utc]
    assert server.wiki_lint("hidden")["reports"] == {}
