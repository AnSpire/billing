"""DoD фазы 0: команда порождает событие, диспетчер вызывает подписанный
обработчик, каждый шаг — в своей транзакции.

Обработчики здесь намеренно пишут в реальную, независимо коммитящуюся БД (а
не в фикстуру с откатом): иначе нельзя было бы честно проверить, что каждый
обработчик действительно живёт в отдельной транзакции, а не в одной общей
с тестом.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from psycopg import Connection

from billing.application.dispatcher import EventDispatcher
from billing.domain.events import DomainEvent
from billing.infrastructure.db.connection import new_connection


@dataclass(frozen=True, kw_only=True)
class Pinged(DomainEvent):
    message: str


def raise_ping(message: str) -> list[DomainEvent]:
    """Заглушка команды: агрегат кладёт событие в список необработанных событий."""
    return [Pinged(message=message)]


@pytest.fixture
def probe_table(test_database_url: str) -> Iterator[None]:
    with new_connection(test_database_url) as conn:
        conn.execute("CREATE TABLE dispatcher_probe (handler_name text NOT NULL)")
    yield
    with new_connection(test_database_url) as conn:
        conn.execute("DROP TABLE dispatcher_probe")


def _recorded_handlers(test_database_url: str) -> list[str]:
    with new_connection(test_database_url) as conn:
        cur = conn.execute("SELECT handler_name FROM dispatcher_probe ORDER BY handler_name")
        return [row[0] for row in cur.fetchall()]


def test_dispatch_invokes_subscribed_handler(
    probe_table: None, test_database_url: str
) -> None:
    dispatcher = EventDispatcher(lambda: new_connection(test_database_url))
    seen_messages: list[str] = []

    def handler(event: Pinged, conn: Connection) -> None:
        seen_messages.append(event.message)
        conn.execute(
            "INSERT INTO dispatcher_probe (handler_name) VALUES (%s)", ("handler_one",)
        )

    dispatcher.subscribe(Pinged, handler)

    dispatcher.dispatch_all(raise_ping("hello"))

    assert seen_messages == ["hello"]
    assert _recorded_handlers(test_database_url) == ["handler_one"]


def test_each_handler_commits_its_own_transaction_independently(
    probe_table: None, test_database_url: str
) -> None:
    """Сбой одного обработчика не откатывает уже закоммиченную работу другого.

    Это и есть свойство саги из CLAUDE.md §2: цепочка обработчиков — не одна
    большая транзакция, поэтому падение на шаге N не трогает шаги 1..N-1.
    """
    dispatcher = EventDispatcher(lambda: new_connection(test_database_url))

    def handler_ok(event: Pinged, conn: Connection) -> None:
        conn.execute(
            "INSERT INTO dispatcher_probe (handler_name) VALUES (%s)", ("handler_ok",)
        )

    def handler_fails(event: Pinged, conn: Connection) -> None:
        conn.execute(
            "INSERT INTO dispatcher_probe (handler_name) VALUES (%s)", ("handler_fails",)
        )
        raise RuntimeError("boom")

    dispatcher.subscribe(Pinged, handler_ok)
    dispatcher.subscribe(Pinged, handler_fails)

    with pytest.raises(RuntimeError, match="boom"):
        dispatcher.dispatch(Pinged(message="hi"))

    assert _recorded_handlers(test_database_url) == ["handler_ok"]
