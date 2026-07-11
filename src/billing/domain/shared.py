"""Value Objects, переиспользуемые несколькими агрегатами
(billing_aggregates.md, «Общие Value Objects»).

Правило переезда сюда: VO появляется в этом модуле, когда у него возникает
**второй реальный потребитель**, а не заранее «на всякий случай» — см.
PLAN.md, разбор в фазе 2 (``Quantity`` там осознанно оставлен рядом с
``ConsumptionStream``, потому что второго потребителя у него пока нет).
``TemporalValidity`` переехал сюда в фазе 3: раньше жил в
``domain/reference_parameter.py``, вторым потребителем стал ``TariffVersion``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TemporalValidity:
    """Полуоткрытый интервал valid-time. ``valid_to=None`` — «до отмены»."""

    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be strictly after valid_from")
