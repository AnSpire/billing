"""Базовый тип доменного события.

В этом проекте "сага" — не распределённая инфраструктура, а обычный
внутрипроцессный обработчик вида "когда случилось событие X — вызови команду
Y" (CLAUDE.md §2). ``DomainEvent`` — общий тип для того, что агрегаты кладут
в свой список необработанных событий после выполнения команды.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
