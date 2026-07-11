from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import Connection

from billing.infrastructure.db.connection import new_connection
from billing.infrastructure.db.migrate import apply_migrations

_DEFAULT_TEST_DATABASE_URL = "postgresql://billing:billing@localhost:5433/billing_test"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return os.environ.get("BILLING_TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def _schema(test_database_url: str) -> None:
    """Прогоняет реальные миграции проекта на тестовой БД один раз за сессию.

    Сейчас каталог миграций пуст (фаза 0 не добавляет доменных таблиц) — это
    подготовка для фаз 1+, когда там появятся настоящие миграции.
    """
    with new_connection(test_database_url) as conn:
        apply_migrations(conn)


@pytest.fixture
def db_connection(test_database_url: str) -> Iterator[Connection]:
    """Соединение на один тест: транзакция откатывается в конце теста.

    Годится для тестов, которым не нужно видеть коммит с другого соединения
    (обычные doctest-ы доменной логики в следующих фазах). Для тестов, которые
    проверяют поведение через *несколько независимых* соединений (как
    диспетчер событий), используйте ``new_connection`` напрямую — см.
    ``test_dispatcher.py``.
    """
    conn = psycopg.connect(test_database_url)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
