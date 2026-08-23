"""Provider-independent values shared by bot components."""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PageTarget:
    domain: str
    slug: str
    heading: str | None = None
    revision: int | None = None
    section_hash: str | None = None


@dataclass(frozen=True)
class PendingWrite:
    token: str
    telegram_id: int
    action: str
    payload: Mapping[str, object]
    expires_at: float


@dataclass(frozen=True)
class WritePreview:
    token: str
    text: str
    buttons: tuple[str, ...] = ("confirm", "reject")


@dataclass(frozen=True)
class BotReply:
    text: str
    buttons: tuple[str, ...] = ()
