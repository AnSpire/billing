"""Чистые тесты агрегата ConsumptionStream — без БД.

Идемпотентность здесь не проверяется (это ответственность UNIQUE-констрейнта
в БД, см. ``test_consumption_stream_repository.py``) — только форма команды.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.consumption_stream import (
    ConsumptionStream,
    ConsumptionStreamRepository,
    ExternalEventId,
    MetricMismatchError,
    Quantity,
    UsageRecorded,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def test_record_usage_produces_event_and_domain_event() -> None:
    stream = ConsumptionStream(account_id="acc-4471", metric="water_m3")

    event, domain_event = stream.record_usage(
        Quantity(value=Decimal("12.4"), metric="water_m3"),
        ExternalEventId("meter-778812-20260706"),
        now=_dt(2026, 7, 6),
    )

    assert event.account_id == "acc-4471"
    assert event.quantity.value == Decimal("12.4")
    assert isinstance(domain_event, UsageRecorded)
    assert domain_event.event_id == event.event_id
    assert domain_event.external_event_id == "meter-778812-20260706"


def test_record_usage_rejects_quantity_with_a_different_metric() -> None:
    stream = ConsumptionStream(account_id="acc-4471", metric="water_m3")

    with pytest.raises(MetricMismatchError):
        stream.record_usage(
            Quantity(value=Decimal("12.4"), metric="electricity_kwh"),
            ExternalEventId("meter-778812-20260706"),
            now=_dt(2026, 7, 6),
        )


def test_quantity_requires_a_metric() -> None:
    with pytest.raises(ValueError):
        Quantity(value=Decimal("1"), metric="")


def test_external_event_id_must_not_be_blank() -> None:
    with pytest.raises(ValueError):
        ExternalEventId("")


def test_repository_port_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        ConsumptionStreamRepository()
