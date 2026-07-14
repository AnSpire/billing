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


@router.post(
    "/{account_id}/usage",
    response_model=RecordUsageOut,
    status_code=201,
    summary="Записать факт потребления (идемпотентно)",
    responses={
        201: {"description": "Событие записано впервые"},
        200: {"description": "Дубль по external_event_id — вернули уже записанное событие"},
        422: {"description": "metric в теле не совпадает с метрикой количества"},
    },
)
def record_usage(
    account_id: str,
    body: RecordUsageIn,
    response: Response,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> RecordUsageOut:
    """Добавляет показание/факт потребления в поток лицевого счёта.

    **Идемпотентно по `external_event_id`.** Это идентификатор события в
    системе-источнике (АСКУЭ, импорт показаний): повторная отправка того же
    `external_event_id` ничего не записывает и возвращает уже сохранённое
    событие. Такой повтор — штатный случай, а не ошибка, поэтому ответ `200` с
    `is_duplicate: true`, а не `409`. Первая запись — `201` с
    `is_duplicate: false`. Источник может безопасно ретраить.

    `quantity` + `metric` образуют величину с единицей измерения; `metric` в теле
    должен совпадать с метрикой, иначе `422`. `meta` — произвольный JSON-мешок
    для данных источника (номер прибора, тип показания), на расчёт не влияет.

    Начисление события не запускают: биллинг стартует явно через
    `POST /assessments`, который собирает потребление за период.
    """
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


@router.get(
    "/{account_id}/usage",
    response_model=list[UsageEventOut],
    summary="История потребления по счёту и метрике",
)
def list_usage(
    account_id: str,
    metric: str = Query(..., description="метрика, напр. 'electricity_kwh'"),
    period: str | None = Query(None, description="'YYYY-MM'; без него — вся история"),
    conn: Connection = Depends(db_connection),
) -> list[UsageEventOut]:
    """Возвращает записанные факты потребления по одной метрике лицевого счёта.

    `metric` обязателен — поток потребления секционирован по метрикам, суммировать
    киловатт-часы с кубометрами бессмысленно. `period` (`'YYYY-MM'`) сужает выборку
    до расчётного периода; без него отдаётся вся история.

    Это ровно тот набор событий, который `POST /assessments` возьмёт в расчёт за
    указанный период — удобно для сверки «почему начислили столько».

    Несуществующий счёт — не ошибка, просто пустой список.
    """
    billing_period = BillingPeriod.parse(period) if period else None
    events = PostgresConsumptionStreamRepository(conn).events_for(
        account_id, metric, period=billing_period
    )
    return [usage_event_out(e) for e in events]
