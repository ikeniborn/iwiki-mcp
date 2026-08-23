"""Telegram sender authorization."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessPolicy:
    allowed_telegram_ids: frozenset[int]

    def allows(self, telegram_id: int) -> bool:
        return telegram_id in self.allowed_telegram_ids
