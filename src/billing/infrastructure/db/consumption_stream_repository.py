"""Реализация порта ``ConsumptionStreamRepository`` (domain) поверх psycopg3.

Идемпотентность здесь не ловится через исключение (в отличие от
``PostgresReferenceParameterRepository`` и его ``ExclusionViolation`` —
см. infrastructure/db/reference_parameter_repository.py): ``INSERT ... ON
CONFLICT DO NOTHING`` — штатный, не ошибочный путь. Дубликат от счётчика или
IoT-шлюза при обрыве связи — рутинная ситуация (billing_aggregates.md §6),
а не нарушение инварианта конкурентной записью.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from billing.domain.consumption_stream import (
    ConsumptionStream,
    ConsumptionStreamRepository,
    ExternalEventId,
    RecordUsageResult,
    UsageEvent,
)
from billing.domain.shared import BillingPeriod, Quantity

_SELECT_COLUMNS = """
    event_id, account_id, metric, quantity_value, external_event_id, meta, recorded_at
"""


class PostgresConsumptionStreamRepository(ConsumptionStreamRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def record_usage(
        self,
        account_id: str,
        metric: str,
        quantity: Quantity,
        external_event_id: ExternalEventId,
        meta: Mapping[str, Any] | None = None,
        *,
        now: datetime,
    ) -> RecordUsageResult:
        stream = ConsumptionStream(account_id=account_id, metric=metric)
        event, _domain_event = stream.record_usage(quantity, external_event_id, meta, now=now)

        inserted = self._conn.execute(
            f"""
            INSERT INTO usage_event (
                event_id, account_id, metric, quantity_value, external_event_id, meta, recorded_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT usage_event_external_event_id_unique DO NOTHING
            RETURNING {_SELECT_COLUMNS}
            """,
            (
                event.event_id,
                event.account_id,
                event.metric,
                event.quantity.value,
                event.external_event_id.value,
                Jsonb(dict(event.meta)),
                event.recorded_at,
            ),
        ).fetchone()

        if inserted is not None:
            return RecordUsageResult(event=self._row_to_event(inserted), is_duplicate=False)

        existing = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM usage_event
            WHERE account_id = %s AND metric = %s AND external_event_id = %s
            """,
            (account_id, metric, external_event_id.value),
        ).fetchone()
        if existing is None:
            raise RuntimeError(
                "usage_event unique conflict was reported but no matching row was found"
            )
        return RecordUsageResult(event=self._row_to_event(existing), is_duplicate=True)

    def events_for(
        self, account_id: str, metric: str, *, period: BillingPeriod | None = None
    ) -> list[UsageEvent]:
        """Фильтр по периоду — через ``recorded_at`` (когда факт принят
        системой). Это приближение: "принято в этом периоде" — не то же
        самое, что "относится к этому периоду" (см. открытый вопрос №3 в
        use_case.md про формализацию пересмотров UC-8) — но для happy-path
        свёртки потребления (UC-4/UC-6/UC-7) факты сейчас всегда приходят с
        ``now``, попадающим в свой же расчётный период, так что этого
        достаточно на этой фазе."""
        if period is None:
            rows = self._conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM usage_event
                WHERE account_id = %s AND metric = %s
                ORDER BY recorded_at
                """,
                (account_id, metric),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM usage_event
                WHERE account_id = %s AND metric = %s
                  AND recorded_at >= %s AND recorded_at < %s
                ORDER BY recorded_at
                """,
                (account_id, metric, period.start, period.end),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: tuple) -> UsageEvent:
        (
            event_id,
            account_id,
            metric,
            quantity_value,
            external_event_id,
            meta,
            recorded_at,
        ) = row
        return UsageEvent(
            event_id=event_id,
            account_id=account_id,
            metric=metric,
            quantity=Quantity(value=Decimal(quantity_value), metric=metric),
            external_event_id=ExternalEventId(external_event_id),
            recorded_at=recorded_at,
            meta=meta,
        )
