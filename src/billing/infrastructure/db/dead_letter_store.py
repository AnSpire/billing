"""Dead-letter лог веерного пересчёта — PLAN.md, фаза 8; use_case.md UC-7,
«Обработка ошибок веера»: падение по конфликту дефолтов на одном аккаунте
не должно останавливать пересчёт остальных 39 999 — вместо этого запись
уходит сюда, на ручной разбор.

Не отдельный DDD-агрегат и не порт в домене (PLAN.md, «Repository — порт в
домене» — это правило для агрегатов с инвариантами и идентичностью; здесь же
плоский операционный журнал сбоев саги, ему нечего защищать инвариантами,
только append-only запись). Поэтому — просто функции поверх ``Connection``,
как и остальной SQL-код проекта (CLAUDE.md §7: никакой ORM).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection


@dataclass(frozen=True)
class DeadLetterEntry:
    account_id: str
    period: str
    key: str
    jurisdiction: str
    reason: str
    retryable: bool
    detail: str
    occurred_at: datetime


def record_dead_letter(
    conn: Connection,
    *,
    account_id: str,
    period: str,
    key: str,
    jurisdiction: str,
    reason: str,
    retryable: bool,
    detail: str,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO dead_letter (
            id, account_id, period, parameter_key, parameter_jurisdiction,
            reason, retryable, detail, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (uuid.uuid4(), account_id, period, key, jurisdiction, reason, retryable, detail, now),
    )


def find_dead_letters_for_account(conn: Connection, account_id: str) -> list[DeadLetterEntry]:
    rows = conn.execute(
        """
        SELECT account_id, period, parameter_key, parameter_jurisdiction,
               reason, retryable, detail, occurred_at
        FROM dead_letter WHERE account_id = %s ORDER BY occurred_at
        """,
        (account_id,),
    ).fetchall()
    return [
        DeadLetterEntry(
            account_id=row[0],
            period=row[1],
            key=row[2],
            jurisdiction=row[3],
            reason=row[4],
            retryable=row[5],
            detail=row[6],
            occurred_at=row[7],
        )
        for row in rows
    ]
