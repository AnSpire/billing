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


@router.get(
    "/{account_id}/balance",
    response_model=BalanceOut,
    summary="Баланс лицевого счёта: подтверждённый и прогнозный",
)
def balance(account_id: str, conn: Connection = Depends(db_connection)) -> BalanceOut:
    """Возвращает два баланса лицевого счёта — их разница и есть смысл эндпоинта.

    - `balance` — **подтверждённый**: сумма проводок, прошедших полный жизненный
      цикл. Это то, что можно показывать клиенту как долг и на что опираться в
      сверке.
    - `projected_balance` — **прогнозный**: подтверждённые проводки плюс те, что
      ещё в полёте (начисление посчитано, квитанция выпущена, проводка не
      финализирована).

    Проводки создаёт только сага (`post_charge` / `post_correction`) — прямой
    записи в журнал через API нет, поэтому баланс всегда выводится из журнала и
    не может с ним разойтись.

    Счёт без единой проводки — не ошибка: вернётся нулевой баланс.
    """
    repo = PostgresAccountRepository(conn)
    return BalanceOut(
        balance=money_out(repo.balance(account_id)),
        projected_balance=money_out(repo.projected_balance(account_id)),
    )


@router.get(
    "/{account_id}/ledger",
    response_model=list[LedgerEntryOut],
    summary="Журнал проводок по лицевому счёту",
)
def ledger(account_id: str, conn: Connection = Depends(db_connection)) -> list[LedgerEntryOut]:
    """Отдаёт журнал проводок — источник истины, из которого выводится баланс.

    Журнал только дописывается: ошибочное начисление не стирается и не правится,
    а компенсируется корректирующей проводкой (её порождает
    `POST /assessments/{account_id}/{period}/recalculate` или веерный пересчёт
    после коррекции справочного параметра). Поэтому по журналу всегда видно, что
    было начислено, когда и на основании какой версии начисления.

    Записи создаёт исключительно сага; ручного эндпоинта записи нет by design.

    Счёт без проводок — пустой список, а не `404`.
    """
    entries = PostgresAccountRepository(conn).entries_for(account_id)
    return [ledger_entry_out(e) for e in entries]
