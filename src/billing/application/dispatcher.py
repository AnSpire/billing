"""Внутрипроцессный диспетчер доменных событий.

Это и есть "сага" в терминах этого проекта (CLAUDE.md §2): не брокер и не
отдельный сервис, а реестр обработчиков вида "на событие X вызови Y".
Каждый обработчик выполняется в **своей собственной** транзакции БД — по
правилу DDD "одна транзакция меняет один агрегат". Если в цепочке участвуют
три агрегата (например в будущем ``Assessment -> Invoice -> Account``), это
три вызова ``dispatch`` с тремя отдельными транзакциями, а не один большой
``BEGIN``.

Фаза 0 не содержала доменной логики; в фазе 6 сюда подключились обработчики
саги ``Assessment -> Invoice -> Account`` (application/billing_saga.py), и
ради них у обработчика появилась возможность вернуть следующие события
цепочки (``Sequence[DomainEvent] | None``) вместо ``None``. Это не
изменение поведения для старых обработчиков (пустой возврат — то же самое,
что раньше), а способ продолжить цепочку, НЕ вызывая ``dispatch`` изнутри ещё
не закоммиченной транзакции: если бы обработчик ``AssessmentCalculated``
сам дёргал ``dispatcher.dispatch(InvoiceIssued)`` до выхода из своего
``with self._connection_factory() as conn:``, обработчик ``InvoiceIssued``
получил бы СВОЁ, отдельное соединение и не увидел бы ещё не закоммиченный
``Invoice`` — разные транзакции, грязного чтения между ними нет. Поэтому
``dispatch`` сперва даёт транзакции обработчика закоммититься (выход из
``with``), и только потом рекурсивно диспатчит то, что он вернул.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from typing import TypeVar

from psycopg import Connection

from billing.domain.events import DomainEvent

E = TypeVar("E", bound=DomainEvent)
EventHandler = Callable[[E, Connection], "Sequence[DomainEvent] | None"]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class EventDispatcher:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory
        self._handlers: dict[type[DomainEvent], list[EventHandler]] = defaultdict(
            list
        )

    def subscribe(
        self, event_type: type[E], handler: EventHandler[E]
    ) -> None:
        self._handlers[event_type].append(handler)

    def handlers_for(self, event_type: type[DomainEvent]) -> list[EventHandler]:
        return list(self._handlers[event_type])

    def dispatch(self, event: DomainEvent) -> None:
        """Вызывает все обработчики, подписанные на тип этого события.

        Каждый обработчик получает свежее соединение и коммитит независимо
        от остальных: если второй обработчик упадёт, изменения первого уже
        закоммичены и не откатываются (см. docstring модуля). События,
        которые обработчик вернул, диспатчатся РЕКУРСИВНО и ПОСЛЕ коммита
        его собственной транзакции — продолжение цепочки саги.
        """
        for handler in self.handlers_for(type(event)):
            with self._connection_factory() as conn:
                produced = handler(event, conn)
            for next_event in produced or ():
                self.dispatch(next_event)

    def dispatch_all(self, events: Iterator[DomainEvent] | list[DomainEvent]) -> None:
        for event in events:
            self.dispatch(event)
