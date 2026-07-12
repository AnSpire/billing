"""ConsumptionStream поверх реальной БД — DoD фазы 2 (PLAN.md):

- повторное RecordUsage с тем же external_event_id не создаёт вторую запись
  (FR-14);
- два факта с разными external_event_id записываются оба (семантические
  дубли — не забота этого агрегата).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from billing.domain.consumption_stream import ExternalEventId, Quantity
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def test_recording_the_same_external_event_id_twice_is_a_no_op(db_connection) -> None:
    repo = PostgresConsumptionStreamRepository(db_connection)
    quantity = Quantity(value=Decimal("12.4"), metric="water_m3")
    external_event_id = ExternalEventId("meter-778812-20260706")

    first = repo.record_usage(
        "acc-4471", "water_m3", quantity, external_event_id, now=_dt(2026, 7, 6)
    )
    second = repo.record_usage(
        "acc-4471", "water_m3", quantity, external_event_id, now=_dt(2026, 7, 6)
    )

    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.event.event_id == first.event.event_id

    events = repo.events_for("acc-4471", "water_m3")
    assert len(events) == 1


def test_two_facts_with_different_external_event_ids_are_both_recorded(db_connection) -> None:
    repo = PostgresConsumptionStreamRepository(db_connection)
    quantity = Quantity(value=Decimal("12.4"), metric="water_m3")

    repo.record_usage(
        "acc-4471", "water_m3", quantity, ExternalEventId("dispatch-1"), now=_dt(2026, 7, 6)
    )
    repo.record_usage(
        "acc-4471", "water_m3", quantity, ExternalEventId("dispatch-2"), now=_dt(2026, 7, 6)
    )

    events = repo.events_for("acc-4471", "water_m3")
    assert len(events) == 2
    assert {e.external_event_id.value for e in events} == {"dispatch-1", "dispatch-2"}


def test_same_external_event_id_is_scoped_to_the_stream_identity(db_connection) -> None:
    """(account_id, metric) — часть идентичности агрегата: тот же
    external_event_id в другом потоке (другой аккаунт или метрика) — не
    дубликат, а отдельный факт."""
    repo = PostgresConsumptionStreamRepository(db_connection)
    external_event_id = ExternalEventId("shared-id")

    repo.record_usage(
        "acc-4471",
        "water_m3",
        Quantity(value=Decimal("12.4"), metric="water_m3"),
        external_event_id,
        now=_dt(2026, 7, 6),
    )
    result = repo.record_usage(
        "acc-9999",
        "water_m3",
        Quantity(value=Decimal("5.0"), metric="water_m3"),
        external_event_id,
        now=_dt(2026, 7, 6),
    )

    assert result.is_duplicate is False
    assert len(repo.events_for("acc-4471", "water_m3")) == 1
    assert len(repo.events_for("acc-9999", "water_m3")) == 1


def test_meta_is_stored_and_returned(db_connection) -> None:
    repo = PostgresConsumptionStreamRepository(db_connection)

    result = repo.record_usage(
        "acc-9012",
        "downtime_hours",
        Quantity(value=Decimal("3"), metric="downtime_hours"),
        ExternalEventId("qa-review-77213"),
        meta={"reviewed_by": "supervisor-42", "supersedes_meta": "qa-agent-v2-run-441"},
        now=_dt(2026, 7, 6),
    )

    assert result.event.meta["reviewed_by"] == "supervisor-42"
