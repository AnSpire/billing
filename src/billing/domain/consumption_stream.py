"""ConsumptionStream — billing_aggregates.md §6.

Тонкий агрегат: "событие потребления неизменяемо, защищать почти нечего —
единственный реальный инвариант — идемпотентность приёма". Идентичность —
``(account_id, metric)``. Как и в ``ReferenceParameter``
(domain/reference_parameter.py), сам агрегат не хранит историю событий и не
проверяет уникальность в памяти — это делает UNIQUE-констрейнт в БД (см.
миграцию ``0002_consumption_stream.sql``). Метод ``record_usage`` — чистая
функция: команда всегда "хочет" записать факт; решение, дубликат это или
нет, принимает репозиторий на записи (см.
``infrastructure/db/consumption_stream_repository.py``).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from billing.domain.events import DomainEvent
from billing.domain.shared import BillingPeriod, Quantity

__all__ = [
    "ConsumptionStreamError",
    "MetricMismatchError",
    "Quantity",
    "ExternalEventId",
    "UsageEvent",
    "UsageRecorded",
    "RecordUsageResult",
    "ConsumptionStream",
    "ConsumptionStreamRepository",
]


class ConsumptionStreamError(Exception):
    """Базовая ошибка домена ConsumptionStream."""


class MetricMismatchError(ConsumptionStreamError):
    """Quantity.metric не совпадает с metric потока, в который её пишут."""


@dataclass(frozen=True)
class ExternalEventId:
    """Ключ идемпотентности — id события в системе-источнике (показание
    счётчика, тикет поддержки и т.п.)."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("external_event_id must not be empty")


@dataclass(frozen=True)
class UsageEvent:
    """Факт потребления (billing_aggregates.md §6). Неизменяем после записи —
    append-only, пересмотр (UC-8) добавляет новый факт, а не правит этот."""

    event_id: uuid.UUID
    account_id: str
    metric: str
    quantity: Quantity
    external_event_id: ExternalEventId
    recorded_at: datetime
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class UsageRecorded(DomainEvent):
    account_id: str
    metric: str
    event_id: uuid.UUID
    external_event_id: str


@dataclass(frozen=True)
class RecordUsageResult:
    """Явный сигнал, что произошло на записи (billing_aggregates.md §6 /
    UC-3: «молча, либо с явным сигналом, что дубликат проигнорирован» — здесь
    выбран явный сигнал, чтобы вызывающий код и тесты могли отличить два
    случая, не заглядывая в БД)."""

    event: UsageEvent
    is_duplicate: bool


@dataclass(frozen=True)
class ConsumptionStream:
    """Идентичность — ``(account_id, metric)``."""

    account_id: str
    metric: str

    def record_usage(
        self,
        quantity: Quantity,
        external_event_id: ExternalEventId,
        meta: Mapping[str, Any] | None = None,
        *,
        now: datetime,
    ) -> tuple[UsageEvent, UsageRecorded]:
        if quantity.metric != self.metric:
            raise MetricMismatchError(
                f"quantity metric {quantity.metric!r} does not match stream metric {self.metric!r}"
            )
        event = UsageEvent(
            event_id=uuid.uuid4(),
            account_id=self.account_id,
            metric=self.metric,
            quantity=quantity,
            external_event_id=external_event_id,
            recorded_at=now,
            meta=dict(meta or {}),
        )
        domain_event = UsageRecorded(
            account_id=self.account_id,
            metric=self.metric,
            event_id=event.event_id,
            external_event_id=external_event_id.value,
        )
        return event, domain_event


class ConsumptionStreamRepository(ABC):
    """Порт (см. PLAN.md, «Repository — порт в домене, реализация в
    infrastructure»). Единственная реализация — ``PostgresConsumptionStreamRepository``."""

    @abstractmethod
    def record_usage(
        self,
        account_id: str,
        metric: str,
        quantity: Quantity,
        external_event_id: ExternalEventId,
        meta: Mapping[str, Any] | None = None,
        *,
        now: datetime,
    ) -> RecordUsageResult: ...

    @abstractmethod
    def events_for(
        self, account_id: str, metric: str, *, period: BillingPeriod | None = None
    ) -> list[UsageEvent]:
        """Без ``period`` — вся история (как в фазе 2). С ``period`` —
        свёртка за расчётный период (нужно с фазы 4, ``BillingAssessment``
        суммирует ``UsageEvent`` за конкретный месяц, не за всё время)."""
        ...
