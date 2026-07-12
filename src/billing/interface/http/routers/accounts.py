"""Лицевой счёт — Account. Только чтение: баланс (подтверждённый и прогнозный)
и журнал проводок. Проводки создаёт сага (post_charge/post_correction), прямой
записи через API нет."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from psycopg import Connection
from pydantic import BaseModel

from billing.infrastructure.db.account_repository import PostgresAccountRepository
from billing.interface.http.deps import db_connection
from billing.interface.http.serialization import (
    LedgerEntryOut,
    MoneyOut,
    ledger_entry_out,
    money_out,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class BalanceOut(BaseModel):
    balance: MoneyOut
    projected_balance: MoneyOut


@router.get("/{account_id}/balance", response_model=BalanceOut)
def balance(account_id: str, conn: Connection = Depends(db_connection)) -> BalanceOut:
    repo = PostgresAccountRepository(conn)
    return BalanceOut(
        balance=money_out(repo.balance(account_id)),
        projected_balance=money_out(repo.projected_balance(account_id)),
    )


@router.get("/{account_id}/ledger", response_model=list[LedgerEntryOut])
def ledger(account_id: str, conn: Connection = Depends(db_connection)) -> list[LedgerEntryOut]:
    entries = PostgresAccountRepository(conn).entries_for(account_id)
    return [ledger_entry_out(e) for e in entries]
