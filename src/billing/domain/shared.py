"""Value Objects, переиспользуемые несколькими агрегатами
(billing_aggregates.md, «Общие Value Objects»).

Правило переезда сюда: VO появляется в этом модуле, когда у него возникает
**второй реальный потребитель**, а не заранее «на всякий случай» — см.
PLAN.md, разбор в фазе 2. ``TemporalValidity`` переехал сюда в фазе 3 (второй
потребитель — ``TariffVersion``). В фазе 4 сюда же переехал ``Quantity``
(второй потребитель — ``BillingAssessment``, folding ``ConsumptionStream`` для
расчёта) и появился ``BillingPeriod`` — сразу с двумя потребителями
(``BillingAssessment`` как часть идентичности и ``ConsumptionStream`` для
фильтрации свёртки по периоду), поэтому живёт здесь с рождения, без
промежуточного этапа "в одном агрегате".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal


@dataclass(frozen=True)
class TemporalValidity:
    """Полуоткрытый интервал valid-time. ``valid_to=None`` — «до отмены»."""

    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be strictly after valid_from")


@dataclass(frozen=True)
class Quantity:
    """Значение + метрика — общая абстракция над разнородным потреблением
    (billing_aggregates.md, «Общие VO»)."""

    value: Decimal
    metric: str

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("metric must not be empty")


@dataclass(frozen=True)
class BillingPeriod:
    """Расчётный период — месяц. Идентичность ``BillingAssessment`` включает
    его напрямую; ``ConsumptionStream`` использует его же, чтобы свернуть
    события за конкретный период (billing_aggregates.md §3)."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")

    @property
    def start(self) -> datetime:
        return datetime(self.year, self.month, 1, tzinfo=timezone.utc)

    @property
    def end(self) -> datetime:
        """Исключающая верхняя граница — первый момент следующего месяца."""
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=timezone.utc)
        return datetime(self.year, self.month + 1, 1, tzinfo=timezone.utc)

    @property
    def valid_on(self) -> datetime:
        """«Конец периода» в смысле аргумента ``resolve()``
        (billing_aggregates.md, «Резолвинг референсных параметров») —
        последний представимый момент ВНУТРИ периода, а не ``end``. Норма,
        начинающая действовать ровно с 1-го числа следующего месяца, не
        должна попасть в резолвинг этого периода — иначе смена ставки 1 июля
        «отравляет» пересчёт июня (PLAN.md, DoD фазы 4)."""
        return self.end - timedelta(microseconds=1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"
