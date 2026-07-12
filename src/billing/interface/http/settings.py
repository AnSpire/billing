"""Конфигурация HTTP-слоя. Единственный обязательный параметр — DSN базы;
берём его из того же места, что и остальной код (``connection.database_url``,
env ``BILLING_DATABASE_URL``), чтобы API и фоновые вызовы смотрели в одну БД."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from billing.infrastructure.db.connection import database_url


@dataclass(frozen=True)
class Settings:
    database_url: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшируется на процесс. В тестах, меняющих ``BILLING_DATABASE_URL``,
    вызывать ``get_settings.cache_clear()`` перед созданием приложения."""
    return Settings(database_url=database_url())
