"""Поток потребления — ConsumptionStream. Приём фактов идемпотентен по
``external_event_id`` (повторная запись — no-op, отвечаем ``200`` с
``is_duplicate=true``, а не ``409``: домен трактует это как штатный случай)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from psycopg import Connection
from pydantic import BaseModel

from billing.domain.consumption_stream import ExternalEventId
from billing.domain.shared import BillingPeriod, Quantity
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)
from billing.interface.http.deps import db_connection, get_now
from billing.interface.http.serialization import UsageEventOut, usage_event_out

router = APIRouter(prefix="/accounts", tags=["consumption"])


class RecordUsageIn(BaseModel):
    metric: str
    quantity: Decimal
    external_event_id: str
    meta: dict[str, Any] | None = None


class RecordUsageOut(BaseModel):
    event_id: str
    is_duplicate: bool


@router.post("/{account_id}/usage", response_model=RecordUsageOut)
def record_usage(
    account_id: str,
    body: RecordUsageIn,
    response: Response,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> RecordUsageOut:
    result = PostgresConsumptionStreamRepository(conn).record_usage(
        account_id,
        body.metric,
        Quantity(value=body.quantity, metric=body.metric),
        ExternalEventId(body.external_event_id),
        body.meta,
        now=now,
    )
    response.status_code = 200 if result.is_duplicate else 201
    return RecordUsageOut(event_id=str(result.event.event_id), is_duplicate=result.is_duplicate)


@router.get("/{account_id}/usage", response_model=list[UsageEventOut])
def list_usage(
    account_id: str,
    metric: str = Query(...),
    period: str | None = Query(None, description="'YYYY-MM'; без него — вся история"),
    conn: Connection = Depends(db_connection),
) -> list[UsageEventOut]:
    billing_period = BillingPeriod.parse(period) if period else None
    events = PostgresConsumptionStreamRepository(conn).events_for(
        account_id, metric, period=billing_period
    )
    return [usage_event_out(e) for e in events]
