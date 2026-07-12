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


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get(invoice_id: uuid.UUID, conn: Connection = Depends(db_connection)) -> InvoiceOut:
    invoice = PostgresInvoiceRepository(conn).get(invoice_id)
    if invoice is None:
        raise HTTPException(404, f"invoice {invoice_id} not found")
    return invoice_out(invoice)
