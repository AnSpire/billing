"""Квитанции — Invoice. Только чтение: записи создаёт исключительно сага
(``Invoice.issue``/``issue_correcting``), выставлять квитанцию «руками» нельзя —
это обошло бы инвариант «квитанция замораживает копию начисления»."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from psycopg import Connection

from billing.infrastructure.db.invoice_repository import PostgresInvoiceRepository
from billing.interface.http.deps import db_connection
from billing.interface.http.serialization import InvoiceOut, invoice_out

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.get(
    "/{invoice_id}",
    response_model=InvoiceOut,
    summary="Получить квитанцию по идентификатору",
    responses={404: {"description": "Квитанции с таким id нет"}},
)
def get(invoice_id: uuid.UUID, conn: Connection = Depends(db_connection)) -> InvoiceOut:
    """Возвращает квитанцию — замороженный снимок начисления на момент выпуска.

    Квитанция хранит **копию** строк начисления, а не ссылку на него: после
    выпуска её сумма и состав не меняются, даже если начисление пересчитают.
    Расхождение оформляется отдельной корректирующей квитанцией, ссылающейся на
    исходную. Так у клиента на руках остаётся ровно тот документ, который ему
    выставили.

    Из этого следует, что эндпоинта «выставить квитанцию руками» нет: квитанции
    создаёт только сага (`Invoice.issue` / `issue_correcting`) — иначе инвариант
    заморозки можно было бы обойти. Получить id квитанции можно из ответа
    `POST /assessments` (поле `invoice`) или `.../recalculate`
    (`correcting_invoice`).
    """
    invoice = PostgresInvoiceRepository(conn).get(invoice_id)
    if invoice is None:
        raise HTTPException(404, f"invoice {invoice_id} not found")
    return invoice_out(invoice)
