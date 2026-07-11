"""Подключение к PostgreSQL.

Никакой ORM и никакого пула соединений — на масштабе одного монолита это не
нужно (см. CLAUDE.md §1). Каждый вызов ``new_connection`` открывает новое
соединение; вызывающий код (в первую очередь ``EventDispatcher``) решает, где
проходят границы транзакций.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import Connection

_DEFAULT_DATABASE_URL = "postgresql://billing:billing@localhost:5433/billing"


def database_url() -> str:
    return os.environ.get("BILLING_DATABASE_URL", _DEFAULT_DATABASE_URL)


@contextmanager
def new_connection(dsn: str | None = None) -> Iterator[Connection]:
    """Открывает соединение как одну транзакцию.

    Коммитит при успешном выходе из блока ``with``, откатывает при
    исключении — это штатное поведение контекстного менеджера соединения в
    psycopg3. Соединение закрывается в любом случае.
    """
    conn = psycopg.connect(dsn or database_url())
    try:
        with conn:
            yield conn
    finally:
        conn.close()
